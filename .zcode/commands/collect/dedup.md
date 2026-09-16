---
description: 跨两个来源统一去重（SHA-256 + 正文指纹）
argument-hint: "[--tolerance 0.02 或 --keep-duplicates，可留空默认]"
skills: ella-collect
---
Use the `ella-collect` skill for this request. 先按 skill 说明解析 `$PY`/`$CLI`，确认同 run 已完成 extract，再执行。

$ARGUMENTS

跑完汇报：unique / duplicate 计数、重复分组数、dedup_groups.csv 路径。两源必须合并计数，不能各算各的。
