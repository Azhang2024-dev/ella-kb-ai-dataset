---
description: 为已验证 query 的文档生成参考题目（题干/选项/答案/解析，需模型）
argument-hint: "[--limit N 或 --item-id <id>，可留空处理全部 validated 文档]"
skills: ella-generate
---

Use the `ella-generate` skill for this request. 先按 skill 说明解析 `$PY`/`$CLI`，确认同 run 已跑过 `query`（产出 `query_records.jsonl` 且有 validated 文档）且模型凭据已配置，再执行（需要 `--llm`）。

$ARGUMENTS

跑完汇报：`total_documents` / `total_questions`、`status_counts`（ok/needs_review）、失败原因分布；产物 `questions/<item_id>.json`、`question_records.jsonl`、`question_summary.json` 路径。定向重跑只补失败项时用 `--item-id` / `--item-id-file`，不要自写脚本重写 jsonl（会漏写状态库；万一同写被绕过，用 `reconcile` 回灌）。
