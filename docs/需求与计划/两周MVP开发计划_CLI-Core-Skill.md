# 两周 MVP 开发计划：CLI + Core + Skill

> **状态：讨论版，不代表已经开始实施。**
>
> 本计划针对两周时间窗口，目标是先交付一个真实可运行的文档采集与筛选 MVP。CLI、Core、Skill 为本阶段必做；MCP、Plugin 和 Agent 自动编排只保留接口与设计余量，不在本阶段实现。
>
> **计划保存位置：** 当前工作目录 `Ella知识库AI生题数据集`
>
> **实施代码目标仓库：** `D:\Users\admin\Desktop\My_Project\Ella知识库AI生题数据集`

---

## 1. 本阶段目标

### 1.1 目标

在 2026 年 9 月 10 日至 9 月 23 日的两个工作周内，交付一条可运行、只读、可复核的文档采集与筛选流程：

```text
两个飞书云盘文件夹
  -> 递归盘点
  -> 合并计数
  -> 统一去重
  -> 只读下载/导出
  -> 内容抽取
  -> 硬规则筛选
  -> 简单语义复核
  -> Query 生成试跑
  -> 报告输出
```

### 1.2 本阶段不承诺

本阶段不承诺以下内容：

- 全量候选文档都完成高质量终选；
- 所有 Agent 平台都完成接入；
- MCP Server 已上线；
- Plugin 已完成；
- Agent 已经能够自主编排全部流程；
- 所有旧 Office 格式都已转换；
- 所有扫描 PDF 都已 OCR；
- GUI 已完成；
- 定时监控云盘新文件已完成。

### 1.3 建议交付目标

- 两个授权文件夹完成完整 inventory；
- 两个来源合并后统一去重；
- 主要文档格式完成只读采集和基础抽取；
- 全部候选可以做第一轮确定性硬筛选；
- 60–100 份样本完成深度筛选试跑；
- 一批通过筛选的文档完成 Query 生成试跑；
- 一个可运行的 CLI；
- 一个可被 Agent 调用的 Skill；
- 为未来 MCP/Plugin/Agent Loop 保留稳定接口。

---

## 2. 设计原则

### 2.1 Core 负责执行，Agent 负责使用

```text
Core：真正处理文件、规则、状态和结果
CLI：提供稳定的命令行入口
Skill：告诉 Agent 何时调用哪个能力
MCP：未来把能力暴露给不同 Agent 平台
Plugin：未来适配具体平台的外壳
```

两周内不让 Agent 自己发明整个工作流，而是先使用固定、可测试的流程：

```text
inventory
  -> acquire
  -> extract
  -> screen
  -> deduplicate
  -> query
  -> report
```

### 2.2 先确定性，后智能化

文件大小、格式、下载状态、哈希、重复关系等内容由程序判断；教育属性、学科和 Query 可生成性等语义问题再交给模型。

原则是：

> Agent 负责选择和解释，Core 负责执行和证明。

### 2.3 先留契约，不提前实现复杂基础设施

MCP、Plugin 和 Agent Loop 未来要接入，但本阶段只完成：

- 稳定输入结构；
- 稳定输出结构；
- 能力名称；
- 状态名称；
- 副作用说明；
- `next_action` 和 `needs_review` 字段。

不提前引入复杂编排框架。

---

## 3. 本阶段需要优化什么

### 3.1 优化目标

在两周内最大化以下结果：

- 实际跑通的采集能力；
- 可复核的合格候选数量；
- 业务人员可以理解的筛选报告；
- 后续接入其他 Agent 平台时的复用程度。

### 3.2 不可违反的硬约束

1. 不删除、移动、重命名或修改飞书云盘源文件；
2. 两个文件夹必须合并后统一去重；
3. 文件大小限制不能被模型覆盖；
4. 不支持或无法解析的文件不能伪装成成功；
5. 每个结果必须保留源文件夹、路径和原始链接；
6. Query 必须面向整篇文档，不能写片段、章节、页码、Sheet 或表格；
7. 运行结果必须能重新查看和导出。

### 3.3 可以调整的变量

- 第一阶段支持的文件格式范围；
- 深度采集的样本量；
- LLM 只处理全部候选还是只处理边界候选；
- 是否立即接 MCP；
- 是否使用在线资源导出；
- 近重复判断的复杂程度；
- 是否做 GUI；
- 本地状态保存的详细程度。

### 3.4 看起来合理但本阶段不采用的方案

| 方案 | 不采用原因 |
|-|-|
| 一开始先做 Plugin | Plugin 只是平台外壳，不能替代采集和筛选 Core |
| 一开始先做 Agent 自动编排 | 出错时很难定位是 Agent、解析器还是业务规则问题 |
| 所有文档都交给 LLM 判断 | 成本高、速度慢、结果不稳定，硬规则也难审计 |
| 直接改现有 GUI 流程 | 会把新旧流程耦合，影响现有工具稳定性 |
| 先做完整 MCP Server | CLI/Core 契约还没验证，过早接入会增加返工 |
| 先做复杂近重复算法 | 两周内难以证明误合并风险可接受 |
| 为所有旧格式强行找转换方案 | 依赖本机 Office 或转换器，容易阻塞主流程 |

### 3.5 最小区分实验

建议先抽取 60 份样本：

- `知识库_2026` 取 30 份；
- `docQA.llm` 取 30 份；
- 尽量覆盖 PDF、TXT、DOCX、PPTX、在线资源和不同语言。

用这 60 份样本验证：

- inventory 是否完整；
- 采集/导出是否稳定；
- 主要格式能否抽取；
- 硬筛选是否正确；
- 去重是否可解释；
- LLM 复核成本和耗时是否可接受；
- Query 是否能稳定生成 10 条。

### 3.6 淘汰指标

出现以下情况时，应立即缩减格式或功能范围：

- 任何源文件被修改；
- 超大小文件被错误接受；
- 精确重复被重复计入；
- 支持格式大量无法抽取；
- 模型频繁输出无法验证的结果；
- 人工复核时间明显失控；
- Query 大量不符合数量或格式要求。

当前结论是：**CLI + Core + Skill 是较优假设，尚未经过样本验证，不能称为最优方案。**

---

## 4. 技术选型

| 层 | 选择 | 原因 | 本阶段不选 |
|-|-|-|-|
| Core | Python 3.12 + 标准库优先 | 兼容现有项目，依赖少，Windows 容易运行 | LangGraph、Temporal、Airflow |
| CLI | `argparse` | Python 内置，命令清晰，不增加框架依赖 | 新增 CLI 框架 |
| 数据状态 | SQLite | 本地可恢复，支持查询，Python 内置 | 分布式数据库、事件溯源 |
| 交换文件 | JSONL、CSV、Markdown | 易检查、易导入、易被 Agent 读取 | 只保存在内存 |
| 云盘访问 | 现有已授权 `lark-cli` 路径的适配器 | 复用已有授权和命令行能力 | 两周内重新开发飞书 API Client |
| 文件下载 | 顺序下载，最多 1–2 并发 | 稳定优先，减少限流和调试成本 | 复杂调度器 |
| 文本抽取 | PyMuPDF、python-docx、openpyxl、python-pptx、标准库 csv | 复用已有依赖，覆盖主要格式 | 为所有格式引入重型框架 |
| LLM | 统一 `LLMAdapter`，内部复用现有 litellm 配置 | 让筛选和 Query 共用接口 | Core 直接调用现有私有函数 |
| Skill | `SKILL.md` + `capability-manifest.json` | Agent 可读，后续可映射 MCP | 把业务逻辑塞进 Skill |
| MCP | 本阶段不实现，仅留契约 | 避免未验证的接口返工 | 现在就接多个宿主 |
| Plugin | 本阶段不实现，仅留 README/目录 | Plugin 依赖具体宿主 | 先做通用 Plugin |
| GUI | 不实现 | CLI/Core 验收后再接入 | 直接修改现有 PySide6 工作流 |
| 测试 | pytest + 本地夹具 + 录制响应 | 不依赖真实云盘也能重复测试 | 只做人工点击测试 |

---

## 5. 目标目录结构

实现代码位于现有工具仓库：

```text
D:\Users\admin\Desktop\My_Project\Ella知识库AI生题数据集
```

新增：

```text
core/document_collection/
    __init__.py
    contracts.py          # 统一输入、输出、状态、证据结构
    config.py             # 简单 JSON 配置加载和校验
    service.py            # CoreService 门面
    inventory.py          # 递归盘点和合并统计
    acquisition.py        # 下载、导出、重试、哈希
    extraction.py         # 格式识别和抽取调度
    screening.py          # 硬规则和语义复核
    deduplication.py      # 精确/规范化去重
    query_generation.py   # Query 生成和校验
    reporting.py          # 报告生成
    adapters/
        __init__.py
        lark_cli.py       # 现有 lark-cli 适配器
        local_files.py    # 本地文件夹适配器
    extractors/
        __init__.py
        pdf.py
        docx.py
        pptx.py
        xlsx.py
        text.py

scripts/
    collection_cli.py

skills/document_collection/
    SKILL.md
    capability-manifest.json
    examples/

mcp_server/
    README.md             # 仅记录未来 MCP 适配设计，不实现 Server

plugins/
    README.md             # 仅记录未来 Plugin 适配规则

tests/document_collection/
    conftest.py
    fixtures/
        lark_drive_pages/
        documents/
        expected/
    test_contracts.py
    test_config.py
    test_inventory.py
    test_acquisition.py
    test_extraction.py
    test_screening.py
    test_deduplication.py
    test_query_generation.py
    test_cli.py
    test_skill_scenarios.py
```

第一阶段不能修改以下现有模块的核心业务逻辑：

```text
core/datasource.py
core/doc_processor.py
app/main_window.py
app/workflow_session.py
workers/
```

如果确认已有函数可以复用，应通过适配器调用；不直接依赖私有函数，也不把新业务逻辑塞进 GUI。

---

## 6. 统一能力契约

### 6.1 配置输入

```python
@dataclass(frozen=True)
class RunConfig:
    run_id: str
    source_folders: tuple[str, ...]
    allowed_extensions: tuple[str, ...]
    max_size_mb: dict[str, int]
    target_languages: tuple[str, ...]
    queries_per_document: int
    dry_run: bool
    approval_mode: str
    llm_enabled: bool
```

### 6.2 资源记录

```python
@dataclass(frozen=True)
class SourceItem:
    source_name: str
    source_folder_path: str
    item_token: str
    name: str
    item_type: str
    extension: str | None
    mime_type: str | None
    remote_size: int | None
    remote_modified_time: str | None
    url: str | None
    metadata: dict
```

### 6.3 统一结果

```python
@dataclass(frozen=True)
class RunResult:
    status: str
    run_id: str
    summary: dict
    artifacts: tuple[dict, ...]
    review_items: tuple[dict, ...]
    errors: tuple[dict, ...]
    next_action: str | None
```

### 6.4 状态值

```text
completed       已完成
partial         部分完成
needs_review    需要人工复核
failed          失败
cancelled       已取消
```

### 6.5 能力名称

```text
collection.inventory
collection.acquire
collection.extract
collection.screen
collection.deduplicate
collection.generate_queries
collection.report
collection.resume
```

CLI 命令和未来 MCP Tool 一一对应：

| 能力名称 | 当前 CLI | 未来 MCP Tool |
|-|-|-|
| `collection.inventory` | `inventory` | `collection_inventory` |
| `collection.acquire` | `acquire` | `collection_acquire` |
| `collection.extract` | `extract` | `collection_extract` |
| `collection.screen` | `screen` | `collection_screen` |
| `collection.deduplicate` | `deduplicate` | `collection_deduplicate` |
| `collection.generate_queries` | `query` | `collection_generate_queries` |
| `collection.report` | `report` | `collection_report` |
| `collection.resume` | `resume` | `collection_resume` |

### 6.6 副作用声明

每项能力在 manifest 中声明允许的副作用：

```json
{
  "name": "collection.inventory",
  "version": "0.1",
  "cli_command": "inventory",
  "future_mcp_tool": "collection_inventory",
  "side_effects": [
    "read_source",
    "write_local"
  ]
}
```

本阶段所有能力都必须明确写出：

- 是否读取云盘；
- 是否写本地文件；
- 是否调用 LLM；
- 是否可能需要人工复核；
- 是否可以重试。

---

## 7. 详细开发流程

## Task 0：确认基线和最小范围

### 工作内容

- 记录目标代码仓库当前 `git status`、当前 commit、Python 版本和依赖版本；
- 确认两个源文件夹和当前需求 revision；
- 从当前需求生成一份简单的 `collection_policy.json`；
- 确认第一批支持格式；
- 确认本阶段只读；
- 使用 `lark-cli --help` 和相关子命令的 help 核实调用参数，不猜接口。

### 产出

```text
docs/document_collection_scope.md
config/collection_policy.json
```

### 验证

- 需求 revision 有记录；
- 配置可以被 JSON 读取；
- 云盘操作范围明确为只读；
- 不修改现有业务代码。

---

## Task 1：创建 Core、CLI、Skill 骨架

### 工作内容

创建：

```text
core/document_collection/
scripts/collection_cli.py
skills/document_collection/SKILL.md
skills/document_collection/capability-manifest.json
```

实现：

- `CoreService` 空接口；
- `RunConfig`、`SourceItem`、`RunResult`；
- CLI 的 `--help`、`--version`；
- `validate-config` 命令；
- Skill 的触发条件、禁止动作和停止条件；
- manifest 中的能力名称和版本。

### 验证

```powershell
.venv\Scripts\python scripts\collection_cli.py --help
.venv\Scripts\python scripts\collection_cli.py validate-config --config config\collection_policy.json
.venv\Scripts\python -m pytest tests\document_collection\test_contracts.py -q
```

### 通过标准

CLI 可以启动，Core 可以导入，Skill 和 manifest 结构完整。

---

## Task 2：实现 inventory

### 工作内容

- 增加 `lark_cli.py`；
- 增加 `local_files.py`；
- 递归列举两个云盘文件夹；
- 记录准确的源文件夹名称和路径；
- 处理分页和嵌套文件夹；
- 区分普通文件、在线 Docx、Sheet、Slides、快捷方式和未知类型；
- 不下载文件正文；
- 输出原始 inventory 和类型统计；
- 合并两个来源后计算去重前后数量。

### 产出

```text
run_manifest.json
source_inventory.jsonl
source_type_counts.csv
inventory_summary.md
```

### 验证

- 使用录制响应验证分页；
- 使用本地文件夹适配器验证后续流程；
- 真实调用只允许读取和列举；
- 两个源文件夹名称在报告中清楚显示；
- 同一 item token 不重复计入。

---

## Task 3：实现只读采集和本地产物

### 工作内容

- 下载普通文件；
- 对在线资源执行允许的导出；
- 导出失败时标记 `pending_export`；
- 使用临时文件，成功后再原子重命名；
- 计算 SHA-256；
- 记录文件大小、媒体类型和来源；
- 默认顺序处理或最多 1–2 并发；
- 每个文件最多重试 3 次；
- 支持中断后继续。

### 产出

```text
artifacts/<sha256>
acquisition_report.jsonl
```

### 验证

模拟：

- 429；
- 5xx；
- 超时；
- 下载截断；
- 校验和错误；
- 磁盘空间不足。

禁止出现：

- 重复半成品；
- 记录访问 token；
- 删除、移动或重命名源文件。

---

## Task 4：实现格式识别和内容抽取

### 第一优先级

- PDF；
- TXT；
- MD；
- DOCX。

### 第二优先级

- PPTX；
- XLSX；
- CSV。

### 待处理类型

- DOC；
- PPT；
- 在线 Sheet；
- 在线 Slides；
- 扫描 PDF；
- 图像主导资源。

### 工作内容

- 结合文件签名和扩展名识别 MIME；
- 记录字符数和词数；
- PDF 保留页码；
- DOCX 保留段落和表格顺序；
- PPTX 保留幻灯片编号；
- XLSX 保留工作簿、Sheet 和单元格范围；
- TXT/MD 保留行号范围；
- 解析失败返回明确错误；
- 旧格式标记为 `conversion_required`，不伪装成成功。

### 验证

- 相同文件重复抽取结果一致；
- 每种已支持格式至少有一个 fixture；
- 抽取失败能进入 `needs_review`；
- 结果包含定位信息。

---

## Task 5：实现硬筛选和基础去重

### 硬筛选规则

按以下顺序执行：

```text
采集成功
  -> 文件类型支持
  -> 文件大小合格
  -> 内容抽取成功
  -> 语言基本符合
  -> 非图形主导
  -> 非精确重复
```

### 去重规则

1. 先比较原始 SHA-256；
2. 再比较规范化文本哈希；
3. 疑似近重复只标记，不自动删除；
4. 保留所有来源路径；
5. 记录推荐保留版本和原因。

### 输出状态

```text
accepted
rejected
needs_review
pending_export
conversion_required
```

### 验证

- 超大小文件不能通过；
- 精确重复不能重复进入候选；
- 近重复不会被静默删除；
- 每个决定有 reason code。

---

## Task 6：实现简单语义复核

### 只把这些问题交给 LLM

- 是否属于大学/高等教育材料；
- 学科分类；
- 是否具有足够内容生成 10 条 Query；
- 是否值得进入最终候选；
- 是否需要人工复核。

### LLM 统一接口

```python
class LLMAdapter(Protocol):
    def classify_document(
        self,
        document_ref: dict,
        policy: dict,
    ) -> dict: ...
```

模型只能返回结构化 JSON，至少包含：

```text
education_level
subject
queryability
confidence
evidence
```

输出格式错误时最多修复重试一次，仍失败就进入 `needs_review`。

### 验证

- 硬失败不能被 LLM 覆盖；
- 没有证据的模型判断不能自动接受；
- LLM 失败可恢复；
- 模型调用次数和成本有记录。

---

## Task 7：实现 Query 生成

### 工作内容

- 使用固定的 `RequirementSpec` 和 query 配置；
- 每份文档生成 10 条 Query；
- Query 面向整篇文档；
- 不写片段、章节、页码、Sheet 或表格；
- 题量小于 12；
- 题量尽量不重复；
- Query 语言跟随文档语言；
- 做 JSON、数量、题型、重复和证据校验。

### 产出

```text
query_records.jsonl
query_evidence.jsonl
query_validation_report.md
```

### 验证

先对 10–20 份确认合格文档试跑，再决定是否扩展到更大批次。

---

## Task 8：实现报告和 Skill 场景测试

### 报告

至少输出：

```text
accepted_documents.csv
rejected_documents.csv
human_review_queue.csv
source_type_counts.csv
coverage_report.md
cost_and_runtime.json
```

### Skill 场景

至少覆盖：

1. 只做 inventory；
2. 检查一个候选；
3. 拒绝超大小文件；
4. 未知 MIME 进入人工复核；
5. 采集失败后恢复；
6. 为一份合格文档生成 Query；
7. 拒绝修改云盘源文件的请求。

### 验证

- Skill 不包含数据处理代码；
- Skill 调用能力名称与 manifest 一致；
- 禁止动作明确；
- `needs_review` 能正常停止流程；
- 重复运行 fixture 时结果稳定。

---

## Task 9：预留 MCP 和 Plugin 接口

### MCP

只创建：

```text
mcp_server/README.md
```

README 说明：

- 未来 MCP Tool 与 capability manifest 一一对应；
- MCP 只负责协议适配；
- 状态仍由 Core 的 SQLite 和本地产物负责；
- MCP 不复制筛选业务规则；
- MCP Server 作为可选依赖，不影响 CLI/Core 使用。

### Plugin

只创建：

```text
plugins/README.md
```

README 说明：

- Plugin 只能调用 CLI 或未来 MCP；
- Plugin 不得复制 Core；
- 宿主平台未确认前不创建具体 wrapper；
- 未来每个平台单独验证安装、认证、工具调用和资源读取。

---

## 8. 两周排期

| 日期 | 阶段 | 主要交付 | 出口条件 |
|-|-|-|-|
| 2026-09-10 | 基线与范围 | 配置、契约、支持格式确认 | 范围冻结 |
| 2026-09-11 - 09-12 | CLI/Core/Skill 骨架 | 目录、CLI、manifest、基础测试 | CLI 可以启动 |
| 2026-09-14 - 09-15 | Inventory | 两个文件夹盘点、统计、合并计数 | inventory 可回放 |
| 2026-09-16 - 09-17 | 采集 | 下载/导出、哈希、重试、恢复 | 小批量只读采集成功 |
| 2026-09-18 | 抽取 | PDF/TXT/DOCX 主路径 | fixture 抽取通过 |
| 2026-09-21 | 硬筛选/去重 | 硬规则、精确去重、待复核 | 硬失败零误放行 |
| 2026-09-22 | 语义/Query | LLM 复核、Query 试跑 | 10 条 Query 校验通过 |
| 2026-09-23 | Skill/验收 | Skill 场景、报告、60–100 份 Pilot | 交付物可复核 |

如果实际只有 8 个工作日，优先顺序为：

```text
Inventory
  > 采集
  > 硬筛选
  > 去重
  > 报告
  > Query
  > Skill 场景
  > PPTX/XLSX 深度支持
```

---

## 9. 简化版验收门禁

不采用原计划的 Gate A–H，只保留 5 个检查点。

### 检查点 1：能运行

- CLI 可以启动；
- Core 可以导入；
- Skill 和 manifest 格式正确。

### 检查点 2：盘点完整

- 两个源文件夹都完成递归盘点；
- 分页没有漏项；
- 每行包含准确源文件夹名称。

### 检查点 3：没有误放行

以下情况必须为零：

- 超大小文件被接受；
- 明确不支持格式被接受；
- 源文件被修改；
- 精确重复被重复计入。

### 检查点 4：结果可解释

每个条目至少有：

```text
status
reason_codes
source_path
artifact_path
```

### 检查点 5：可以复跑

同一份 fixture 重跑时：

- 决策一致；
- artifact 哈希一致；
- 报告数量一致；
- 只有时间戳和 run_id 可以不同。

---

## 10. 验证方案

### 10.1 单元测试

- 契约序列化；
- 配置校验；
- 文件大小判断；
- MIME 判断；
- 哈希和去重；
- 状态恢复；
- Query 数量和格式校验；
- 报告数量统计。

### 10.2 录制响应测试

用脱敏的飞书 CLI 响应覆盖：

- 单页目录；
- 多页目录；
- 嵌套目录；
- 空目录；
- 缺少 next token；
- 429；
- 5xx；
- 超时；
- 在线资源导出失败。

### 10.3 本地 Pilot

先使用 60 份样本：

- 两个源各 30 份；
- 覆盖主要格式和语言；
- 记录采集成功率、抽取成功率、待复核比例、去重比例、Query 通过率、人工复核耗时。

### 10.4 回归命令

```powershell
.venv\Scripts\python -m pytest tests\document_collection -q
.venv\Scripts\python -m compileall -q core\document_collection scripts
.venv\Scripts\python scripts\collection_cli.py --help
git diff --check
```

如果没有调用真实在线服务，必须标记为“结构/回放验证”，不能写成“线上集成已验证”。

---

## 11. 未来扩展条件

以下条件满足后，再实现 MCP：

- CLI 命令已经稳定；
- Core 输入输出结构至少经历一次真实 Pilot；
- Skill 场景已经通过；
- 需要第二个 Agent 宿主；
- 目标宿主无法直接调用 CLI。

以下条件满足后，再实现 Agent Loop：

- 单个能力都已经能独立运行；
- `RunResult.next_action` 已稳定；
- `needs_review`、`partial`、`failed` 状态含义清晰；
- 已经收集足够的失败案例；
- 固定流程开始限制效率，而不是流程本身还不稳定。

以下条件满足后，再实现 Plugin：

- 已经确认具体宿主平台；
- MCP 或 CLI 接口稳定；
- 宿主的认证、资源读取和审批机制已经确认；
- Plugin 只需要包装，不需要重新实现业务。

---

## 12. Definition of Done

两周版本完成的标准：

- [ ] 两个授权文件夹可以只读递归盘点；
- [ ] 两个来源可以合并计数并统一去重；
- [ ] CLI 可以运行 inventory、acquire、screen、query、report；
- [ ] Core 不依赖 Qt；
- [ ] Skill 可以指导 Agent 调用上述能力；
- [ ] 主要格式可以完成第一轮采集和抽取；
- [ ] 硬规则错误放行数为零；
- [ ] 每个结果都有来源和理由；
- [ ] 60–100 份样本完成 Pilot；
- [ ] 一批合格文档完成 10 条 Query 生成试跑；
- [ ] 同一 fixture 可以重复运行；
- [ ] MCP、Plugin 和 Agent Loop 的接口文件已经预留；
- [ ] 没有修改现有云盘源文件和既有 GUI 业务逻辑。

---

## 13. 最终结论

本阶段采用：

```text
CLI + Core + Skill：现在实现
MCP：现在定义契约，后续实现
Plugin：现在定义边界，后续实现
Agent Loop：现在预留状态字段，后续实现
GUI：本阶段不动
```

这条路线的核心是：

> 先把能力做成稳定、可测试、可复跑的独立模块；再把这些能力接给不同 Agent 平台；最后才做自动编排。

这样即使后续需求、数据源、模型供应商或 Agent 平台发生变化，主要维护点也集中在适配器、配置和 Skill，而不是整套系统推倒重来。
