---
description: 只读盘点两个已授权云盘文件夹，生成候选清单（不下载正文）
argument-hint: "[--per-source-limit N 或 --include-subfolder DOC-EN 等，可留空全量盘点]"
skills: ella-collect
---
Use the `ella-collect` skill for this request. 先按 skill 说明解析 `$PY`/`$CLI`，确认环境已 onboarding，用**新** run id 执行。

$ARGUMENTS

跑完汇报：status、unique_items 总数、每个源的条数、产物路径、next_action。触发停止规则就停下问用户。
