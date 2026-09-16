---
description: 生成覆盖与淘汰报告（配额缺口 / 格式分布 / 成本耗时）
argument-hint: "[无参数]"
skills: ella-report
---
Use the `ella-report` skill for this request. 先按 skill 说明解析 `$PY`/`$CLI`，再执行。

$ARGUMENTS

跑完汇报：P0 语种配额缺口表（current/target）、格式分布 vs 软目标、淘汰原因 Top、`delivery_summary.csv`（文件名/类型/学科/语种/query 数）与 coverage_report.md 路径。缺口大时建议按语种目录定向批次（见 ella-collect skill）。
