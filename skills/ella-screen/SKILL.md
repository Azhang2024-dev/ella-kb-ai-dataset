---
name: ella-screen
description: "知识库文档筛选：硬规则筛选（格式/大小/语种/图形主导/精确重复）+ 可选语义复核（教育属性/学科/内容质量，需模型）。当用户要『筛一下哪些能用』『挑合格文档』『复核这批资料』『按语种/学科过滤』时使用；面向整条流水线请用 document_collection 编排 skill。前置：本 run 已完成 ella-collect（extract + deduplicate）。不负责导入（走 ella-collect）、出题（走 ella-generate）。"
---

# ella-screen：筛选（硬规则 + 可选语义复核）

从"干净正文集合"里挑出**合格文档**，每条结论都带 reason_codes 与 evidence。

## 触发词
筛选、复核、挑合格、"哪些能用"、"哪些进评测"、按语种/学科过滤。

## 覆盖的阶段

| 你要做的事 | 命令 | 说明 | 动手前读 |
|-|-|-|-|
| 只要硬规则（快，不用模型） | `$CLI screen` | 格式/大小/语种/图形主导/精确重复 | — |
| 硬规则 + 语义复核 | `$CLI screen --semantic` | 额外判教育属性/学科/内容质量；需模型可用 | 编排 SKILL.md §0 环境段 |
| 强制不用模型 | `$CLI screen --no-semantic` | 显式关掉策略里的语义开关 | — |

## 怎么跑

```bash
$PY $CLI --run-id '@latest' screen            # 仅硬规则
$PY $CLI --run-id '@latest' screen --semantic # 含模型复核
```

**顺序是硬约束**：screen 依赖 deduplicate 的结果，不能颠倒在 extract 之前。
语义复核是逐条独立模型调用，可加 `--concurrency 4` 提速且不改变结论。

## 本步要看的 summary 字段
- `status_counts`：accepted / rejected / needs_review 各多少
- `accepted_by_language` / `accepted_by_subject`：配额进度
- `reason_counts`：淘汰原因分布
- 三张产物：`accepted_documents.csv` / `rejected_documents.csv` / `human_review_queue.csv`

## 硬约束（不能让步）
- 硬失败（格式/大小/图形主导/精确重复）**模型无权改判**；
- 没有 evidence 的模型判断**不得自动 accepted**，转 needs_review；
- 语种置信度不足时既不放行也不淘汰，转人工。

## 停止规则与安全纪律
本步拿不准是否该停下时，**引用编排 skill**：见编排 SKILL.md §6。
特别地，`needs_review` 队列非空时**必须停下**把 `human_review_queue.csv` 交给用户，
不得自行放行。业务数字一律以 `config/collection_policy.json` 为准（本文件不复制）。
