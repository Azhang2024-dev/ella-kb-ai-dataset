---
name: ella-collect
description: "知识库语料准备：把一批文档（本地文件夹或已授权的云盘文件夹）收进来、抽成正文、跨源去重，产出干净可用的正文集合。当用户要『导入/收集/整理一批资料』『把 PPT/PDF/DOCX 读进来』『去重看看有没有重复』时使用；面向整条流水线请用 document_collection 编排 skill。不负责筛选合格性判定（走 ella-screen）、出题（走 ella-generate）、汇总报告（走 ella-report）。"
---

# ella-collect：语料准备（导入 → 抽取 → 去重）

把"一堆文档"变成"干净可用的正文集合"。这是本地优先链路的第一步，等价于云盘链路的
`inventory + acquire + extract + deduplicate`。

## 触发词
导入、收集、整理资料、读进来、抽取正文、解析文档、去重、重复文档。

## 覆盖的三个阶段（域内路由）

| 你要做的事 | 命令 | 说明 | 动手前读 |
|-|-|-|-|
| 本地已有下载好的文档 | `$CLI ingest --dir <文件夹>` | 直接登记，不碰云盘；默认原地引用源文件不复制 | 下方"入口选择" |
| 文档在飞书云盘里 | `$CLI inventory` 然后 `$CLI acquire` | 只读盘点+下载/导出；需 lark-cli 与云盘授权 | 编排 SKILL.md §0 |
| 把已登记的文件抽成正文 | `$CLI extract` | 七格式（pdf/docx/pptx/xlsx/csv/txt/md），统计字符数与语言 | — |
| 跨源去重 | `$CLI deduplicate` | SHA-256 抓完全相同 + 规范化正文指纹抓同内容不同容器 | — |

入口选择：**用户手上已经有文件 → 用 `ingest`**（不需要云盘授权，权限面最小）；
只有需要从云盘全量拉取时才走 `inventory`+`acquire`。两条入口在 extract 之后完全汇合。

## 怎么跑（先解析路径）

所有命令都是 `$PY $CLI [全局参数] <命令> [参数]`。先解析 `$PY`/`$CLI`（见编排 SKILL.md §2），
不确定进度先跑 `$CLI --run-id '@latest' status`。

```bash
# 本地优先入口（推荐）
$PY $CLI --run-id batch-1 ingest --dir "D:/资料/已下载"
$PY $CLI --run-id batch-1 --concurrency 8 extract
$PY $CLI --run-id batch-1 deduplicate
```

`--concurrency N` 只影响速度，不改变任何结论（抽取是逐条独立的）；本地文件 IO 受益明显，
推荐 4–8。

## 本步要看的 summary 字段
- `ingest`：`status_counts`（acquired / rejected / needs_review）、`type_counts`（这批都是什么格式）
- `extract`：`status_counts`（extracted / needs_review / extraction_failed）、`language_counts`、`total_chars`
- `deduplicate`：`duplicate_items`、`unique_items`、`near_duplicate_pairs`

## 下一步
三个阶段的 `next_action` 会依次指向 extract → deduplicate → `collection.screen`。

## 停止规则与安全纪律
本步拿不准是否该停下、能不能改数据时，**引用编排 skill**：
停止条件见编排 SKILL.md §6，禁止动作见 §7（尤其：禁止修改源文件、禁止伪造成功、
禁止在历史 run 上跑写操作）。业务数字（大小上限、允许格式）一律以
`config/collection_policy.json` 为准，本文件不复制。
