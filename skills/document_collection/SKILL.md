---
name: document_collection
description: 采集/导入文档并做质量筛选、生成评测 query 与参考题目。当用户要求盘点/采集/下载两个已授权云盘文件夹（知识库_2026、docQA.llm）里的学习资料，或要导入一个本地文件夹（"我手上有一批文档，帮我筛一遍并出题"）并抽取/去重/筛选/生题，或要为合格文档生成 10 条整篇文档级评测 query 与参考题目（题干/答案/解析），或要出采集覆盖与淘汰报告时使用。也用于首次安装（环境自检与依赖安装）、续跑中断的采集 run、复核 needs_review 队列、检查语种/学科配额缺口、编排整条流水线（半自动/全自动）。不负责：编辑云盘文档内容、飞书审批/消息/日历、与本采集流水线无关的通用文档处理。
---

# 知识库文档采集与筛选（编排 Skill）

## 0. Onboarding：首次安装（agent 可代做，授权除外）

用户把本 skill 目录（及各 `ella-*` 能力域目录）拷进平台 skills 根目录后，
对 agent 说"装一下环境"即可。按序自检自修，全部通过后报告「就绪」：

1. **解析仓库根**：读本目录 `runtime.json` 的 `repo` 字段，确认该路径下存在
   `scripts\collection_cli.py`。是 → 跳到第 4 步。
   文件不存在、路径失效或还是 `runtime.example.json` 的占位符 → 问用户要本机仓库根，
   更新**本目录与全部 `ella-*` 目录**的 `runtime.json`（5 份必须同值；
   只改 skills 侧副本，仓库零改动）。
2. **Python 环境**：`<repo>\.venv` 不存在时创建并装依赖（Python ≥ 3.12）：

   ```powershell
   # Windows (PowerShell)
   cd <repo>; python -m venv .venv; .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   ```bash
   # Linux / macOS
   cd <repo> && python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
   ```

3. **lark-cli ≥ 1.0.85**：`lark-cli --version` 检查；缺失时**提示用户自行**
   `npm install -g @larksuite/cli`（不代装）。云盘授权 `lark-cli auth login`
   必须用户亲自完成，agent 只提示到这一步。
4. **规则自检**：跑 `<repo>` 里的 `validate-config`（命令见 §2），
   退出码 0 → 报告「就绪」；退出码 4 → 策略文件自相矛盾，停下来告诉用户，不要改 policy。

换机器 / 换路径时重复本节即可；除 `runtime.json` 外不在 skills 目录写任何文件。

## 1. 这个 Skill 是什么形态

Skill 目录只放说明文件和 1 行配置，不放代码、不放策略副本、不落运行产物：

```text
<skills 根目录>/document_collection/      ← 本编排 skill：什么时候调哪个能力、什么时候必须停
  SKILL.md
  runtime.json                            ← 唯一配置：仓库根路径（1 行；模板见 runtime.example.json）
  capability-manifest.json                ← 能力清单（11 能力 + 3 诊断），机器可读
  examples/scenarios.md                   ← 7 个场景示例
<skills 根目录>/ella-collect/                ← 能力域 skill：导入+抽取+去重（推荐入口）
<skills 根目录>/ella-screen/                 ← 能力域 skill：筛选
<skills 根目录>/ella-generate/               ← 能力域 skill：生题
<skills 根目录>/ella-report/                 ← 能力域 skill：汇总与只读诊断
```

**为什么按"能力域"分 skill，而不是按流水线每一步分**：官方 skill 指南要求
"一个 skill = 一个连贯的工作单元"，过窄会导致一个任务要加载多个 skill、描述互相重叠。
`ella-collect`/`ella-screen`/`ella-generate`/`ella-report` 四个域正好对应"准备资料、筛、出题、
汇总"四件独立的事；每个域内部用路由表收敛该域的全部命令。

平台发现 Skill 的约定是 `<skills 根目录>/<skill 名>/SKILL.md`，一层，不能再深。
所以 skill 文件夹**不是项目仓库**：仓库里还有 `.venv`、`output`、`core`、`tests`、`docs`，
整个拷进 skills 目录只会把 SKILL.md 埋进深层、把几百 MB 环境塞进 skill 目录。

真正干活的是仓库里的 Core（`core\document_collection\`），业务规则唯一事实来源是
`config\collection_policy.json`。本 Skill 只负责告诉你什么时候调哪个能力、什么时候必须停。
不要在对话里手写筛选逻辑、不要自己拼 lark-cli 命令、不要绕过 CLI 直接改 SQLite。

流水线固定：

```text
盘点 -> 只读采集 -> 抽取正文 -> 跨源去重 -> 硬规则筛选 -> (可选)语义复核 -> 生成 query -> 生成参考题目 -> 交付（选目标集+真实名+交付表） -> 报告
```

## 2. 调用入口（先解析路径，再跑命令）

每次执行前：

1. 按 §0 第 1 条解析并确认 `repo`（仓库根）真实存在；
2. 推导执行体：Windows 下 `$PY = "<repo>\.venv\Scripts\python.exe"`、
   `$CLI = "<repo>\scripts\collection_cli.py"`；类 Unix 下把 `.venv\Scripts` 换成 `.venv/bin`；
3. 所有命令都是 `$PY $CLI [全局参数] <命令> [参数]`。CLI 按自身位置解析仓库根，
   在任何 cwd 下都能跑，**不需要先 cd**；要把产物写到别处用 `--workspace <目录>`。
4. 上游提供 `collection.*` MCP 工具时优先用 MCP（能力名、参数、返回结构见
   capability-manifest.json，`future_mcp_tool` 字段就是映射表）；没有 MCP 就退回 CLI。

先跑这两条确认环境和规则：

```powershell
# PowerShell
& $PY $CLI --help
& $PY $CLI validate-config
```

```bash
# bash
"$PY" "$CLI" --help
"$PY" "$CLI" validate-config
```

## 3. 编排职责：半自动 / 全自动

用户说"跑流水线 / 采一批 / 继续跑"时，由本 skill 编排。**编排知识在这里，业务在各 `ella-*` 域 skill**：
按 `ella-collect`（准备资料）→ `ella-screen`（筛）→ `ella-generate`（出题）→ `ella-report`（汇总）
的顺序调域 skill 覆盖的阶段，两种模式：

- **半自动（默认）**：逐步执行，每步跑完用 `status` 把矩阵汇报给用户，在这些 gate 点停下等放行：
  `screen` 之后（看 needs_review 队列）、`query` 之后（看校验报告）、交付前（必跑 `audit`）。
- **全自动**：无 gate 连跑（等价 `run-all`），只在触发 §6 停止规则时停。
  用户明确说"全自动/不要停"才用；触发停止规则时必须停下，全自动不豁免停止规则。

> 说明：skill 之间**不能互相调用**（官方机制如此），编排只能靠描述由模型逐步执行。
> 需要确定性的一条龙时，用 CLI 的 `run-all`（顺序写死在代码里）而不是指挥 skill 互调。

不知道进行到哪一步时，先跑 `status`（阶段×状态矩阵）或 `resume`（下一步建议）：

```powershell
# PowerShell
& $PY $CLI --run-id '@latest' --json status
& $PY $CLI --run-id '@latest' --json resume
```

```bash
# bash
"$PY" "$CLI" --run-id '@latest' --json status
"$PY" "$CLI" --run-id '@latest' --json resume
```

（PowerShell 里 `@latest` **必须加引号**，否则被当展开符。）

## 4. 命令与能力对照

| 命令 | 能力名 | 什么时候用 |
|-|-|-|
| `ingest --dir <本地文件夹>` | `collection.ingest` | **本地优先入口（推荐）**：直接登记已下载好的文档，不碰云盘 |
| `inventory` | `collection.inventory` | 云盘链路第一步；只列举不下载正文 |
| `acquire` | `collection.acquire` | 云盘链路：下载普通文件、导出在线资源 |
| `extract` | `collection.extract` | 识别真实格式并抽取正文 |
| `deduplicate` | `collection.deduplicate` | 跨两个来源统一去重 |
| `screen` | `collection.screen` | 硬规则筛选，`--semantic` 才启用模型复核 |
| `query` | `collection.generate_queries` | 只给 accepted 文档生成 10 条 query；`--item-id` 定向 |
| `questions` | `collection.generate_questions` | 为已验证 query 的文档生成参考题目（题干/答案/解析）；`--item-id` 定向 |
| `deliver` | `collection.deliver` | **交付**：按语种/学科选目标集 + 真实文件名落地 + 出交付表（一行一条 query）；`--per-language` 定量、`--item-id` 定向 |
| `release` | `collection.release` | **人工放行**：把人工复核确认合格的 `needs_review` 条目改判为 accepted；只允许 `needs_review→accepted`，逐条复核硬约束并以追加方式落库。需人工指定 `--item-id`，先 `--dry-run` 看判定 |
| `material-type` | `collection.material_type` | **资料类型标注**：给交付表「类型」列判 教材/课堂笔记/试卷/讲义/作业习题。只读抽取文本头部（不重抽不读全文），规则先行、模型兜底（`--llm`），判不出的留空并标 needs_review |
| `reconcile` | —（一致性修复） | 把 `query_records.jsonl` 回灌状态库（jsonl 与库错位时用） |
| `report` | `collection.report` | 出覆盖、淘汰、配额缺口、成本报告 + 交付汇总表 |
| `resume` | `collection.resume` | 不知道进行到哪一步时先跑这个 |
| `status` | —（只读诊断） | 阶段×状态矩阵：每步各有多少条、处于什么状态 |
| `explain` | —（只读诊断） | 查一个条目为什么被判成这个结果 |
| `audit` | —（只读诊断） | 交付前必跑：机械化验证 5 个验收检查点 |
| `runs` | — | 列出工作区里已有的 run |
| `validate-config` | — | 改过策略文件后必须先跑 |

**两条入口，共用后半段流水线**（extract 之后完全相同）：

```text
本地链路（推荐）：ingest --dir <文件夹> → extract → deduplicate → screen → query → questions → deliver → report
云盘链路（可选）：inventory → acquire   → extract → deduplicate → screen → query → questions → deliver → report
```

用户说"我手上有一批下载好的文档"时走本地链路；只有需要从云盘全量拉取时才走云盘链路。
**任何一条阶段命令都可加 `--concurrency N` 提速**（抽取、语义复核、生题、出题是逐条独立的，
并发不改变任何业务结论；默认 1 串行）。

执行顺序是固定的：**inventory → acquire → extract → deduplicate → screen → query → questions → deliver → report**。
`screen` 依赖 `deduplicate` 的结果，不能颠倒；`questions` 依赖 `query` 的 validated 产物，不能颠倒。

**交付前必须跑 `audit`**，它把 5 个验收检查点变成可重复执行的断言：

```powershell
# PowerShell
& $PY $CLI --run-id '@latest' audit
& $PY $CLI --run-id '@latest' audit --compare-with "$repo\output\document_collection\<另一次运行>"
```

```bash
# bash
"$PY" "$CLI" --run-id '@latest' audit
"$PY" "$CLI" --run-id '@latest' audit --compare-with "$repo/output/document_collection/<另一次运行>"
```

> ⚠️ **`audit` / `resume` / `status` / 任何阶段命令都会写运行目录**（`audit_report.*`、
> `result_*.json`、`state.sqlite3` 的 runs 表）。要复核一个**历史 run**，先把它复制到临时
> 工作区，再用 `--workspace` 指过去，不要在原目录上跑（这条踩过两次，见开发日志 D23-2）：
>
> ```powershell
> Copy-Item -Recurse "$repo\output\document_collection\<历史 run>" "<临时工作区>\<历史 run>"
> & $PY $CLI --workspace "<临时工作区>" --run-id <历史 run> audit
> ```

任何一条 ❌ 都不能交付。检查点含义：

| 检查点 | 验什么 |
|-|-|
| 1 能运行 | manifest 与契约逐项一致；适配器协议不含任何写方法 |
| 2 盘点完整 | 无分页漏项风险；两个源都用真实名称记录；每条都有源名 |
| 3 没有误放行 | 超大小 / 不支持格式 / 硬拒绝被翻案 / 同组多条被接受 / 相同 SHA-256，六项必须为 0 |
| 4 结果可解释 | 每条结论都有 status + reason_codes + source_path + artifact_path；每条淘汰有单一决定原因 |
| 5 可以复跑 | 与另一次运行对比：下载类产物哈希、条目计数、确定性阶段决策必须逐条一致 |

检查点 5 有个必须知道的口径：**在线资源导出产物不具备字节级可复现性**
（`drive +export` 每次由服务端重新渲染，字节数会差几个 byte），
audit 会披露差异但不判失败；下载类文件则必须逐字节一致。

## 5. 续跑同一个 run

所有阶段共享同一个 SQLite 状态，靠 `--run-id` 关联。`@latest` 表示复用最近一次运行：

```powershell
# PowerShell
& $PY $CLI --run-id pilot-20260909 inventory
& $PY $CLI --run-id pilot-20260909 acquire --limit 60
& $PY $CLI --run-id '@latest' extract
```

```bash
# bash
"$PY" "$CLI" --run-id pilot-20260909 inventory
"$PY" "$CLI" --run-id pilot-20260909 acquire --limit 60
"$PY" "$CLI" --run-id '@latest' extract
```

重复执行同一阶段是安全的（按 item_token / item_id upsert，不会翻倍）。

但「安全」只对**你自己新建的 run** 成立。`output\document_collection\` 下的历史 run 是证据
（例如 `pilot-20260909` 是缺陷 D5 的对照，盘点只有 3,478 条；用修好的代码重跑会变成 4,892 条，
证据就没了）。要复核历史 run，按 §4 的做法复制到临时工作区再跑。

## 6. 必须停下来的情况

| 信号 | 动作 |
|-|-|
| `status=needs_review` 且 `review_items` 非空 | 停下，把 `human_review_queue.csv` 给人看，不要自行放行 |
| `pending_export` 出现 | 停下，确认在线资源导出格式后再重试，不要改判为成功 |
| `conversion_required` 出现 | 停下，本期没有批准的 doc/ppt/xls 转换器，不要自己找工具转 |
| `mime_extension_mismatch` | 停下，不要按扩展名硬抽 |
| `content_audit_rejected` | 网关内容审核拦截（`inappropriate content`）：**不要重跑**（必复现），处置序 换源→剔除→必要时换模型/网关；如实落 needs_review，不要混入"格式无效" |
| `llm_budget_exhausted` | 推理模型把 max_tokens 预算耗在思考上、正文为空：**调大 `policy.llm.max_tokens`**（实测 16000→32000 有效），不要改提示词、不要重试原参数 |
| `acquire_failed` 比例明显偏高 | 停下，先查 lark-cli 授权状态（`lark-cli auth status`），不要盲目重跑 |
| 模型不可用但策略要求语义复核 | 停下，语义项一律 needs_review，绝不自动 accepted |
| 退出码 4 | 策略文件自相矛盾，先修 `config/collection_policy.json` |

退出码：`0` 完成 / `2` 部分完成 / `3` 需人工复核 / `1` 失败 / `4` 配置错误。

**LLM 网关错误的处置口径**（判据一律看 `retryable` 字段与 `reason_codes`，不要看错误文案猜）：

- **429 限流**：`llm.py` 已内建退避（5/10/20s × 3 次仍失败才落账）。退避后仍失败，**降并发重跑**
  （`--concurrency 8→4→2`），禁止加并发硬撞；实测 180+178 份并发 8 全程未出现 429。
- **5xx / timeout**：同走退避路径；3 次后单条落 `needs_review`/`acquire_failed` 且批次继续（设计如此），
  人工整批重跑补漏即可。
- **`inappropriate content`（内容审核）**：`retryable=false`，重跑必复现，**不要重跑**；
  该类已单列 `content_audit_rejected`，与"格式无效"（`llm_output_invalid`）区分。
- **预算耗尽**（`llm_budget_exhausted`）：调大 max_tokens，不改提示词。

## 7. 禁止动作

1. **禁止修改云盘源**：不删除、不移动、不重命名、不改标题、不上传、不建文件夹、不加快捷方式。
   适配器层有只读白名单，白名单外的命令会直接抛 `SafetyViolation`；不要试图绕过。
2. **禁止放宽大小上限**：PDF/DOC/DOCX/PPTX ≤ 100 MB，XLSX/CSV/TXT/MD ≤ 20 MB。
   不能用压缩、拆分、截取规避；`--keep-oversized` 只是保留证据，不改变拒绝结论。
3. **禁止让模型覆盖硬失败**：格式、大小、图形主导、精确重复由程序判定，模型无权改判。
4. **禁止伪造成功**：抽取不到正文、导出失败、模型不可用，都要如实落成
   `needs_review` / `pending_export` / `conversion_required` / `failed`。
5. **禁止在 query 文案里写定位**：不能出现片段、章节、页码、Sheet、表格。
   定位信息只写进 `query_evidence.jsonl` 的内部台账。
6. **禁止改动业务数字**：题型条数、题量、语种配额、学科清单都在
   `config/collection_policy.json`，改完必须重跑 `validate-config`。
7. **禁止把输出里的凭据外传**：日志与录像已做脱敏，不要再手工粘贴 token。
8. **禁止在历史 run 目录上跑写操作**：`audit` / `resume` / 阶段命令都会写运行目录与 `state.sqlite3`。
   复核历史 run 必须先复制到临时工作区，再用 `--workspace` 指过去。
9. **禁止把仓库拷进 skills 目录**：skill 文件夹只放说明文件与 `runtime.json`，
   不复制 `core`、`config`、`.venv`、`output`；`config\collection_policy.json` 只能有一份。
10. **禁止绕过 CLI 自写重跑脚本**：定向"只补失败项"用 `query`/`questions` 的 `--item-id`，
    不要写一次性脚本重写 jsonl —— 那会漏掉状态库写入（上一批踩过：jsonl 178 / 库 172）。
    万一同写被绕过，用 `reconcile` 从 `query_records.jsonl` 回灌状态库，不要手工改 SQLite。

## 8. 输出契约

每个命令都能加 `--json` 输出统一结构：

```json
{
  "status": "completed | partial | needs_review | failed | cancelled",
  "run_id": "...",
  "capability": "collection.screen",
  "contract_version": "0.1",
  "summary": {},
  "artifacts": [{"path": "...", "kind": "..."}],
  "review_items": [{"item_id": "...", "source_path": "...", "reason_codes": []}],
  "errors": [{"stage": "...", "message": "..."}],
  "next_action": "collection.generate_queries"
}
```

产物都落在 `$repo\output\document_collection\<run_id>\`（可用 `--workspace` 改到别处）：

```text
run_manifest.json              盘点元信息与需求 revision
source_inventory.jsonl         合并去重后的唯一资源
source_inventory_raw.jsonl     去重前逐条记录
source_type_counts.csv         每个源文件夹 x 文件类型 数量表
inventory_summary.md           盘点摘要
acquisition_report.jsonl       采集逐条结果
artifacts/<sha256[:2]>/...     内容寻址的原始文件
extracted/<item_id>.txt        抽取出的正文
segments/<item_id>.jsonl       带定位的正文分段
extraction_report.jsonl        抽取逐条结果
dedup_report.jsonl             去重逐条结果
dedup_groups.csv               重复分组与推荐保留版本
screening_report.jsonl         筛选逐条结果与证据
accepted_documents.csv         合格
rejected_documents.csv         淘汰（带原因）
human_review_queue.csv         待人工复核
query_records.jsonl            每份文档的 10 条 query
query_evidence.jsonl           query 的内部核验依据
query_validation_report.md     query 校验结论
questions/<item_id>.json       每份文档的参考题目（题干/选项/答案/解析）
question_records.jsonl         参考题目汇总（一行一份文档）
question_summary.json          参考题目生成结果
delivery_files/<语种>/...      交付用真实文件名（硬链接指向内容寻址仓，非哈希名）
selected.csv                   交付选取清单（语种/学科/类型/真实文件名/来源）
交付表_Query.xlsx              交付表：一行一条 query（列同在线 sheet4）
coverage_report.md             覆盖、淘汰、配额缺口
cost_and_runtime.json          成本与耗时
all_decisions.csv              全量决定汇总
state.sqlite3                  唯一状态源
```

## 9. 关键业务口径（改之前先确认）

- P0 语种：en / ru / fr / zh / ar / es，每种至少 20 份。
- P1 学科：法学、教育学/语言学、临床医学/护理、基础理科，每类至少 1 份。
- **学科是硬条件（范围门禁）**：文档学科必须属于 P0∪P1 八类之一；
  判为范围外（心理学/社会学/政治学/艺术学/文学/历史学/哲学/机械工程…）→ 淘汰；
  模型判不出（unknown）→ 待人工复核。学科以**模型判定优先**（`screen --semantic`），
  关键词兜底只在模型没给结论时补位。开关在 `subjects.gate`。
- **语种以正文实检为准**（抽取阶段的 language），不按目录名——目录放错不影响判定。
- 每份文档 10 条 query：选择题 2（题量 5）、填空题 2（3）、解答题 2（2）、判断题 2（7）、混合题型 2（9，标记 `compatibility_only`）。
- 每条 query 题量必须 < 12，同文档内不同题型题量尽量不重复。
- **query 句式要多样**：模拟真实用户出题的口吻，同一份文档 10 条 query 的开头句式不能雷同
  （同一开场套路最多 3 次，超了判 `query_style_repeated` 并自动带提示重生成一次）。
- 参考题目（`questions`）为每条 query 实际写出题干/选项/答案/解析，语言跟随文档，答案须有原文依据。
- 超过 5 万字不淘汰，标记 `requires_full_text_review` 走全文理解。
- query 与出题的输入窗口同一个参数（`content_rules.prompt_text_chars`，默认 5 万字符），
  与端侧/云侧入库窗口对齐；截断为头 50% + 中 25% + 尾 25% 抽样，截断份数会写进产物。
- 文字、表格、公式可保留；示意图/插图/电路图/受力图等图形主导内容本期排除。
- 两个文件夹必须合并后统一去重计数，不能各算各的。
- **交付（`deliver`）**：每语种定量（默认 30），产出真实文件名目录 + `交付表_Query.xlsx`
  （一行一条 query，列同在线 sheet4：语种/文件名/格式/字符数/item_id/类型/学科/离线文件/query/query_type）。
- 以上数字都以 `config/collection_policy.json` 为准，本文件只是速查。

## 10. 参考

- 能力与参数明细：本 Skill 目录下的 `capability-manifest.json`
- 业务规则与阈值：`$repo\config\collection_policy.json`
- 范围与接口事实：`$repo\docs\document_collection_scope.md`
- 未来 MCP 适配：`$repo\mcp_server\README.md`
- 未来 Plugin 边界：`$repo\plugins\README.md`
- 场景示例：本 Skill 目录下的 `examples\scenarios.md`
- 开发过程与缺陷留档：`$repo\docs\document_collection\开发日志.md`
