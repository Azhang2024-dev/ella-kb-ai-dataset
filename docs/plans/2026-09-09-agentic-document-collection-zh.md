# Agentic 文档采集与筛选开发计划（中文版）

> 本文件是 `2026-09-09-agentic-document-collection.md` 的中文对应版。代码路径、接口名称、文档 ID、版本号和命令保持原样，便于中英文对照和后续执行。
>
> **给 Agent 执行者：** 必须使用 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans`，按任务逐项执行本计划。每个任务都必须完成自己的验证检查点，确认通过后才能进入下一任务。

**目标：** 构建一个与平台无关、以 Agent 为主导的文档采集与筛选系统。系统可以通过 CLI 和 MCP 调用，后续也可以包装成 Skill 或特定宿主平台的 Plugin；系统能够处理两个已授权的飞书云盘数据源，同时不修改云盘中的原始文件。

**架构：** 在类型化能力工具之上构建一个轻量、与运行时无关的 Agent Harness。Python Core 包负责确定性操作、持久化状态、证据和验证；Agent 负责选择下一项能力、处理歧义并推进循环。MCP 作为跨平台接口；Skill 描述 Agent 的行为；Plugin 只负责特定宿主平台的包装，不承载业务核心。

**技术栈：** Python 3.12；标准库 `asyncio`、`sqlite3`、`dataclasses`、`argparse`、`pathlib`、`hashlib`、`json`；已有依赖 `PyMuPDF`、`pdfplumber`、`python-docx`、`openpyxl`、`pandas`、`litellm`；PPTX 使用 `python-pptx`；官方 MCP Python SDK v2 放在隔离的可选依赖组中；PySide6 仅作为后续 UI 适配层；测试使用 `pytest` 和 `pytest-asyncio`。

**需求依据：** 当前飞书采集计划 `REDACTED_DOC_039`，revision `198`；主需求 `REDACTED_DOC_074`，revision `428`；多格式需求 `OtdnwZwPaiOhHfk43mYcpuAOndg`，revision `1140`。每次运行都必须记录这些 revision 和内容哈希；一个运行开始后，不得静默跟随在线文档的新版本。

## 全局约束

- 数据源文件夹为 `知识库_2026` 和 `docQA.llm`；不得修改云盘原始文件。
- 两棵数据源目录树必须合并盘点、统一去重，不能把两个文件夹分别计数后直接当作最终文档数。
- P0 语言为英语、俄语、法语、中文、阿拉伯语和西班牙语；按当前计划，每种语言最终至少需要 20 份文档。
- P1 学科为法学、教育学/语言学、临床医学/护理、基础理科；按当前计划，每个方向最终至少需要 1 份文档。
- 当前计划要求每份最终文档生成 10 条 query；每条 query 请求的题目数小于 12；query 文本必须面向整篇文档，不能写片段、章节、页码、Sheet 或表格。
- 当前计划把文件大小限制作为硬门槛：PDF/DOC/DOCX/PPTX 最大 100 MB；XLSX/CSV/TXT/MD 最大 20 MB；在线资源在取得导出文件大小或权威大小之前，必须保持 `pending_size_verification` 状态。
- 超过 50,000 字符的文档不能直接自动淘汰；必须进入全文理解路径，并完成覆盖度验证。
- 图像理解不是批处理默认路径；以图形为主的资料排除，但包含文字、表格和公式的资料可以保留。
- 硬规则和软目标必须分开。格式比例只是软目标，不能为了满足格式比例而保留低质量文档。
- 任何 LLM 输出都不能覆盖确定性硬拒绝，除非有明确记录的人工决定。
- 所有外部副作用默认拒绝。系统只允许读取、列举、下载/导出，以及写入本地产物；不得删除、移动、重命名或修改云盘源文件。
- 所有长时间运行的操作必须可恢复、幂等并且可审计。
- 第一阶段不能修改已有 crawler/query 流程。新增文档采集代码必须放在独立包中，在确认安全后通过适配器复用已有能力。
- `D:\Users\admin\Desktop\My_Project\Ella知识库AI生题数据集` 当前工作树不干净。实施必须从明确的分支/worktree 快照开始，不能把无关的既有修改混入本功能。

---

## 1. 范围、边界和目标产品

### 1.1 第一版包含

- 需求快照，以及把需求编译为可执行策略。
- 对两个授权飞书云盘文件夹进行递归盘点。
- 带重试和检查点的本地采集/导出。
- 格式识别和文档内容抽取。
- 页码/章节/工作表/语义单元定位器。
- 精确去重、规范化文本去重，以及近重复人工复核簇。
- 确定性硬筛选和可配置的软排序。
- Agent 辅助的语义分类和异常处理。
- 面向整篇文档的 query 生成，以及 query 级别的证据记录。
- MCP Server 暴露和至少一个宿主平台的 Agent 冒烟测试。
- 在各个独立能力通过验收后，再实现通用编排循环。
- 报告、审计账本、回放夹具和操作员文档。

### 1.2 第一版明确不包含

- 向飞书云盘文件夹回写内容。
- 自动接受在线文档中尚未确认的业务规则变化。
- 完全自主地修改源代码、prompt 或生产策略。
- 大规模多 Agent swarm。
- 一直运行的云端编排部署，包括 Airflow、Temporal 或 Kubernetes。
- 在 CLI/MCP 路径通过 Pilot 之前制作完整的 PySide6 界面。
- 对所有扫描 PDF 自动执行 OCR。
- 把 LLM 评审作为唯一验收机制。

### 1.3 产品分层

```text
宿主 Agent（OpenClaw / Codex / 其他 MCP 客户端）
          |
          | Skill：说明、停止规则、工具选择指引
          v
MCP Server / CLI
          |
          | 类型化能力服务
          v
Core 包
  - 数据源适配器
  - 采集/导出
  - 抽取器
  - 去重
  - 策略评估
  - Query 生成
  - 验证
          |
          v
SQLite 运行账本 + 不可变本地产物 + 报告
```

Skill 不是数据处理引擎，而是轻量的行为层。MCP Server 和 CLI 必须能够在没有 Skill 的情况下执行同一项能力，以保证系统可测试，也能被非 Agent 自动化流程调用。

---

## 2. 实现前必须先稳定的契约

### 2.1 运行上下文

在 `core/document_collection/contracts.py` 中创建不可变 dataclass 或 Pydantic model，至少定义以下逻辑记录：

```python
@dataclass(frozen=True)
class RunContext:
    run_id: str
    requirement_revision: str
    policy_hash: str
    source_snapshot_id: str
    workspace: str
    dry_run: bool
    approval_mode: str  # "deny_by_default" | "approved_read" | "operator_confirmed"
    max_cost: float | None
    max_steps: int
    random_seed: int
```

还必须定义：

```python
@dataclass(frozen=True)
class SourceItem:
    source_name: str
    source_folder_token: str
    source_path: str
    item_token: str
    item_type: str
    name: str
    url: str | None
    mime_type: str | None
    extension: str | None
    remote_size: int | None
    remote_modified_time: str | None
    metadata: dict[str, Any]

@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    path: str
    sha256: str
    media_type: str
    byte_size: int
    created_by: str

@dataclass(frozen=True)
class EvidenceRef:
    artifact_id: str
    locator_type: str  # page, section, sheet, table, char_range, semantic_unit
    locator: dict[str, Any]
    quote_hash: str | None

@dataclass(frozen=True)
class DecisionRecord:
    item_token: str
    decision: str  # accepted, rejected, review, pending
    reason_codes: tuple[str, ...]
    hard_failures: tuple[str, ...]
    soft_scores: dict[str, float]
    evidence: tuple[EvidenceRef, ...]
    policy_hash: str
    reviewer: str  # deterministic, agent, human
```

MCP 工具结果中不能直接返回大段文档正文。应返回 `ArtifactRef` 和 `EvidenceRef`；大段抽取文本通过本地 artifact resource 或受控的范围读取接口暴露。

### 2.2 能力契约

每项能力都必须声明：

```text
name
version
input_schema
output_schema
preconditions
side_effects
retry_policy
idempotency_key
verification_command
```

能力只能返回以下状态之一：

```text
completed
partial
needs_approval
needs_human_decision
retryable_failure
permanent_failure
```

这样，即使底层解析器或数据源发生变化，Agent 仍然面对稳定的调用表面。

### 2.3 策略契约

创建 `config/collection_policy.example.json`。如果活动策略中包含私有数据源标识或模型设置，则活动策略必须放在版本控制之外。

策略必须分开保存以下内容：

```json
{
  "requirement_snapshot": {
    "document_id": "...",
    "revision_id": 198,
    "content_sha256": "..."
  },
  "sources": [],
  "hard_rules": {},
  "soft_targets": {},
  "language_quota": {},
  "subject_quota": {},
  "query_policy": {},
  "model_policy": {},
  "review_policy": {},
  "retention_policy": {}
}
```

不得仅因为实现方便就新增阈值。如果需求没有定义文档长度或解析质量的数值下限，应记录诊断信息并把该条目送入语义评审，而不是擅自发明硬性淘汰线。

---

## 3. 仓库目录和文件责任

下面的实现路径均相对于：

```text
D:\Users\admin\Desktop\My_Project\Ella知识库AI生题数据集
```

### 3.1 新增 Core 包

```text
core/document_collection/
    __init__.py
    contracts.py          # RunContext、SourceItem、ArtifactRef、Decision 等契约
    errors.py             # 稳定错误码和可重试性
    policy.py             # 加载/验证/计算策略快照指纹
    requirement_compiler.py
    state_store.py        # SQLite 结构和事务
    event_log.py          # 只追加的 JSONL 事件
    artifact_store.py     # 原子写入、内容寻址的本地产物存储
    scheduler.py          # 有界并发和共享限流器
    source_base.py        # DocumentSource 协议
    acquisition.py
    extraction.py
    normalization.py
    deduplication.py
    screening.py
    ranking.py
    query_generation.py
    auditing.py
    service.py             # 应用服务门面
    adapters/
        __init__.py
        lark_drive.py
        local_files.py
        online_doc.py
        online_sheet.py
        online_slides.py
    extractors/
        __init__.py
        pdf_extractor.py
        docx_extractor.py
        legacy_office_extractor.py
        pptx_extractor.py
        xlsx_extractor.py
        csv_extractor.py
        text_extractor.py
```

### 3.2 Agent 和 MCP 层

```text
mcp_server/
    __init__.py
    server.py              # 官方 MCP Python SDK 入口
    tool_registry.py
    resource_registry.py
    error_mapping.py

scripts/
    collection_cli.py      # argparse CLI；只调用 core.service
    collection_requirements.py
    collection_replay.py

skills/
    document_collection/
        SKILL.md
        examples/
        capability-manifest.json

workers/
    document_collection_worker.py  # 仅在 CLI 稳定后新增
```

### 3.3 测试和夹具

```text
tests/document_collection/
    conftest.py
    fixtures/
        lark_drive_pages/
        documents/
        extracted/
        policies/
        expected_reports/
    test_contracts.py
    test_policy.py
    test_state_store.py
    test_artifact_store.py
    test_lark_inventory.py
    test_acquisition.py
    test_extractors.py
    test_deduplication.py
    test_screening.py
    test_query_generation.py
    test_auditing.py
    test_mcp_contract.py
    test_skill_scenarios.py
    test_replay.py
```

### 3.4 初期不得修改的现有文件

不得为了适配新功能而修改 `core/datasource.py`、`core/smart_crawler.py`、`core/datasource_wikipedia.py`，也不得仅为了接入新功能而修改现有 `core/doc_processor.py` 的 stage dispatch。已有 Stage 1 抽取和 query 生成能力，在确认其输出语义后可以通过适配器包装或复用；但新子系统需要页码/章节/工作表级别的来源定位，而当前扁平化抽取文本路径不能保证这一点。

第一次 GUI 集成之前，不得修改 `app/main_window.py` 或现有工作流页面。开始 UI 工作后，应遵循现有的 `MainWindow -> WorkflowSession -> workflow page -> worker` 模式，业务逻辑仍然放在 `core/document_collection` 中。

---

---

## 4. 开发任务和实现细节

## Task 0：冻结需求、仓库基线和风险决策

**文件：**

- 创建：`docs/superpowers/specs/2026-09-09-agentic-document-collection-spec.md`
- 创建：`docs/superpowers/specs/requirement-snapshots/2026-09-09.json`
- 创建：`docs/superpowers/specs/open-decisions.md`
- 只有经过明确评审后才能修改：现有源文件均不修改

**目的：** 防止在需求持续变化或代码库处于脏状态时直接开始实现。

**步骤：**

- [ ] 记录当前需求 ID 和 revision：采集计划 `198`、主需求 `428`、多格式需求 `1140`。
- [ ] 保存已获取文档内容的 SHA-256，以及完整的获取命令输出。
- [ ] 针对已知差异写出核对表：
  - 采集计划的 query 策略与源文档中用户题量范围的差异；
  - PPT/PPTX 源统计与当前大小规则只列出 PPTX 的差异；
  - DOC/PPT 旧格式与当前解析器可用性的差异；
  - 在线资源大小如何核验；
  - v0.2 多格式需求仍处于 draft 状态。
- [ ] 记录现有仓库的 `git status`、当前 commit、Python 版本、依赖版本和 `lark-cli` 版本。
- [ ] 从选定基线创建实现分支/worktree。除非负责人明确选择，否则不要把当前工作树中的无关修改带入基线。
- [ ] 在实现相关行为之前，先取得 `open-decisions.md` 中所有事项的明确决定。

**验证：**

- 不联网时仍然可以重新读取需求快照。
- 评审人员可以判断任意后续运行使用的是哪一版需求。
- 基线和开放决策未记录前，不产生代码修改。

**闸门：** 需求和基线分支获得批准。

## Task 1：新增独立包骨架和依赖边界

**文件：**

- 创建：`core/document_collection/__init__.py`
- 创建：`core/document_collection/contracts.py`
- 创建：`core/document_collection/errors.py`
- 创建：`core/document_collection/service.py`
- 创建：`scripts/collection_cli.py`
- 创建：`requirements-collection.txt`
- 测试：`tests/document_collection/test_contracts.py`

**实现：**

- [ ] Core 记录使用标准库 dataclass 和 typing；`core/document_collection` 下不能引入 Qt。
- [ ] 官方 MCP Python SDK 作为可选的采集集成依赖，在第一次 MCP 冒烟测试通过之前，与现有 GUI 依赖集合隔离。
- [ ] 只有在环境尚未提供 `python-pptx`、`pytest` 和 `pytest-asyncio` 时，才把它们加入采集依赖文件。
- [ ] 定义稳定错误码，例如 `SOURCE_AUTH`、`SOURCE_PAGE_INCOMPLETE`、`SIZE_EXCEEDED`、`MIME_UNKNOWN`、`EXTRACTION_FAILED`、`LOCATOR_MISSING`、`DUPLICATE_HIGH_CONFIDENCE`、`NEEDS_HUMAN_REVIEW` 和 `BUDGET_EXCEEDED`。
- [ ] 让 `scripts/collection_cli.py` 只导入 `core.document_collection.service`，并提供 `--help`、`--version` 和空操作的 `validate-policy` 命令。

**验证：**

```powershell
.venv\Scripts\python -m pytest tests\document_collection\test_contracts.py -q
.venv\Scripts\python -m compileall -q core\document_collection scripts\collection_cli.py
```

**闸门：** Core 可以在不加载 Qt 的情况下导入；现有应用测试保持不变。

## Task 2：实现策略加载、验证、哈希和需求快照

**文件：**

- 创建：`core/document_collection/policy.py`
- 创建：`core/document_collection/requirement_compiler.py`
- 创建：`config/collection_policy.example.json`
- 创建：`scripts/collection_requirements.py`
- 测试：`tests/document_collection/test_policy.py`

**实现：**

- [ ] 使用 UTF-8 加载 JSON 策略；除非明确列在 `extensions` 下，否则拒绝未知的顶层字段。
- [ ] 在哈希前使用排序后的 key 对 JSON 做规范化。
- [ ] 在运行开始前验证所有硬规则、配额、query 策略和副作用策略。
- [ ] 实现 `RequirementSnapshot`，包含文档 ID、revision、内容哈希、获取时间和核对说明。
- [ ] 实现 `compile_requirement_draft(snapshot) -> RequirementSpecDraft`。LLM 可以提出语义字段，但输出必须通过 Schema 验证，并且在人工确认前必须标记为 `draft`。
- [ ] 对冲突字段执行确定性核对；遇到冲突时返回 `needs_human_decision`，不能静默选择其中一项。
- [ ] 把确认后的 `RequirementSpec` 固化到运行记录中；在线文档后续变化必须新建一次运行，不能修改当前活动运行。

**验证：**

- 相同策略内容产生相同哈希。
- 只改变 key 顺序不会改变哈希。
- 缺少硬规则的策略在采集前失败。
- 冲突的 query 策略会被报告为开放决策。
- 需求 revision 变化后会产生不同的 snapshot ID。

**闸门：** 没有经过验证并版本化的策略快照，运行不得开始。

## Task 3：实现持久化状态、产物存储和事件日志

**文件：**

- 创建：`core/document_collection/state_store.py`
- 创建：`core/document_collection/artifact_store.py`
- 创建：`core/document_collection/event_log.py`
- 测试：`tests/document_collection/test_state_store.py`
- 测试：`tests/document_collection/test_artifact_store.py`

**实现：**

- [ ] 使用 SQLite，并把显式迁移文件放在 `core/document_collection/migrations/`。
- [ ] 创建 `runs`、`requirement_snapshots`、`source_items`、`acquisition_attempts`、`artifacts`、`extractions`、`dedup_clusters`、`decisions`、`queries`、`evidence`、`approvals` 和 `events` 表。
- [ ] 每次条目状态变更使用一个事务；不得把条目更新和事件日志写入拆成两个未提交的操作。
- [ ] 使用基于 SHA-256 的内容寻址文件名，把产物保存到 `output/document_collection/<run_id>/artifacts/`。
- [ ] 先写入临时文件，再原子重命名。
- [ ] 定义幂等键：
  - inventory：`(source_name, folder_token, snapshot_params_hash)`；
  - acquisition：`(item_token, remote_version_or_modified_time, requested_export_format)`；
  - extraction：`(artifact_sha256, extractor_name, extractor_version)`；
  - decision：`(item_token, policy_hash, evidence_hash, reviewer_version)`。
- [ ] 实现 `resume_run(run_id)`：已经完成的幂等操作直接跳过，未完成的操作重新进入队列。
- [ ] 事件 payload 不保存完整敏感文档正文，只保存 artifact/evidence 引用。

**验证：**

- 在每类状态变更期间终止并重启运行。
- 重复执行同一操作不会生成重复产物或重复决策。
- SQLite integrity check 通过。
- 每次状态变更都有对应事件。

**闸门：** 进程终止后，运行可以恢复，且不会重新下载或重新决策已经完成的条目。

## Task 4：实现数据源适配器和 inventory

**文件：**

- 创建：`core/document_collection/source_base.py`
- 创建：`core/document_collection/adapters/lark_drive.py`
- 创建：`core/document_collection/adapters/local_files.py`
- 创建：`scripts/collection_cli.py` 的 `inventory` 子命令
- 测试：`tests/document_collection/test_lark_inventory.py`
- 夹具：`tests/document_collection/fixtures/lark_drive_pages/`

**接口：**

```python
class DocumentSource(Protocol):
    async def snapshot(self, ctx: RunContext) -> str: ...
    async def enumerate_items(self, snapshot_id: str) -> AsyncIterator[SourceItem]: ...
    async def acquire(self, item: SourceItem, destination: Path, ctx: RunContext) -> ArtifactRef: ...
```

**实现：**

- [ ] 将 `lark-cli drive files list` 封装在一个适配器中。CLI 参数构造和 JSON 响应解析必须集中在 `lark_drive.py` 内。
- [ ] 显式使用 `folder_token`、`page_size` 和 `page_token` 字段；不能依赖客户端猜测分页方式。
- [ ] 递归盘点 `知识库_2026` 和 `docQA.llm`，并在每一行记录中保留准确的数据源文件夹名称。
- [ ] 保存原始分页响应作为夹具/回放产物；仅在发布测试夹具确有需要时脱敏 token。
- [ ] 检测 `has_more`；如果页面声称还有数据但没有 next token，必须快速失败，不能继续猜测。
- [ ] 分开识别文件夹、普通文件、在线 Docx、在线 Sheet、在线 Slides、快捷方式和未知资源类型。
- [ ] inventory 阶段不得调用内容下载。
- [ ] 提供 `--dry-run` 和 `--record-response` 模式。
- [ ] 增加本地文件系统适配器，使后续阶段可以不依赖飞书访问进行测试。

**验证：**

- 回放夹具可以产生确定性的 inventory 行。
- inventory 数量与当前记录的 source snapshot 一致；如果云端发生变化，必须报告差异，不能静默接受。
- 适配器中不存在源文件变更命令。
- 分页和递归测试覆盖空文件夹、单页文件夹、多页文件夹、嵌套文件夹、重复 token 和格式错误页面。

**闸门：** 仅 inventory 模式可以完成两个文件夹的盘点，并生成 source snapshot，不下载文件。

## Task 5：实现采集、导出、重试和限流

**文件：**

- 创建：`core/document_collection/acquisition.py`
- 创建：`core/document_collection/scheduler.py`
- 修改：`core/document_collection/adapters/lark_drive.py`
- 测试：`tests/document_collection/test_acquisition.py`

**实现：**

- [ ] 增加共享并发限制器，而不是只在单个任务中 sleep。
- [ ] 如果服务端提供重试延迟，优先遵守；否则使用有界指数退避。
- [ ] 将失败分类为可重试、永久失败或需要审批。
- [ ] 使用原子临时下载，并在提交 artifact 记录前核验字节数和 SHA-256。
- [ ] 按资源类型实现采集：
  - 本地文件：只在安全时复制或创建硬链接；
  - PDF/DOC/DOCX/PPTX/XLSX/CSV/TXT/MD：下载二进制内容；
  - 在线 Docx/Sheet/Slides：通过对应文档适配器导出或获取；
  - 未知资源/快捷方式/JPG/URL：保留 inventory 元数据，但不能强行纳入最终集合。
- [ ] 在线资源在取得导出文件或权威元数据前，保持 `pending_size_verification`。
- [ ] 实现 `--max-concurrency`、`--min-interval`、`--max-retries`、`--max-bytes` 和 `--approval-mode`。
- [ ] 不得记录访问 token 或签名 URL。

**验证：**

- 模拟 429、5xx、超时、响应截断、校验和不匹配和磁盘空间不足。
- 确认重试不会生成重复的半成品文件。
- 确认暂停后的运行可以从最近一次已提交的条目继续。
- 确认适配器无法调用删除、移动或重命名操作。

**闸门：** 小批量只读采集 Pilot 可以在完整保留来源信息的情况下完成，且不会修改源文件。

## Task 6：实现格式识别和保留来源定位的内容抽取

**文件：**

- 创建：`core/document_collection/extraction.py`
- 创建：`core/document_collection/normalization.py`
- 创建：`core/document_collection/extractors/*.py`
- 测试：`tests/document_collection/test_extractors.py`
- 夹具：`tests/document_collection/fixtures/documents/`

**实现：**

- [ ] 使用文件签名和扩展名识别真实 MIME；绝不能只信任扩展名。
- [ ] 定义统一的抽取结果：

```python
@dataclass(frozen=True)
class ExtractionResult:
    artifact_id: str
    extractor: str
    extractor_version: str
    text_units: tuple[TextUnit, ...]
    char_count: int
    word_count: int
    cjk_char_count: int
    page_count: int | None
    extraction_ratio: float | None
    has_tables: bool
    has_formulas: bool
    has_images: bool
    graphic_dominant: bool | None
    locator_quality: str
    diagnostics: tuple[str, ...]
```

- [ ] PDF：优先使用 PyMuPDF，失败时使用 pdfplumber；保留页码和文本块；扫描页或视觉结构复杂的页面，进入受限的 OCR/视觉兜底路径。
- [ ] DOCX：使用 python-docx 处理段落和表格；尽可能保留标题样式和表格坐标。
- [ ] DOC：使用明确的可选转换适配器；不能静默把 DOC 当成 DOCX。如果没有经过批准的转换器，标记为 `conversion_required`。
- [ ] PPTX：使用 python-pptx 抽取幻灯片文本，以及存在时的演讲者备注；保留幻灯片编号。旧版 PPT 在验证前单独标记为不支持/需要转换。
- [ ] XLSX：使用 openpyxl；保留工作簿、工作表、单元格范围、合并单元格、公式以及可用时的显示值。
- [ ] CSV：使用 csv 模块，并做编码识别和分隔符诊断；保留行列坐标。
- [ ] TXT/MD：保留行号范围，并识别 Markdown 结构是真实结构还是被压平的纯文本。
- [ ] 在线 Docx/Sheet/Slides：将导出结果规范化到同一个 `ExtractionResult` 契约，并记录原在线 token。
- [ ] 阿拉伯语和混合方向文本：记录抽取顺序诊断；对可疑页面送入视觉复核。
- [ ] 不能把当前扁平化的 Stage 1 输出作为唯一真相来源，因为 query 证据需要页码/章节/工作表定位。
- [ ] 对长文档做标记而不是直接拒绝；全文理解覆盖度是后续验证步骤。

**验证：**

- 每个夹具都有稳定的页码/章节/工作表定位。
- 对相同 artifact 和相同抽取器版本，抽取结果确定性一致。
- 损坏或受密码保护的文件会产生类型明确的失败。
- DOC/PPT 旧格式行为是显式的。
- 图形主导夹具与“包含插图但以文字为主”的夹具能够区分。

**闸门：** 抽取层能够为所有进入筛选阶段的候选文档提供内部证据。

## Task 7：实现精确、规范化和近重复分析

**文件：**

- 创建：`core/document_collection/deduplication.py`
- 测试：`tests/document_collection/test_deduplication.py`

**实现：**

- [ ] 首先比较 artifact SHA-256。
- [ ] 然后使用有文档说明、考虑语言差异的规范化函数处理文本：去除抽取产生的空白噪声，但不能去掉有意义的数字、公式或语言字符。
- [ ] 计算规范化文本哈希。
- [ ] 近重复比较前先按元数据分桶，包括标题、页数、语言和大致字符数。
- [ ] 实现有上限的近重复候选比较。除非置信度阈值和证据策略均确认安全，否则不得自动删除近重复项。
- [ ] 输出 `dedup_cluster_id`、`duplicate_confidence`、`canonical_candidate` 和 `retention_reason`。
- [ ] 优先保留教育属性证据更强、定位质量更好、抽取比例更高、解析失败更少的版本。
- [ ] 即使最终集合只保留一个代表，也必须保留所有源文件的来源信息。

**验证：**

- 精确重复在夹具中达到 100% 精确聚类。
- 清洗版/转换版会形成待复核簇，而不是被静默丢弃。
- 仅标题相同的不同文档不能被合并。
- 跨源重复报告同时显示两个源文件夹名称。

**闸门：** 去重过程可解释、可逆。

## Task 8：实现硬筛选、语义评审和排序

**文件：**

- 创建：`core/document_collection/screening.py`
- 创建：`core/document_collection/ranking.py`
- 创建：`core/document_collection/requirement_review.py`
- 测试：`tests/document_collection/test_screening.py`

**实现：**

- [ ] 首先评估确定性硬规则：
  - 真实 MIME 和支持的格式；
  - 大小限制；
  - 采集成功；
  - 抽取成功；
  - 目标语言存在且占主导；
  - 能提供来源定位；
  - 排除图形主导文档；
  - 去重策略；
  - 文档层面的大学/高等教育使用证据。
- [ ] 返回 reason code，不使用无法稳定解析的自由文本作为唯一依据。
- [ ] 仅将无法可靠确定的语义属性交给 LLM，例如教育属性、学科、材料类型、内容质量，以及内容能否支持 10 条互不重复的 query。
- [ ] 要求 LLM 返回带证据引用和置信度的结构化 JSON；输出格式错误或没有证据支持时拒绝接受。
- [ ] 默认不把完整原文暴露给模型。优先提供受控摘录或结构化语义单元；全文评审必须是有明确开关的模式，并受成本和大小限制。
- [ ] 保持当前计划中的硬规则和软规则分离。
- [ ] 在可行的语言/学科分桶内排序；先满足最低配额，再优化质量和多样性。
- [ ] 把格式比例 `3:2:2:1:1:1` 仅作为软目标。
- [ ] 同时保存 Agent 决策和人工决策；不得覆盖历史决策。

**验证：**

- 预置的硬失败样本绝不能通过。
- LLM 语义判断失败时返回 `review` 或 `unknown`，不能静默接受。
- 格式非常理想但教育属性证据不足的文档，不能仅为满足格式平衡而接受。
- 修改软目标后重新排序，不得改变硬资格判断。

**闸门：** 评审人员可以依据已保存的证据和 reason code，解释每一份文档为什么被接受或拒绝。

## Task 9：把 query 能力实现为独立 Skill/Tool

**文件：**

- 创建：`core/document_collection/query_generation.py`
- 创建：`skills/document_collection/SKILL.md`
- 创建：`skills/document_collection/capability-manifest.json`
- 测试：`tests/document_collection/test_query_generation.py`
- 测试：`tests/document_collection/test_skill_scenarios.py`

**实现：**

- [ ] 使用确认后的 `RequirementSpec` 和选定的文档 artifact，不读取可变的在线策略。
- [ ] 每份最终文档严格生成 10 条 query 记录。
- [ ] query 文本必须面向整篇文档；不得在 query 中写章节、节、页码、Sheet 或表格。
- [ ] 把内部覆盖目标、证据定位和原文片段单独存储，不写进对外 query 文本。
- [ ] 按当前计划生成 8 条单题型 query 和 2 条混合题型 query；混合题型只用于兼容性验证。
- [ ] 使用互不重复的、且小于 12 的单条题量；可建议使用 2、3、5、7、9 等数值，但具体映射必须可配置。
- [ ] query 语言跟随文档语言。
- [ ] 校验 JSON Schema、query 数量、题型数量、题目数、语言、证据是否存在，以及 query 文本是否重复。
- [ ] 模型输出格式错误时，只能在配置的重试预算内使用 repair prompt 重试；超出预算后标记为 `query_generation_failed`。
- [ ] 保存 prompt 版本、模型 ID、输入 artifact 哈希、输出哈希和验证器结果。

**Skill 验收场景：**

```text
给定一份已批准的英文或中文文档夹具，
当 Agent 被要求为该文档创建评测 query 时，
Agent 调用 query 能力，
返回严格 10 条结构化记录，
query 文本不包含片段/章节引用，
内部证据仍然保留定位信息，
并产出验证报告。
```

**闸门：** query Skill 必须先独立可调用、可验证，然后才能建设全量采集编排。

## Task 10：实现 MCP Server 和能力资源

**文件：**

- 创建：`mcp_server/server.py`
- 创建：`mcp_server/tool_registry.py`
- 创建：`mcp_server/resource_registry.py`
- 创建：`mcp_server/error_mapping.py`
- 测试：`tests/document_collection/test_mcp_contract.py`

**实现：**

- [ ] 使用官方 MCP Python SDK v2；在采集依赖锁定文件中固定已经测试过的 minor 版本。
- [ ] 开发和单宿主测试先使用本地 stdio transport；本地契约稳定后再增加 HTTP transport。
- [ ] 每项稳定能力暴露一个 MCP tool，不要把整个流水线压缩成一个大工具。
- [ ] 工具输入/输出 Schema 必须与 `contracts.py` 对齐。
- [ ] 大型报告和抽取文本范围通过 resource 或 artifact reference 暴露，不能塞进超大的 tool 响应。
- [ ] 将 Core 类型化错误映射为结构化 MCP 错误，并带上可重试性和人工动作字段。
- [ ] 根据 `approval_mode` 强制执行 tool allowlist。
- [ ] 增加 `health`、`capabilities` 和 `resume_run` 工具。
- [ ] MCP 层必须是薄适配器，不得包含文档选择业务规则。

**验证：**

- Schema 客户端能够列出工具，并验证所有工具 Schema。
- 每个工具都能使用本地夹具调用，无需飞书凭据。
- 只读策略会拒绝破坏性操作。
- 大型 artifact 通过引用返回，并可通过受控 resource 操作读取。
- MCP 重启后运行状态不丢失，因为状态保存在 SQLite 和本地产物中。

**闸门：** 至少一个宿主平台可以通过 MCP 调用 inventory、inspect、screening、query 和 audit，不直接导入 Core 内部实现。

## Task 11：建立单宿主 Agent Skill 冒烟测试

**文件：**

- 修改：`skills/document_collection/SKILL.md`
- 创建：`tests/document_collection/agent_eval_cases.jsonl`
- 创建：`scripts/collection_agent_smoke.py`
- 测试：`tests/document_collection/test_skill_scenarios.py`

**实现：**

- [ ] 定义 Skill 允许使用的工具、所需证据、停止条件和人工审批边界。
- [ ] 增加以下场景：只做 inventory；检查一个候选；拒绝超大文件；将未知 MIME 送人工复核；采集失败后恢复；为一份已接受文档生成 query；拒绝修改云盘源文件的请求。
- [ ] 记录真实工具调用、参数、状态迁移和最终产物。
- [ ] 增加确定性回放模式，由 Agent 工具调用与夹具服务器进行核对。
- [ ] 离线测试通过后，至少通过选定的 Agent 平台执行一次真实调用。

**验收指标：**

- 所有工具调用符合 Schema。
- 不产生任何禁止的副作用。
- 每个最终决策都有证据，或者明确返回 `needs_human_decision`。
- 同一夹具重复运行时，除时间戳和 run ID 外，决策和 artifact 哈希等价。

**闸门：** 单个 Skill 稳定到足以作为后续编排的构件。

## Task 12：实现通用 Manager Loop

**文件：**

- 创建：`core/document_collection/agent_loop.py`
- 创建：`core/document_collection/stop_conditions.py`
- 创建：`core/document_collection/approvals.py`
- 创建：`scripts/collection_orchestrator.py`
- 测试：`tests/document_collection/test_replay.py`

**实现：**

- [ ] 按 `observe -> plan -> act -> verify -> checkpoint -> continue/stop` 实现循环。
- [ ] Manager 只读取当前 `RunContext`、任务状态、待处理队列、验证器发现的问题和策略快照。
- [ ] Manager 调用能力工具，不调用 Skill 内部私有函数。
- [ ] 增加以下硬停止条件：`quota_met`、`source_exhausted`、`needs_human_decision`、`budget_exceeded`、`max_steps`、`time_limit` 和 `verification_failed`。
- [ ] 增加每次运行有界的成本和 token 账本。
- [ ] 在批量采集、全文 LLM 评审、OCR/视觉兜底和最终接受前增加审批闸门。
- [ ] 增加恢复策略：
  - 对临时数据源失败重试；
  - 在 capability manifest 允许时切换抽取器；
  - 隔离格式损坏的 artifact；
  - 对不支持格式或冲突规则请求人工复核。
- [ ] Agent 绝不能原地修改策略快照。
- [ ] 把 Manager 的计划作为事件持久化，但以工具输出和验证器结果为权威。

**验证：**

- Manager 在每个夹具上都能到达终止状态。
- 注入临时失败后会重试，而不是无限循环。
- 注入硬规则失败后不会接受文档。
- 修改软配额后会重新排序，但不会改变硬失败结果。
- 停止后的运行可以继续，且不会重复已经完成的副作用。

**闸门：** 整个过程可以由 Agent 驱动，而不是依赖固定写死的阶段图。

## Task 13：增加报告生成和人工复核交接

**文件：**

- 创建：`scripts/collection_report.py`
- 创建：`docs/document-collection-operator-guide.md`
- 测试：`tests/document_collection/test_auditing.py`

**每次运行必须输出：**

```text
run_manifest.json
requirement_snapshot.json
source_inventory.jsonl
source_type_counts.csv
acquisition_report.jsonl
extraction_report.jsonl
dedup_clusters.csv
accepted_documents.csv
rejected_documents.csv
human_review_queue.csv
query_records.jsonl
query_evidence.jsonl
coverage_report.md
cost_and_runtime.json
```

**实现：**

- [ ] 每份报告都包含准确的数据源文件夹名称。
- [ ] 分开统计原始候选数、去重后的候选数和最终接受数。
- [ ] 提供 reason code 分布，以及按语言/学科的覆盖情况。
- [ ] 包含所有 pending 和 blocked 条目；不能把它们隐藏成 rejected。
- [ ] 提供可以在程序外编辑、再导入的复核队列，导入时记录 reviewer、时间、决策和备注。
- [ ] 不替换 Agent 决策；在当前策略快照下追加一条人工决策事件，使其在当前运行中生效。
- [ ] 除非明确请求，日常摘要报告不包含原始文档正文。

**闸门：** 外部评审人员不需要打开 Agent 对话记录，仅凭报告就能还原最终数量和接受决策。

## Task 14：在 CLI/MCP 验收后再增加可选 PySide6 集成

**文件：**

- 创建：`workers/document_collection_worker.py`
- 创建：`app/widgets/document_collection_page.py`
- 仅在 Pilot 通过后修改：`app/widgets/workflow_pages.py`、`app/main_window.py`、`app/workflow_session.py`
- 测试：`tests/document_collection/test_gui_adapter.py`

**实现：**

- [ ] Worker 只是围绕 Core service 或 CLI 的薄 QThread 包装。
- [ ] 复用现有 `WorkflowSession`/`LogDrawer` 模式，不引入第二套日志系统。
- [ ] 暴露开始、暂停、恢复、停止、打开输出和打开复核队列操作。
- [ ] Qt widget 中不能放筛选或解析逻辑。
- [ ] 保留现有工作流页面；合入前测试紧凑尺寸和原生布局。

**闸门：** GUI 是已经测试过的 Core/MCP 路径的客户端，而不是第二套实现。

## Task 15：增加第二宿主兼容性和可选 Plugin 包装

**文件：**

- 只有确认目标宿主后才创建：`skills/<confirmed-host>/SKILL.md`
- 创建：`plugins/<host>/README.md`
- 测试：`tests/document_collection/test_cross_host_contract.py`

**实现：**

- [ ] 使用同一个 MCP Server 和能力 Schema。
- [ ] 宿主专用 Skill 只保留安装、工具发现和平台约定说明。
- [ ] 不得在 Plugin 中复制 Core 策略或解析代码。
- [ ] 增加兼容性矩阵，记录宿主、transport、认证、resource 读取、审批和已知限制。
- [ ] 只有同一批场景已经在不依赖宿主特性的 MCP 路径上通过后，才能打包 Plugin wrapper。

**闸门：** 至少两个 Agent 宿主可以针对同一个 MCP Server 运行相同的 inventory 和 query 场景，并产生等价的结构化产物。

---

## 5. 排期和发布闸门

以下是按 1 名开发者 + 1 名兼职业务评审人员估算的初始工作日排期。假设飞书授权已经可用，不包含未解决业务决策所需的时间。

| 日期 | 阶段 | 主要交付物 | 出口条件 |
|-|-|-|-|
| 2026-09-10 - 09-11 | 需求与基线 | 需求快照、核对表、分支/worktree 基线、开放决策 | revision 和范围获批准 |
| 2026-09-14 - 09-18 | 契约与状态 | Core 契约、策略加载器、SQLite 账本、产物存储、事件日志 | 契约/状态测试通过 |
| 2026-09-21 - 09-23 | Inventory | 飞书与本地适配器、递归盘点、回放夹具 | 两个文件夹仅 inventory 运行成功 |
| 2026-09-24 - 09-29 | 采集 | 下载/导出、重试、限流、检查点 | 只读采集 Pilot 成功 |
| 2026-09-30 - 10-06 | 抽取 | PDF/DOCX/PPTX/XLSX/CSV/TXT/MD 适配器、来源定位 | 夹具抽取和定位测试通过 |
| 2026-10-07 - 10-09 | 去重与硬筛选 | 精确/规范化去重、硬门槛、reason code | 预置违规项零错误放行 |
| 2026-10-12 - 10-14 | 语义评审 | Agent 辅助的教育属性/学科/材料分类 | 评审输出有证据支持 |
| 2026-10-15 - 10-19 | Query 能力 | 独立 query Skill/Tool、Schema 校验、证据 | 单文档 query 场景通过 |
| 2026-10-20 - 10-22 | MCP 集成 | MCP Server、resource、错误映射、health/resume | 一个宿主能调用所有稳定能力 |
| 2026-10-23 - 10-27 | Manager Loop | 通用 Agent 循环、审批、停止条件、恢复 | 回放和故障注入测试通过 |
| 2026-10-28 - 11-03 | Pilot | 覆盖两个源的分层 100–200 个资源 Pilot | 达到 Go/No-Go 指标或完成记录 |
| 2026-11-04 - 11-06 | 稳定化 | 修复、报告、操作指南、跨宿主冒烟 | Pilot 审计通过 |
| 2026-11-09 起 | 可选 UI/Plugin | PySide6 页面和宿主 wrapper | 仅在 Core release 稳定后开始 |

### 5.1 发布闸门

- **Gate A：契约就绪：** 所有输入、输出和错误都有 Schema 与夹具。
- **Gate B：Inventory 就绪：** source snapshot 完整且可回放。
- **Gate C：采集就绪：** 只读下载/导出可恢复并受限流控制。
- **Gate D：筛选就绪：** 预置硬规则违规项的错误放行数为零。
- **Gate E：Skill 就绪：** 每个能力都能由 Agent 调用，并产出结构化 artifact。
- **Gate F：编排就绪：** Manager Loop 能处理重试、审批、停止和恢复。
- **Gate G：Pilot 就绪：** 两个数据源、相关格式和代表性语言都被覆盖。
- **Gate H：生产就绪：** 审计报告、保留规则、运行手册和回滚路径均获批准。

## 6. 验证和评估方案

### 6.1 确定性单元测试

- 契约序列化和验证。
- 策略哈希和 revision 处理。
- SQLite 事务和迁移测试。
- 产物原子写入和校验和验证。
- 分页、递归和格式错误源响应。
- MIME sniffing 和大小限制。
- 页码/章节/工作表定位保留。
- 精确去重和规范化去重。
- 硬规则评估器。
- Query 数量/题型/题量校验。
- 报告总数和 reason code 聚合。

### 6.2 录制的集成测试

使用经过脱敏、录制的 Lark 响应测试以下情况：

- 首页和 continuation page；
- 嵌套文件夹；
- 在线 Docx/Sheet/Slides；
- 缺少字段或未知字段；
- 429、5xx、超时和格式错误 JSON；
- 两次 snapshot 之间发生变化的资源。

录制测试只能证明适配器行为，不能证明当前在线授权仍然有效。真实访问必须单独报告。

### 6.3 Agent 评估测试

创建 `agent_eval_cases.jsonl`，字段示例：

```json
{
  "case_id": "inventory_nested_folder_001",
  "goal": "在不修改两个授权数据源的前提下完成盘点",
  "allowed_tools": ["collection_inventory"],
  "required_artifacts": ["source_inventory.jsonl"],
  "forbidden_actions": ["drive.delete", "drive.move", "drive.rename"],
  "must_verify": ["pagination_complete", "source_names_exact"],
  "terminal_condition": "completed"
}
```

评估以下指标：

- 工具选择是否正确；
- Schema 是否有效；
- 是否避免禁止的副作用；
- 证据是否完整；
- 是否正确升级歧义；
- 注入失败后能否恢复；
- 重复运行时最终 artifact 是否稳定。

### 6.4 Pilot 验证

Pilot 必须按以下维度分层：

- 源文件夹；
- 语言；
- 主要文件类型；
- 在线资源与本地资源；
- 短文档与长文档；
- 纯文本与表格/公式密集内容；
- 疑似重复项与唯一项。

收集以下指标：

```text
inventory 完整度
采集成功率
按格式统计的抽取成功率
定位完整度
未知 MIME 比例
旧版 DOC/PPT 转换率
精确重复率
近重复复核精度
硬规则错误放行数
语义评审一致性样本
Query Schema 通过率
Query 证据覆盖率
单条目平均耗时
重试率
每份接受文档的 LLM token/成本
每份接受文档的人工复核分钟数
恢复运行成功率
```

需求没有定义的业务质量阈值，不应提前擅自设定。当前先定义工程闸门；语义验收阈值应在第一批带标签的人工复核样本后确定。

### 6.5 回归命令

每完成一个功能任务都运行：

```powershell
.venv\Scripts\python -m pytest tests\document_collection -q
.venv\Scripts\python -m compileall -q core\document_collection mcp_server scripts tests
```

Pilot 之前运行：

```powershell
.venv\Scripts\python -m pytest -q
.venv\Scripts\python scripts\collection_cli.py validate-policy --policy config\collection_policy.example.json
.venv\Scripts\python scripts\collection_replay.py --fixture tests\document_collection\fixtures
git diff --check
```

如果没有实际调用外部服务，结果必须标注为结构/回放验证，而不是在线集成验证。

---

## 7. 安全、隐私和运行控制

- 使用明确的用户身份和现有已授权 CLI 路径；不要把凭据复制到策略文件。
- 对事件日志和测试夹具中的访问 token、签名 URL 和个人标识进行脱敏。
- 将源文件 artifact 和抽取文本保存到已配置的本地目录，并记录保留策略。
- 为 inventory、采集、抽取和确定性去重提供 `--no-llm` 模式。
- 为测试提供 `--no-network` 回放模式。
- 全文 LLM 评审、OCR/视觉处理和批量采集必须经过审批。
- 设置每次运行的成本、字节数、时间和步骤预算。
- 增加 kill switch：停止新增副作用，但允许正在进行的原子写入完成。
- 每一个 LLM 辅助决策都保存模型名、prompt 版本和策略哈希。
- 在 artifact reference 足够的情况下，不要把原始文档内容放入普通 Agent 对话历史。
- 定义已下载文件和抽取文本在删除前保留多久；这是业务/安全决策，不能用实现默认值替代。

---

## 8. 开放决策和缺失需求

以下事项目前缺失或不够明确，必须在受影响的任务开始前解决：

1. **需求权威性：** 多格式文档仍标记为 draft v0.2。确认 revision 1140 是否对本次采集运行具有约束力。
2. **旧版 Office 支持：** 确认当前是否必须支持 `.doc` 和 `.ppt`，以及是否批准使用 LibreOffice、Microsoft Word COM 或其他转换器。
3. **在线导出契约：** 定义在线 Docx、Sheet 和 Slides 的导出方式，以及什么大小可以视为权威文件大小。
4. **MIME 和格式策略：** 解决 PPT/PPTX 源统计与当前只列出 `.pptx` 大小规则之间的差异。
5. **全文理解后端：** 确认由哪个服务/模型提供全文理解，以及长文档成本如何预算。
6. **图像理解配额：** 确认“每账号每周 3 次”的限制是否仍然有效，以及配额耗尽如何表示。
7. **模型/供应商策略：** 选择允许的供应商、fallback 顺序、最大成本、超时、并发，以及是否允许保留原始 prompt/response。
8. **语义验收阈值：** 定义把文档判定为大学层级、学科有效，以及足以生成 10 条互不重复 query 所需的最低证据。
9. **人工复核责任：** 指定复核人员、复核入口、响应时限，以及对歧义语言、阿拉伯语版式和重复簇的升级路径。
10. **数据保留和版权：** 定义保留周期、本地存储位置、访问控制，以及抽取文本是否可以发送给外部 LLM 服务商。
11. **跨平台矩阵：** 确认首批必须调用 MCP Server 的两个 Agent 宿主；不要为未使用的宿主提前开发 Plugin wrapper。
12. **部署目标：** 确认只在本机运行、共享 Windows 主机运行，还是部署为远程服务。这会决定是否需要 HTTP transport 和服务账号。
13. **数据源监控：** 确认系统是否需要定时发现云盘中新建/修改条目，还是只处理显式指定的 snapshot。
14. **Query 证据格式：** 确认最终交付需要原文引用、哈希加定位，还是脱敏片段。
15. **配额可行性：** 确认 P0 配额是否必须硬性交付，即使某种语言不足 20 份高质量文档也不能降低标准。系统必须报告不可行，而不是用低质量文档填充。
16. **失败处理策略：** 确认未解决候选是留待后续运行，还是在当前发布中永久排除。
17. **产物格式：** 确认标准交付是 JSONL/CSV 加 Excel，还是还必须生成飞书文档/Sheet。
18. **操作员权限：** 定义哪些操作需要用户批准：批量采集、OCR/视觉、全文 LLM、最终接受和对外共享。

---

## 9. 风险和缓解措施

| 风险 | 影响 | 缓解措施 |
|-|-|-|
| 运行过程中在线需求发生变化 | 输出不一致 | 每次运行冻结需求快照和策略哈希 |
| Agent 作出看似合理但没有依据的决定 | 错误数据进入最终集合 | 确定性硬门槛、证据要求和人工复核状态 |
| 把 MCP 会话误当成持久化状态 | 工作丢失或重复 | 由 SQLite/产物账本持有状态，MCP 只负责传输调用 |
| OpenClaw 或其他宿主改变 Skill/Plugin 行为 | 集成中断 | MCP 契约测试和宿主专用薄 wrapper |
| 无法解析 `.doc`/`.ppt` | 候选丢失或错误接受 | 明确的转换适配器和 `conversion_required` 状态 |
| PDF 文本抽取丢失页面结构 | Query 证据无法审计 | 保留来源定位的抽取契约和定位测试 |
| LLM 供应商限流或成本飙升 | 运行停滞或过于昂贵 | 共享限流器、预算、重试分类和确定性兜底 |
| 近重复算法合并过度 | 唯一文档被丢弃 | 复核簇和可逆的 canonical 选择 |
| GUI 和 CLI 分叉 | 出现两套不兼容实现 | GUI 只在 CLI/MCP 验收后调用 Core service |
| 把脏工作树混入功能 | 回归难以审查 | 明确基线/worktree 和逐任务提交 |
| 敏感源文本泄露到日志 | 安全/合规问题 | 只使用 artifact 引用、脱敏、日常日志不写原文 |

---

## 10. 完成定义

只有在以下条件全部满足时，第一版才算完成：

- 版本化需求快照可以生成经过验证的策略。
- 两个授权文件夹都可以递归、只读地完成 inventory。
- 源 inventory 可回放、可审计。
- 采集和处理可恢复、幂等。
- 主要支持格式可以产出保留来源定位的抽取结果。
- 硬门槛、去重、语义评审和排序可以产生有理由的决策。
- 每项能力都可以分别通过 CLI 和 MCP 调用。
- 至少一个 Agent 宿主可以调用所有已发布能力，并产出结构化 artifact。
- Manager Loop 可以从临时失败中恢复，并在需要人工决定时停止。
- Query 生成满足当前的整篇文档 query 策略，并将内部证据单独保存。
- 报告能够区分原始候选、去重后候选、已接受文档、已拒绝文档和待复核项。
- 安全、保留、成本和审批策略已经文档化。
- 100–200 个资源的 Pilot 通过工程闸门，并产出一份记录业务质量后续问题的清单。
- 可选 GUI/Plugin 集成不属于 Core 完成定义；它们可以在 Core release 后继续增加。

---

## 11. 实现时需要查阅的官方技术参考

- Model Context Protocol 架构与生命周期：用于设计跨平台的 Tool/Resource/Prompt 边界，不用于持久化状态。
- 官方 MCP Python SDK：使用受支持的 Python Server API 和 transport 指引，不自行发明 MCP 适配器。
- OpenClaw Skills 文档：确保 Skill 文件聚焦于指令，并包含必要的平台约定。
- OpenClaw Plugin SDK 文档：仅用于宿主包装，不把采集 Core 放进 Plugin。
- OpenClaw MCP 文档：用于宿主集成和工具策略要求。
- OpenAI Harness Engineering：用于仓库/系统事实源、可读性、反馈循环和环境设计。
- Anthropic 长时运行 Agent Harness 与 Loop Engineering：用于上下文交接、有界循环、停止条件和评估驱动的工作方式。
- LangGraph durable execution 文档：只有在第一轮 Pilot 后通用循环确实需要第三方 checkpoint/runtime 实现时，才评估是否引入。
- 当前仓库的 `AGENTS.md`、`README.md` 以及现有 `core/`/`workers/` 边界：用于避免破坏现有 GUI 和 crawler/query 流程。

---

## 12. 执行顺序摘要

```text
冻结需求和基线
  -> 定义契约和状态
  -> Inventory 能力
  -> 采集能力
  -> 抽取能力
  -> 去重和硬筛选
  -> 语义评审能力
  -> Query 能力
  -> MCP Server
  -> 单宿主 Skill 冒烟测试
  -> 通用 Manager Loop
  -> Pilot 和审计
  -> 可选 GUI 和 Plugin wrapper
```

第一项实际实现目标是 **仅完成 inventory**，不是直接实现完整流水线，也不是先做 GUI。第一项不可逆的设计决策是能力契约和运行账本；在增加更多 Skill 之前，必须先完成评审。
