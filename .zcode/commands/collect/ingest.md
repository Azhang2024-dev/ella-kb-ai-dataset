---
description: 本地优先入口：登记一个本地文件夹里已下载好的文档（不访问云盘）
argument-hint: "--dir <本地文件夹> [--extension pdf] [--limit N] [--copy]"
skills: ella-collect
---
Use the `ella-collect` skill for this request. 先按 skill 说明解析 `$PY`/`$CLI`，用**新** run id 执行 `ingest --dir`。

$ARGUMENTS

跑完汇报：status_counts（acquired / rejected / needs_review）、type_counts（这批都是什么格式）、产物路径、next_action。默认原地引用源文件（只读，不复制、不移动、不改名）；需要 run 目录自包含时才加 `--copy`。
