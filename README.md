# Ella 知识库 AI 生题评测集 · 文档采集与筛选工具

从两个已授权的飞书云盘文件夹里**只读**地拉取候选文档，按业务硬规则（可选叠加语义复核）筛出合格文档，
再为每份合格文档生成 10 条评测 query，并据此写出参考题目（含答案与解析）。全流程可断点续跑、可复跑、
每条结论都带证据。

> 2026-09-10：本项目从 `Ella知识库数据集小工具`（另一个项目）整体迁入，成为独立仓库。
> 迁移清单、路径改写、验证结果与原仓库清理动作见 [docs/迁移记录_20260910.md](docs/迁移记录_20260910.md)。

---

## 1. 当前状态

| 项 | 状态 |
|-|-|
| CLI + Core + Skill（两周 MVP 三件必做） | 已实现、已验证 |
| Skill 形态（1 编排 + 4 能力域 skill，可发行包） | 已落地并部署到 `%USERPROFILE%\.agents\skills\`（5 个目录）；安装器已按裁定裁撤（见开发日志 D23-1），安装动作 = 编排 SKILL.md §0 Onboarding 由 agent 代做 |
| 入口形态 | **本地优先**：`ingest --dir` 直接吃本地文件夹（推荐，免云盘授权）；云盘只读链路保留为可选 |
| MCP Server / Plugin / Agent 编排 | 只留边界文档与接口预留，未实现（按计划不做） |
| 离线测试 | **405 项 pytest 全绿**；ruff `F,E9` 清零 |
| 验收门禁 `audit` | 真实运行 **19/19 PASS**（迁移后已在新仓库重跑复现）；新环境六语种冒烟 **14/14 PASS** |
| 真实六语种批次（`p0-langs-20260913`） | 六语种各 30 篇入选（共 180）；query **178/180** validated（1780 条）；参考题目 **178 × 52 = 9256 题**。目录导览见该 run 目录下 `README.md` |
| 真实云盘盘点 | 两源合计 **4,892** 条，与在线计划 1.2 节表格逐格一致 |
| 真实 pilot（80 条分层样本） | 采集 70 / 抽取 70 / 去重后 69 / 语义合格 6 / **60 条 query 全部通过校验** |
| 离交付还差什么 | 教材形态口径待与需求方对齐（六语种入选以单篇论文/讲义为主，见 §6） |

## 2. 目录结构

| 路径 | 作用 |
|-|-|
| `core/document_collection/` | 全部业务实现：契约、配置、状态库、盘点、采集、抽取、去重、筛选、生题、出题、报告、门禁自检 |
| `core/document_collection/adapters/` | 数据源适配层：`lark_cli`（飞书云盘，只读）、`local_files`、`recorded`（录制回放） |
| `core/document_collection/extractors/` | 格式抽取：pdf / docx / pptx / xlsx / text |
| `config/collection_policy.json` | **业务规则唯一事实来源**（题型题量、大小上限、语种配额、只读白名单） |
| `config/llm_config.json` | 模型凭据（本地文件，已被 `.gitignore` 排除，不入库） |
| `config/llm_config.example.json` | 凭据模板：发给同事后照抄一份、填自己的 Key |
| `scripts/collection_cli.py` | CLI 启动器（实现在包内，这里只加 `sys.path` 并转发退出码） |
| `skills/document_collection/` | 编排 skill：`SKILL.md`（含 §0 Onboarding）+ `runtime.json`（1 行仓库根，模板 `runtime.example.json`）+ 能力清单 + 场景示例（拷到平台 skills 根即用） |
| `skills/ella-collect/` `ella-screen/` `ella-generate/` `ella-report/` | 能力域 skill（4 个）：分别是"导入+抽取+去重""筛选""生题""汇总+只读诊断"，每个域内部用路由表收敛该域的全部命令 |
| `tests/document_collection/` | 18 个测试文件 / 405 项用例 + 真实格式夹具 |
| `docs/` | 需求与计划、开发日志、基线与范围、结果与验证、迁移记录 |
| `output/document_collection/<run_id>/` | 每次运行的产物：清单、样本、抽取正文、报告、状态库 |
| `mcp_server/` `plugins/` | 未来 MCP / Plugin 的边界说明（**只有 README，无实现**） |

## 3. 快速开始

### 3.1 本地优先链路（推荐：手上已有一批文档）

不需要云盘授权、不需要 lark-cli，权限面最小：

```bash
# 1) 导入一个本地文件夹（递归；默认原地引用源文件，不复制不改动）
.venv/Scripts/python.exe scripts/collection_cli.py --run-id batch-1 ingest --dir "D:/资料/已下载"
# 2) 抽取 -> 去重（--concurrency 提速，不改变结论）
.venv/Scripts/python.exe scripts/collection_cli.py --run-id batch-1 --concurrency 8 extract
.venv/Scripts/python.exe scripts/collection_cli.py --run-id batch-1 deduplicate
# 3) 筛选（--semantic 需模型）
.venv/Scripts/python.exe scripts/collection_cli.py --run-id batch-1 screen --semantic
# 4) 定向生题（只给指定几篇出题；不传 --item-id 则全部 accepted）
.venv/Scripts/python.exe scripts/collection_cli.py --run-id batch-1 --concurrency 4 query --llm
# 5) 据 query 写参考题目（题干/选项/答案/解析）
.venv/Scripts/python.exe scripts/collection_cli.py --run-id batch-1 --concurrency 4 questions --llm
# 6) 交付：选目标集 + 真实文件名落地 + 出交付表（一行一条 query）
.venv/Scripts/python.exe scripts/collection_cli.py --run-id batch-1 deliver --per-language 30
# 7) 交付汇总 + 门禁
.venv/Scripts/python.exe scripts/collection_cli.py --run-id batch-1 report
.venv/Scripts/python.exe scripts/collection_cli.py --run-id batch-1 audit
```

### 3.2 云盘链路（可选：需要从飞书云盘全量拉取）

```powershell
cd D:\Users\admin\Desktop\My_Project\Ella知识库AI生题数据集

# 首次：建环境（已建好可跳过）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 规则自检（不联网、不下载）
.venv\Scripts\python.exe scripts\collection_cli.py validate-config

# 盘点两个云盘文件夹（只读，约 75 秒）
.venv\Scripts\python.exe scripts\collection_cli.py --run-id <run> inventory

# 分层抽样采集（同 seed 结果可复现）
.venv\Scripts\python.exe scripts\collection_cli.py --run-id <run> acquire --sample 80 --sample-seed 20260909

# 抽取 -> 去重 -> 筛选（加 --semantic 才调用模型；两条链路此后完全共用）
.venv\Scripts\python.exe scripts\collection_cli.py --run-id <run> extract --concurrency 8
.venv\Scripts\python.exe scripts\collection_cli.py --run-id <run> deduplicate
.venv\Scripts\python.exe scripts\collection_cli.py --run-id <run> screen --semantic

# 生题 -> 交付 -> 报告 -> 交付前门禁
.venv\Scripts\python.exe scripts\collection_cli.py --run-id <run> query --limit 10 --llm
.venv\Scripts\python.exe scripts\collection_cli.py --run-id <run> questions --llm
.venv\Scripts\python.exe scripts\collection_cli.py --run-id <run> deliver --per-language 30
.venv\Scripts\python.exe scripts\collection_cli.py --run-id <run> report
.venv\Scripts\python.exe scripts\collection_cli.py --run-id <run> audit

# 两个只读诊断
.venv\Scripts\python.exe scripts\collection_cli.py --run-id <run> explain --item-id <token>
.venv\Scripts\python.exe scripts\collection_cli.py --run-id '@latest' resume
```

回归验证：

```powershell
.venv\Scripts\python.exe -m pytest tests\document_collection -q
.venv\Scripts\python.exe -m ruff check --select F,E9 core\document_collection tests\document_collection scripts\collection_cli.py
```

### 3.3 用 Agent 平台（可选）

本工具支持任何带 skills 目录发现的 agent 平台（ZCode / Codex / Claude 类 CLI Agent 等）。
安装 = 把 `skills\` 下 **5 个目录**（`document_collection` 编排 + 4 个 `ella-*` 能力域）
整体拷到平台 skills 根目录（如 `%USERPROFILE%\.agents\skills\`），对 agent 说一句
"装一下环境"——它会照编排 SKILL.md §0 Onboarding 自动完成：填 runtime.json 本机路径 →
建 .venv 装依赖 → 检查 lark-cli → 跑 validate-config 报告就绪（云盘授权一步由用户亲自完成）。

之后说人话即可触发：
- **整条流水线**："跑流水线 / 继续采一批" → 编排 skill 半自动模式（gate 点等你放行）；
- **按能力域调用**："导入这个文件夹" → `ella-collect`；"筛一下" → `ella-screen`；
  "给这几篇出题/生题" → `ella-generate`（query + 参考题目）；"出报告/看进度" → `ella-report`。

> 为什么是 4 个域而不是 9 个步骤：官方 skill 指南要求"一个 skill = 一个连贯工作单元"，
> 按步骤切会让一个任务要加载多个 skill、描述互相重叠。域内用路由表收敛命令。

ZCode 用户另有可选增强：把 `.zcode\commands\collect\` 拷到仓库级或 `~\.zcode\commands\`，
获得 `/collect:status`、`/collect:inventory`、`/collect:pipeline` 等 12 个斜杠命令；
其他平台用户不拷它，自然语言触发完全等价。
上游 MCP 上线后同一份 skill 原样兼容（能力名与参数映射见 capability-manifest.json 的
`future_mcp_tool`）。

## 4. 改规则只改一个文件

题型与题量、每份 query 条数、大小上限、语种配额、只读命令白名单、模型开关，全部外置在
`config/collection_policy.json`。改数字不需要动代码；改完先跑 `validate-config`。

## 5. 不可违反的硬约束

1. **云盘只读**：只允许 5 条只读命令，适配器协议里没有任何写方法，越界抛 `SafetyViolation`。
2. **大小上限是硬门槛**：PDF/DOC/DOCX/PPTX ≤ 100MB，XLSX/CSV/TXT/MD ≤ 20MB；采集阶段拒了，筛选阶段不得翻案。
3. **query 面向整篇文档**：文案里不得出现片段、章节、页码、Sheet、表格（六语种黑名单校验）。
4. **每份 10 条 query**，题量互不重复且都 < 12（选择 5 / 填空 3 / 解答 2 / 判断 7 / 混合 9）。
   句式要多样：同文档同一开场套路最多 3 次（模拟真实用户出题口吻，超了判 `query_style_repeated` 并自动重生成一次）。
5. **学科是硬条件（范围门禁）**：文档学科必须属于 P0∪P1 八类；范围外（心理学/社会学/艺术学…）淘汰，
   判不出转人工复核。学科以模型判定优先（`screen --semantic`）。**语种以正文实检为准**，不按目录名。
6. **硬失败不可被模型覆盖**；没有 evidence 的模型判断不得判为 accepted。
7. 超 5 万字不淘汰，标记 `requires_full_text_review`；query 与出题的输入窗口同一个参数
   （`content_rules.prompt_text_chars`，默认 5 万字符），与端侧/云侧入库窗口对齐。
8. **参考题目**（题干/答案/解析）由 `questions` 能力产出，语言跟随文档、答案须有原文依据；
   出题产物落盘与状态库**同写**，定向重跑只补失败项走 `--item-id`（不要自写脚本）。
9. **交付**由 `deliver` 能力产出：真实文件名目录（硬链接）+ `交付表_Query.xlsx`（一行一条 query，
   列同在线 sheet4）；选取时二次校验学科合规与语种（与 screen 门禁互为双保险）。

## 6. 已知遗留与下一步

| 项 | 说明 |
|-|-|
| 教材形态口径 | 六语种入选以单篇论文/讲义/试卷为主，与"大学教材"形态有差距；是否放宽口径待与需求方对齐（中文批 DOC-ZH 目录 508 条里无教材） |
| query 内容审核拦截 | p0-langs 批 2 份文档被 LLM 网关内容审核拒绝（"inappropriate content"，已单列 `content_audit_rejected`，重跑必复现）；处置序 换源→剔除→必要时换模型/网关 |
| `pending_export` | 1 条 `no permission`，需业务方补授权后 `acquire --status pending_export` |
| `conversion_required` | 全量 17 个 `.doc` + 1 个 `.ppt`，需批准转换器（本期不做） |
| 扫描版 PDF | 是否做 OCR 待决策（本期不做） |
| 阿拉伯语版式双重核验 | 需求 6.2 要求，需要图文理解额度（本期未做） |
| 人工复核队列 | `human_review_queue.csv` 需指定责任人 |
| 飞书授权 | refresh token **2026-09-21 到期**（2026-09-14 实测），大批量采集需在此之前完成或重新授权 |

## 7. 文档索引

| 想看什么 | 去哪 |
|-|-|
| 给 Agent 的使用说明（触发/边界/命令） | `skills/document_collection/SKILL.md`（装机副本在 `%USERPROFILE%\.agents\skills\`，5 个目录） |
| 能力清单（11 能力 + 3 诊断） | `skills/document_collection/capability-manifest.json` |
| 两周 MVP 开发计划 | `docs/需求与计划/两周MVP开发计划_CLI-Core-Skill.md` |
| 技术架构选型与理由 | `docs/需求与计划/技术架构选型.txt` |
| 开发日志（决策、22 个缺陷台账、验证记录） | `docs/document_collection/开发日志.md` |
| 基线与范围（需求 revision、实测接口事实、只读声明） | `docs/document_collection_scope.md` |
| 执行结果与验证记录 | `docs/结果与验证/执行结果与验证记录.md`（历史时点记录，2026-09-09 MVP 验收） |
| 第一轮严格筛选报告 | `docs/结果与验证/严格筛选报告_20260908_第一轮.md` |
| 迁移记录（本次搬迁） | `docs/迁移记录_20260910.md` |
| 问题与改造清单（中文批次 debug） | `docs/结果与验证/问题与改造清单_20260912_中文批次.md` |
| 改动计划与实施记录（P0 六语种批 P1–P8） | `docs/结果与验证/改动计划_20260913_P0六语种批.md` |
| 新环境真实场景测试报告 | `docs/结果与验证/新环境测试报告_20260913.md` |
| 事故修复与复验证据（D23） | `docs/结果与验证/事故修复验证_20260910/` |
| 需求原文快照 | `docs/[已脱敏目录]/` |
| MCP / Plugin 预留边界 | `mcp_server/README.md`、`plugins/README.md` |

## 8. 发给同事

发整个仓库（内部 Git；clone 天然排除 `.venv/`、`output/`、`config/llm_config.json`、各 skill 的 `runtime.json`）。同事上手：

```powershell
git clone <仓库地址> && cd Ella知识库AI生题数据集
# 用 Agent：把 skills\ 下 5 个目录拷到平台 skills 根（%USERPROFILE%\.agents\skills\），
#   然后对 agent 说"装一下环境"，它会自动建 venv、装依赖、填 runtime 路径、跑 validate-config。
#   ZCode 用户再拷 .zcode\commands\collect\ 获得 /collect:* 斜杠命令（可选）。
# 用 CLI：照 §3 直接跑（python -m venv .venv; pip install -r requirements.txt）
#   推荐本地入口：ingest --dir <文件夹> → extract → deduplicate → screen → query → questions → report → audit
# 需要模型功能：Copy-Item config\llm_config.example.json config\llm_config.json，填自己的 Key（永不入库）
# 需要采集云盘：lark-cli auth login（每台机器各自授权；refresh token 过期重登即可）
```

用 Agent 的人对它说人话就行（"盘点知识库_2026"），SKILL.md 的 `description` 会自动触发；
想深究规则看 `config/collection_policy.json`。三条纪律：云盘只读；复核历史 run 先复制到
临时工作区再用 `--workspace` 指过去；凭据永不进 git / zip / skills 目录。
