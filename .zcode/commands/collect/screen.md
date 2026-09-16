---
description: 硬规则筛选 + 可选语义复核（--semantic 需模型凭据），screen 后是人工 gate 点
argument-hint: "[--semantic | --no-semantic]"
skills: ella-screen
---
Use the `ella-screen` skill for this request. 先按 skill 说明解析 `$PY`/`$CLI`，确认同 run 已完成 deduplicate，再执行。

$ARGUMENTS

跑完汇报：accepted / rejected / needs_review 计数与三张 CSV 路径。**screen 是 gate 点**：needs_review 队列非空时必须停下把 human_review_queue.csv 交给用户，不得自行放行。
