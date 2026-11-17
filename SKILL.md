---
name: extract-video-transcript
description: 从 Bilibili 视频链接、BV 号或本地音视频文件提取带时间戳的全文逐字稿，并按需总结。优先使用视频已有字幕；无字幕时获取公开音频流或读取本地媒体，用 faster-whisper 本地转写。用户要求“提取视频逐字稿”“视频转文字”“提取字幕”“整理全文稿”“转写分P视频”“根据视频内容总结”时使用。输出包括 Markdown 逐字稿和 JSON 任务记录，不适用于纯文本或已有逐字稿的单独润色。
---

# 视频逐字稿提取

使用 `scripts/video_transcript.py` 完成确定性流程；把内容校订、重复分段判断和总结留给自己处理。

## 工作流程

1. 确认输入是 B 站链接/BV 号或本地音视频文件，并在当前工作区确定输出目录。
2. 对 B 站输入先检查分 P、时长和字幕，不直接下载全部音频：

```bash
python3 <skill-dir>/scripts/video_transcript.py '<url>' --inspect-only --pages all
```

3. 根据用户要求选择 `--pages`：支持 `all`、`1-4`、`1,3`。用户要求“全文”时覆盖全部内容；若中配与原版明显重复，可只转写一种语言，但必须明确说明排除的是重复版本，不能静默缩小范围。
4. 检查 `faster-whisper` 是否可用。已有字幕的 B 站任务只需 Python 标准库；需要语音识别时才安装依赖。
5. 运行脚本生成初稿，再校订、总结并验证交付物。

## 依赖与模型

先检查：

```bash
python3 -c 'import faster_whisper; print(faster_whisper.__version__)'
```

若缺失，告知用户将安装临时转写依赖。优先在任务输出目录或工作区创建专用虚拟环境，不全局安装：

```bash
python3 -m venv '<task-dir>/.venv'
'<task-dir>/.venv/bin/python' -m pip install faster-whisper
```

使用该环境的 Python 执行脚本。首次加载模型会联网下载模型权重，之后音频识别在本机完成；不需要 OpenAI API Key。默认模型为 `turbo`，CPU 上默认 `int8`。追求更高准确率且资源允许时可改用 `large-v3`；快速测试可用 `tiny`，但不要将 `tiny` 的结果当作高质量交付。

## 执行

B 站视频：

```bash
python3 <skill-dir>/scripts/video_transcript.py '<url-or-bvid>' \
  --output-dir '<workspace>/<task-output>' \
  --pages 1-4 \
  --language zh \
  --initial-prompt '根据视频主题填写人名、术语和专有名词'
```

本地音视频：

```bash
'<task-dir>/.venv/bin/python' <skill-dir>/scripts/video_transcript.py '<media-file>' \
  --output-dir '<workspace>/<task-output>' \
  --language zh
```

自动检测语言时使用 `--language auto`。需要指定 B 站字幕时，将 `--subtitle-language` 设为接口返回的 `lan` 或 `lan_doc`；没有匹配字幕会明确回退到语音识别。已有输出需要覆盖时才使用 `--overwrite`。

长视频转写应在后台运行并报告进度。脚本成功后默认清理临时音频和中间 JSON；调试或需要续查时传入 `--keep-work`。失败时会保留工作目录和 `task-record.json`，不要删除诊断现场。

## 校订与总结

- 读取 `transcript.md`，确认每个目标分 P 都有正文、时间戳和结尾，不只看脚本退出码。
- 修正有充分上下文依据的识别错字和标点；不要编造听不清的内容，不要擅自删改重复、口头禅或论证顺序。
- 明确区分“已有字幕”和“语音识别稿”。未经完整人工复听，不宣称逐字准确率 100%。
- 用户要求逐字稿时保持内容完整；另做精简版时使用单独标题或文件。
- 总结应有机讲清核心问题、论证脉络、例子和结论，避免机械复述各分 P 或堆砌口号。区分作者观点与已验证事实；涉及健康、安全、财务等现实决策时保留必要边界。
- 将总结写入最终 Markdown 或在回复中呈现，按用户要求交付。

## 输出与记录

脚本固定生成：

- `transcript.md`：标题、来源、各分 P 的带时间戳全文。
- `task-record.json`：原始输入、规范化来源、参数、每段采用的方法、字幕语言或模型、音频大小、分段数量、警告、输出路径和清理状态。

交付时链接最终文件，简述覆盖范围、采用的方法和仍需注意的准确性限制。若只完成部分分段或验证受阻，明确说明剩余事项。

## 失败处理

- 无可用字幕且缺少 `faster-whisper`：安装任务专用依赖后继续，不假装已有文本。
- B 站接口无音频或下载失败：保留错误与工作目录，检查接口响应、链接有效性和网络；不要绕过登录、付费或访问控制。
- 某一分段为空或失败：不将整项任务标为完成。
- 输出已存在：先检查，只有用户要求替换或明确允许时才使用 `--overwrite`。
- 成功后只清理由本任务创建的临时目录；保留用户已有文件和需要交付的结果。
