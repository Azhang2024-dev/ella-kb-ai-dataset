---
name: ella-report
description: "知识库交付汇总与只读诊断：生成覆盖/淘汰/配额缺口/成本报告与人读交付汇总表（每份文档一行：文件名/类型/主题/学科/语种/query 数），并回答『进行到哪了』『为什么这条被淘汰』『能不能交付』。当用户要『出报告/看汇总/看配额缺口/看进度/验收』时使用；面向整条流水线请用 document_collection 编排 skill。不负责导入（走 ella-collect）、筛选（走 ella-screen）、出题（走 ella-generate）。"
---

# ella-report：交付汇总与只读诊断

产出一份**人和机器都能读**的交付视图，并回答"现在什么状态""为什么是这个结果"
"能不能交付"。

## 触发词
报告、汇总、覆盖率、配额缺口、"采了多少"、"进行到哪了"、验收、门禁、为什么淘汰。

## 覆盖的操作（域内路由）

| 你要做的事 | 命令 | 说明 | 只读？ |
|-|-|-|-|
| 出覆盖/淘汰/配额/成本报告 | `$CLI report` | 含**人读交付汇总表** delivery_summary.csv/.md | 否（写产物） |
| 看阶段×状态矩阵 | `$CLI status` | 每步各有多少条、处于什么状态 | 否* |
| 查某条为什么是这个结果 | `$CLI explain --item-id <id>` | 完整判定链（每阶段 status + reason_codes + evidence） | 是 |
| 交付前验收门禁 | `$CLI audit` | 机械化 5 个验收检查点 PASS/FAIL | 否（写产物） |
| 不知道下一步跑什么 | `$CLI resume` | 给出 next_action | 否* |

\* `status`/`resume`/`audit` 会更新 run 的时间戳与 result 文件（`write_local` 副作用），
所以对**历史 run** 复核前必须先复制到临时工作区（见编排 SKILL.md §4 的 D23-2 说明）。

## 怎么跑

```bash
$PY $CLI --run-id '@latest' report          # 交付汇总（含 delivery_summary.csv/.md）
$PY $CLI --run-id '@latest' status          # 阶段×状态矩阵
$PY $CLI --run-id '@latest' explain --item-id <id>
$PY $CLI --run-id '@latest' audit           # 交付前必跑
```

## 本步要看的 summary 字段
- `report`：`accepted_total`、`language_quota_met`、`subject_quota_met`、`status_counts`
- **交付汇总表**：每份文档一行 —— 文件名 / 文件类型 / 主题 / 学科 / 语种 / 字符数 /
  Query 数 / 题型概览 / 状态。这就是"这批到底交付了什么"的一眼视图。
- `audit`：`passed`、`failed_checks`（任何一条 ❌ 都不能交付）

## 停止规则与安全纪律
本步拿不准是否该停下时，**引用编排 skill**：见编排 SKILL.md §6。
`audit` 不通过（退出码 1）时停下，按 failed 明细逐条修。禁止为了"看起来通过"而放宽
业务数字或伪造产物——业务数字一律以 `config/collection_policy.json` 为准。
