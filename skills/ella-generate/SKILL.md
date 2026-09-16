---
name: ella-generate
description: "知识库 AI 生题：为合格文档生成整篇文档级评测 query（含题型与题量配比）并做双重校验，再据 query 生成参考题目（题干/选项/答案/解析）。当用户要『出题/生题/生成 query/生成题目』『给这些文档配题』『定向给某几篇出题』时使用；面向整条流水线请用 document_collection 编排 skill。前置：本 run 已有 accepted 文档（ella-screen 已跑），出题还需先跑 query。必须有可用模型才能产出，模型不可用时如实标 needs_review，不伪造。不负责导入（走 ella-collect）、筛选（走 ella-screen）、汇总（走 ella-report）。"
---

# ella-generate：生成评测 query 与参考题目

为合格文档生成整篇文档级的评测 query，并按题型/题量配比与多语种定位词黑名单做双重校验；
再据已验证的 query 生成**参考题目**（题干/选项/答案/解析）。
**必须有可用模型**才能产出；模型不可用时如实转 needs_review，绝不伪造结果。

## 触发词
出题、生题、生成 query、生成题目、配题、"给这些文档出题"、定向出题。

## 覆盖的阶段

| 你要做的事 | 命令 | 说明 | 动手前读 |
|-|-|-|-|
| 给全部 accepted 文档出 query | `$CLI query --llm` | 每份文档按 policy 的题型/题量生成 | 编排 SKILL.md §0 环境段 |
| **只给指定几篇出 query（定向批次）** | `$CLI query --llm --item-id <id>` | 可重复；或用 `--item-id-file 白名单.txt`（每行一个，# 注释） | 下方"定向批次" |
| 生成参考题目（题干/答案/解析） | `$CLI questions --llm` | 消费 query 阶段的 validated 文档；同样支持 `--item-id` 定向 | 下方"参考题目" |

## 怎么跑

```bash
# 全部 accepted：先生成 query
$PY $CLI --run-id '@latest' --concurrency 4 query --llm

# 再据 query 生成参考题目（题干/选项/答案/解析）
$PY $CLI --run-id '@latest' --concurrency 4 questions --llm

# 定向：只给这 3 篇出 query（避免重跑全量）
$PY $CLI --run-id '@latest' --concurrency 4 query --llm \
  --item-id id-aaa --item-id id-bbb --item-id id-ccc

# 定向：从白名单文件读（每行一个 item_id）
$PY $CLI --run-id '@latest' query --llm --item-id-file selected.txt
$PY $CLI --run-id '@latest' questions --llm --item-id-file selected.txt
```

**定向批次是本 skill 的关键能力**：`--item-id` 让你只处理指定文档，不必为了"这 30 篇"
重跑全量。item_id 可从 `accepted_documents.csv` 或 `status` 取。

生题与出题几乎全部耗时在模型往返，`--concurrency 4~8` 提速明显且不改变结论。

## 本步要看的 summary 字段
- `total_documents` / `total_queries`：处理了几篇、产出几条
- `status_counts`：validated / needs_review
- `error_codes`：失败原因分布（如 `llm_budget_exhausted` = 推理吃光预算，需调大
  `policy.llm.max_tokens` 后重跑，**不是**改提示词；`content_audit_rejected` = 网关内容审核，
  **不要重跑**，换源）
- 产物：`query_records.jsonl`（每篇的 query）、`query_evidence.jsonl`（内部定位台账）、
  `query_validation_report.md`
- 参考题目：`questions/<item_id>.json`、`question_records.jsonl`、`question_summary.json`

## 硬约束
- query 语言跟随文档语言，且文案里**不得**出现片段/章节/页码/Sheet/表格等定位词；
- 定位信息只写进 `query_evidence.jsonl` 的内部台账；
- 参考题目语言同样跟随文档，答案必须能在原文找到依据，不得编造；
- 题型与题量配比、题量上限一律以 `config/collection_policy.json` 为准。

## 停止规则与安全纪律
本步拿不准是否该停下时，**引用编排 skill**：见编排 SKILL.md §6。
模型不可用但策略要求生题时停下；`needs_review` 非空时把失败原因交给用户。
**重跑只补失败项时必须走 CLI 的 `--item-id`，不要自写脚本重写 jsonl** ——
那会漏掉状态库写入；万一同写被绕过，用 `reconcile` 从 `query_records.jsonl` 回灌。
