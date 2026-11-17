#!/usr/bin/env python3
"""Extract timestamped transcripts from Bilibili videos or local media."""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

USER_AGENT = "Mozilla/5.0"
BV_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}")


class TranscriptError(RuntimeError):
    pass


def request_headers(referer):
    return {"User-Agent": USER_AGENT, "Referer": referer}


def get_json(url, referer, timeout):
    request = Request(url, headers=request_headers(referer))
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise TranscriptError(f"请求失败：{url}\n{exc}") from exc
    if payload.get("code") not in (None, 0):
        raise TranscriptError(
            f"接口返回错误 {payload.get('code')}：{payload.get('message', '未知错误')}"
        )
    return payload


def parse_pages(spec, count):
    if spec == "all":
        return list(range(1, count + 1))
    selected = set()
    for token in spec.split(","):
        token = token.strip()
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", token)
        if not match:
            raise TranscriptError(f"无效分 P 范围：{token}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start or end > count:
            raise TranscriptError(f"分 P 范围超出 1—{count}：{token}")
        selected.update(range(start, end + 1))
    return sorted(selected)


def choose_subtitle(subtitles, requested_language):
    if not subtitles:
        return None
    if requested_language:
        for subtitle in subtitles:
            if requested_language.lower() in {
                str(subtitle.get("lan", "")).lower(),
                str(subtitle.get("lan_doc", "")).lower(),
            }:
                return subtitle
        return None
    return subtitles[0]


def subtitle_segments(subtitle, referer, timeout):
    subtitle_url = subtitle.get("subtitle_url") or ""
    if subtitle_url.startswith("//"):
        subtitle_url = "https:" + subtitle_url
    if not subtitle_url.startswith("https://"):
        raise TranscriptError("字幕地址缺失或不是 HTTPS 地址")
    payload = get_json(subtitle_url, referer, timeout)
    rows = []
    for item in payload.get("body") or []:
        text = str(item.get("content") or "").strip()
        if not text:
            continue
        rows.append(
            {
                "start": float(item.get("from", 0)),
                "end": float(item.get("to", item.get("from", 0))),
                "text": text,
            }
        )
    if not rows:
        raise TranscriptError("字幕文件为空")
    return rows


def download_audio(bvid, cid, destination, referer, timeout):
    query = urlencode({"bvid": bvid, "cid": cid, "fnval": 16, "fourk": 0})
    payload = get_json(
        f"https://api.bilibili.com/x/player/playurl?{query}", referer, timeout
    )
    audio_list = ((payload.get("data") or {}).get("dash") or {}).get("audio") or []
    if not audio_list:
        raise TranscriptError(f"CID {cid} 没有可用音频流")
    audio = max(audio_list, key=lambda item: item.get("bandwidth", 0))
    urls = [audio.get("baseUrl") or audio.get("base_url")]
    urls.extend(audio.get("backupUrl") or audio.get("backup_url") or [])
    errors = []
    for url in filter(None, urls):
        partial = destination.with_suffix(destination.suffix + ".part")
        try:
            request = Request(url, headers=request_headers(referer))
            with urlopen(request, timeout=timeout) as response, partial.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
            if partial.stat().st_size == 0:
                raise TranscriptError("下载结果为空")
            partial.replace(destination)
            return destination.stat().st_size, audio.get("id"), audio.get("bandwidth")
        except (HTTPError, URLError, TimeoutError, OSError, TranscriptError) as exc:
            errors.append(str(exc))
            partial.unlink(missing_ok=True)
    raise TranscriptError("音频下载失败：\n" + "\n".join(errors))


class WhisperTranscriber:
    def __init__(self, args):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriptError(
                "缺少 faster-whisper。请在任务专用虚拟环境中安装："
                "python3 -m pip install faster-whisper"
            ) from exc
        threads = args.cpu_threads or min(8, os.cpu_count() or 1)
        self.model_name = args.model
        self.model = WhisperModel(
            args.model,
            device=args.device,
            compute_type=args.compute_type,
            cpu_threads=threads,
        )

    def transcribe(self, media_path, language, initial_prompt):
        segments, info = self.model.transcribe(
            str(media_path),
            language=None if language == "auto" else language,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=initial_prompt or None,
        )
        duration = getattr(info, "duration", None)
        rows = []
        for segment in segments:
            text = segment.text.strip()
            if not text or (duration is not None and segment.start >= duration):
                continue
            rows.append(
                {
                    "start": float(segment.start),
                    "end": float(min(segment.end, duration) if duration else segment.end),
                    "text": text,
                }
            )
        return rows, {
            "language": getattr(info, "language", None),
            "duration": duration,
        }


def timestamp(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def join_text(left, right):
    if left and right and left[-1].isascii() and right[0].isascii() and not left[-1].isspace() and not right[0].isspace():
        return left + " " + right
    return left + right


def render_paragraphs(segments, max_seconds=55):
    paragraphs = []
    group = []
    start = None
    for segment in segments:
        if not group:
            start = segment["start"]
        if group and segment["start"] - start >= max_seconds:
            text = ""
            for item in group:
                text = join_text(text, item["text"])
            paragraphs.append((start, text))
            group = []
            start = segment["start"]
        group.append(segment)
    if group:
        text = ""
        for item in group:
            text = join_text(text, item["text"])
        paragraphs.append((start, text))
    return paragraphs


def render_markdown(title, source, results, language, model):
    lines = [
        f"# {title}",
        "",
        f"- 来源：{source}",
        f"- 转写语言：{language}",
        "- 说明：优先采用视频已有字幕；无字幕的分段由 faster-whisper 本地识别。识别稿可能仍有错字，关键内容请结合时间戳回查。",
        "",
        "## 逐字稿",
        "",
    ]
    for result in results:
        lines.extend([f"### {result['label']}", ""])
        method = result["method"]
        if method == "subtitle":
            lines.extend([f"字幕：{result.get('subtitle_language', '未知语言')}", ""])
        else:
            lines.extend([f"识别模型：{model}", ""])
        for start, text in render_paragraphs(result["segments"]):
            lines.extend([f"[{timestamp(start)}] {text}", ""])
    return "\n".join(lines).rstrip() + "\n"


def inspect_bilibili(bvid, pages_spec, timeout):
    referer = f"https://www.bilibili.com/video/{bvid}/"
    view = get_json(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", referer, timeout
    )["data"]
    selected = parse_pages(pages_spec, len(view["pages"]))
    report = {"title": view["title"], "bvid": bvid, "pages": []}
    for number in selected:
        page = view["pages"][number - 1]
        query = urlencode({"bvid": bvid, "cid": page["cid"]})
        player = get_json(
            f"https://api.bilibili.com/x/player/v2?{query}", referer, timeout
        )
        subtitles = ((player.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        report["pages"].append(
            {
                "page": number,
                "part": page["part"],
                "cid": page["cid"],
                "duration": page["duration"],
                "subtitles": [
                    {"lan": item.get("lan"), "lan_doc": item.get("lan_doc")}
                    for item in subtitles
                ],
            }
        )
    return report


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="B 站链接/BV 号，或本地音视频文件")
    parser.add_argument("-o", "--output-dir", help="输出目录；默认使用视频标题或文件名")
    parser.add_argument("--pages", default="all", help="B 站分 P，如 all、1-4、1,3")
    parser.add_argument("--language", default="zh", help="识别语言，默认 zh；auto 为自动检测")
    parser.add_argument("--subtitle-language", help="字幕 lan 或 lan_doc；不匹配时回退到语音识别")
    parser.add_argument("--model", default="turbo", help="faster-whisper 模型，默认 turbo")
    parser.add_argument("--device", default="cpu", help="识别设备，默认 cpu")
    parser.add_argument("--compute-type", default="int8", help="计算类型，默认 int8")
    parser.add_argument("--cpu-threads", type=int, help="CPU 线程数")
    parser.add_argument("--initial-prompt", default="", help="专有名词等识别提示")
    parser.add_argument("--timeout", type=int, default=60, help="网络超时秒数")
    parser.add_argument("--inspect-only", action="store_true", help="仅检查 B 站分 P 和字幕，不下载")
    parser.add_argument("--keep-work", action="store_true", help="成功后保留临时音频和识别 JSON")
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖已有输出文件")
    return parser


def main():
    args = build_parser().parse_args()
    source_path = Path(args.input).expanduser()
    is_local = source_path.is_file()
    match = BV_PATTERN.search(args.input)
    if not is_local and not match:
        raise TranscriptError("输入不是可读取的本地文件，也未找到有效 BV 号")

    if not is_local and args.inspect_only:
        print(json.dumps(inspect_bilibili(match.group(0), args.pages, args.timeout), ensure_ascii=False, indent=2))
        return 0
    if args.inspect_only:
        raise TranscriptError("--inspect-only 仅支持 B 站输入")

    bvid = None if is_local else match.group(0)
    source = str(source_path.resolve()) if is_local else f"https://www.bilibili.com/video/{bvid}/"
    default_name = source_path.stem if is_local else bvid
    output_dir = Path(args.output_dir or default_name + "_transcript").expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = output_dir / "transcript.md"
    record_path = output_dir / "task-record.json"
    for path in (transcript_path, record_path):
        if path.exists() and not args.overwrite:
            raise TranscriptError(f"输出已存在：{path}；如需覆盖请使用 --overwrite")
    work_dir = Path(tempfile.mkdtemp(prefix=".transcript-work-", dir=output_dir))
    record = {
        "input": args.input,
        "normalized_source": source,
        "output_dir": str(output_dir),
        "transcript": str(transcript_path),
        "settings": {
            "pages": args.pages,
            "language": args.language,
            "subtitle_language": args.subtitle_language,
            "model": args.model,
            "device": args.device,
            "compute_type": args.compute_type,
        },
        "pages": [],
        "warnings": [],
        "status": "running",
        "work_dir": str(work_dir),
    }

    try:
        results = []
        transcriber = None
        if is_local:
            pages = [{"page": 1, "part": source_path.stem, "cid": None, "duration": None}]
            title = source_path.stem
            referer = ""
        else:
            referer = f"https://www.bilibili.com/video/{bvid}/"
            view = get_json(
                f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", referer, args.timeout
            )["data"]
            title = view["title"]
            selected = parse_pages(args.pages, len(view["pages"]))
            pages = [view["pages"][number - 1] for number in selected]

        for page in pages:
            number = page["page"]
            label = f"P{number} {page['part']}" if not is_local else page["part"]
            print(f"处理 {label}", flush=True)
            rows = None
            page_record = {"page": number, "label": label, "cid": page["cid"]}
            if not is_local:
                query = urlencode({"bvid": bvid, "cid": page["cid"]})
                player = get_json(
                    f"https://api.bilibili.com/x/player/v2?{query}", referer, args.timeout
                )
                subtitles = ((player.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
                subtitle = choose_subtitle(subtitles, args.subtitle_language)
                if subtitle:
                    try:
                        rows = subtitle_segments(subtitle, referer, args.timeout)
                        page_record.update(
                            {"method": "subtitle", "subtitle_language": subtitle.get("lan")}
                        )
                    except TranscriptError as exc:
                        warning = f"{label} 字幕读取失败，回退到语音识别：{exc}"
                        record["warnings"].append(warning)
                        print(warning, file=sys.stderr, flush=True)

            if rows is None:
                if is_local:
                    media_path = source_path.resolve()
                    audio_bytes = media_path.stat().st_size
                    audio_id = None
                    bandwidth = None
                else:
                    media_path = work_dir / f"p{number}.m4s"
                    audio_bytes, audio_id, bandwidth = download_audio(
                        bvid, page["cid"], media_path, referer, args.timeout
                    )
                if transcriber is None:
                    transcriber = WhisperTranscriber(args)
                rows, info = transcriber.transcribe(
                    media_path, args.language, args.initial_prompt
                )
                raw_path = work_dir / f"p{number}.json"
                raw_path.write_text(
                    json.dumps({"info": info, "segments": rows}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                page_record.update(
                    {
                        "method": "asr",
                        "model": args.model,
                        "audio_bytes": audio_bytes,
                        "audio_id": audio_id,
                        "audio_bandwidth": bandwidth,
                        "detected_language": info.get("language"),
                        "duration": info.get("duration"),
                    }
                )
            if not rows:
                raise TranscriptError(f"{label} 没有生成任何文本")
            page_record["segment_count"] = len(rows)
            page_record["last_segment_end"] = rows[-1]["end"]
            record["pages"].append(page_record)
            results.append({"label": label, "segments": rows, **page_record})

        transcript_path.write_text(
            render_markdown(title, source, results, args.language, args.model), encoding="utf-8"
        )
        record["status"] = "completed"
        if not args.keep_work:
            shutil.rmtree(work_dir)
            record["work_dir"] = None
            record["work_cleaned"] = True
        else:
            record["work_cleaned"] = False
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"逐字稿：{transcript_path}")
        print(f"任务记录：{record_path}")
        return 0
    except Exception:
        record["status"] = "failed"
        record["work_preserved_for_diagnosis"] = str(work_dir)
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        raise


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TranscriptError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(1)
