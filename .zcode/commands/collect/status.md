---
description: 查看采集流水线当前状态：阶段×状态矩阵 + 下一步建议
argument-hint: "[run-id，默认 @latest]"
skills: ella-report
---
Use the `ella-report` skill for this request. 先按 skill 说明解析 `$PY`/`$CLI` 与 run id，再执行并汇报。

$ARGUMENTS

汇报格式：用表格呈现 items / artifacts / extractions / screen / queries 各状态计数与 languages 分布，末尾给出 `next_action` 与一句建议。发现 needs_review / pending_export / conversion_required 计数非零时，按编排 skill §6 停止规则说明，不要自行处理。
