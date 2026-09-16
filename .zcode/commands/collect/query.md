---
description: 为 accepted 合格文档生成 10 条评测 query 并校验（需模型）
argument-hint: "[--limit N，可留空处理全部 accepted]"
skills: ella-generate
---
Use the `ella-generate` skill for this request. 先按 skill 说明解析 `$PY`/`$CLI`，确认同 run 有 accepted 文档且模型凭据已配置，再执行（需要 `--llm`）。

$ARGUMENTS

跑完汇报：生成条数 / validated 数、query_validation_report.md 结论。**query 后是 gate 点**：校验未全绿时停下汇报失败原因，不得手工改文件凑数。校验通过后，参考题目（题干/答案/解析）走 `/collect:questions`。
