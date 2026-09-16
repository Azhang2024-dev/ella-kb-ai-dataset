# Ella 采集流水线 · 可发行版改造计划（平台无关修订版）

目标：**任何 agent 平台**（ZCode / Codex / Claude 类 CLI Agent / 其他支持 skills 目录发现的宿主）的用户，从仓库拿到 skill 包放进全局 skills 目录后，对 agent 说一句话即可完成环境安装，然后用自然语言或 slash 命令单步调用或整条流水线调度。

用户已拍板：仓库为分发载体；本轮一起做 status 只读子命令；本机旧部署同步升级。本修订吸收反馈：**不绑定 ZCode 专有机制**，slash 命令降级为 ZCode 的可选增强层，发行主体是平台无关的 skill 包。

## 平台无关性设计原则

1. **发行主体 = 纯 skill 目录**（SKILL.md + runtime.example.json + manifest），只依赖两件全平台通用的事实：`<skills 根>/<skill 名>/SKILL.md` 发现约定（现有 SKILL.md L20 已声明）、agent 能执行 shell 命令并读写文件。不依赖任何平台的专有 frontmatter 字段。
2. **slash 命令 = 可选增强**：ZCode 用户额外拷 `.zcode/commands/`（或全局 `~/.zcode/commands/`）获得 /collect:* 命令；其他平台用户用自然语言触发同一个 skill（description 负责触发），功能完全等价，不拷也不缺功能。
3. **跨平台 shell 口径**：所有命令示例同时给 PowerShell 与 bash 两栏（现有 scenarios.md 只有 PowerShell；runtime 解析改为「读 runtime.json 的 repo 字段」的跨平台写法，PowerShell 与 bash 各一行）。

## Part 1 — status 只读子命令（core + CLI + 测试）

与上一版相同，不变：

1. `service.py` 新增 `status()`（capability=`collection.status`，诊断性质不进 8 能力契约）：阶段×状态矩阵（items/artifacts/extractions/screen 计数、queries validated 数、dedup 组数、llm_stats）+ next_action（复用 `_decide_next`）；service 出结构化 summary，人读渲染放 CLI 层。
2. `cli.py` 新增 `status` 子命令（挨着 resume/audit/explain）：人读打印矩阵表格 + next_action；`--json` 出 RunResult。
3. manifest `diagnostics` 数组追加 status 条目（read_only_diagnostic，不破坏 manifest↔contracts 一致性测试）。
4. 新增 ~6 项测试（空库/有数据、JSON 结构、人读输出、只读性），现有 356 项保持全绿。

## Part 2 — Skill 包拆分（1 编排 + 8 步骤，平台无关）

```
skills/
  document_collection/          # 编排 skill（原名保留，8 个文档测试锚定此路径）
    SKILL.md                    # 改造：新增「0. Onboarding（首次安装，agent 可代做）」
                                #   + 「编排职责」节（半自动/全自动模式、gate 点、逐步汇报 status 矩阵）
                                #   + 跨平台命令两栏示例；保留 §5 停止/§6 禁止/§7 契约全部测试锚点原文
    runtime.example.json        # 新增占位模板 {"repo": "<填本机仓库根路径>"}
    runtime.json                # 本机实际路径（.gitignore 排除，不发行）
    capability-manifest.json    # diagnostics +status；其余不动（contract 一致性不破坏）
    examples/scenarios.md       # 保留 7 场景锚点；命令改 PowerShell/bash 两栏
  collect-inventory/SKILL.md    # 8 个步骤 skill，每个 ≤60 行，纯平台无关格式：
  collect-acquire/SKILL.md      #  - frontmatter 仅 name + description（不碰任何平台专有字段）
  collect-extract/SKILL.md      #  - description 含「单步骤；整条流水线用 document_collection」防误触发
  collect-dedup/SKILL.md        #  - 正文：触发词 / 前置条件 / CLI 调用式（两栏 shell）/ 本步停止规则（引用编排 SKILL.md §5）/ 本步要看的 summary 字段
  collect-screen/SKILL.md
  collect-query/SKILL.md
  collect-report/SKILL.md
  collect-status/SKILL.md
```

runtime 解析约定（解决 9 份副本漂移问题）：每个 skill 目录都放 1 行 runtime.json 指向同一仓库根（拷贝安装形态下每个目录必须自洽）；onboarding 流程统一生成/校验 9 份内容一致（同一 repo 值），编排 skill 的 onboarding 第 1 步就是「校验所有 collect-* 目录的 runtime.json 与本目录一致」。

Onboarding 流程（编排 SKILL.md 新章节，agent 按序自检自修，全部跨平台）：
1. 读本目录 runtime.json → repo 路径存在且含 `scripts/collection_cli.py`？→ 是则跳到 4；
2. 失效/占位符：问用户要本机仓库根，更新本目录及全部 collect-* 目录的 runtime.json（只改 skills 侧副本，仓库零改动）；
3. 环境安装：`<repo>/.venv` 不存在 → `python -m venv .venv` + `pip install -r requirements.txt`（Windows/Linux 命令两栏）；
4. `lark-cli --version` ≥ 1.0.85？缺 → 提示用户自行 `npm install -g`；`lark-cli auth login` 授权必须用户亲自完成，agent 只提示；
5. 跑 `validate-config` → 全绿报「就绪」；退出码 4 → 提示 policy 矛盾，停下来。

每个步骤 skill 的安全纪律引用不复制：云盘只读白名单、禁止绕过 CLI 改 SQLite、历史 run 先复制再复核（D23-2）、凭据不入 skills 目录——各 1-2 行引用编排 SKILL.md §6；业务数字（题型题量/配额/大小上限）零复制，一律指路 `config/collection_policy.json`。

## Part 3 — slash 命令（可选增强层，仅 ZCode）

新建 `.zcode/commands/collect/`（`/collect:status|inventory|acquire|extract|dedup|screen|query|report|pipeline`），每个 ≤15 行：frontmatter（description、argument-hint、skills: 对应 skill）+ 一行指令 + `$ARGUMENTS` + 统一收尾（按 RunResult 汇报 status/artifacts/review_items/next_action，触发停止规则就停）。
- README 明确标注：此目录是 ZCode 专属可选增强，其他平台用户跳过，不影响任何功能；
- 不把 `.zcode/commands` 写进任何 skill 的依赖路径——所有 skill 在无此目录时完整可用。

## Part 4 — 测试、验收、部署同步

1. 既有 356 项全绿（A 组 8 个锚点测试不动：`name: document_collection`、中文短句、manifest↔contracts 一致性全保留）；
2. 新增测试 ~18 项：status 命令 ~6；skill 包一致性 ~8（每个步骤 skill 有合法 frontmatter、引用的 CLI 子命令真实存在、正文不含 policy 数字与 `--sample-seed 2026` 之类硬编码业务值、runtime.json 9 份同值）；commands 格式 ~4（可解析、skills 名真实存在）；
3. 模拟新机器手动验收：临时目录复制仓库 → runtime 填占位 → 照 onboarding 走完 → validate-config 全绿 → status 出矩阵；
4. 本机部署同步升级：新版 9 个 skill 目录拷到 `~/.agents/skills/`（替换旧单体版，消除两份竞争指令），runtime.json 填本机仓库根；
5. README §3.1/§8 改写为平台无关分发说明（拷贝清单 1→9 个目录、onboarding 一句话、ZCode 用户额外拷 commands）。

## 交付顺序

Part 1（status）→ Part 2（skill 拆分）→ Part 3（commands）→ Part 4（测试全绿 + 部署升级 + README）→ git 提交（1 个 commit）。

## 明确不做

- 不复活 D23-1 裁撤的安装器（安装 = agent 执行 onboarding 人话步骤）；
- 不做 MCP Server（mcp_server/README.md 边界不变）；
- 不改 collection_policy.json 业务数字；不改 core 七阶段业务逻辑（只加只读 status）;
- 不为其他平台写专有适配（Cursor 规则文件 / Claude 命令文件等）；他们的触发靠 skill description 通用约定，够用且零维护。