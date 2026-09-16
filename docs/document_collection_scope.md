# 文档采集与筛选：基线与范围（Task 0）

> 本文是两周 MVP 的**范围冻结记录**。所有后续实现都以本文为准；
> 需要变更时先改本文，再改代码，避免口径漂移。
>
> 生成时间：2026-09-09　　执行分支：`feat/document-collection-mvp`
> 迁移：2026-09-10 起本项目独立成仓库，第 1 节已按迁移后现状改写，见 `docs/迁移记录_20260910.md`

---

## 1. 代码仓库基线

### 1.1 当前基线（2026-09-10 迁移后）

| 项 | 值 |
|-|-|
| 仓库 | `D:\Users\admin\Desktop\My_Project\Ella知识库AI生题数据集`（本项目专用，不再与其他项目共用工作区） |
| 分支 | `main` |
| 工作区状态 | 干净；`output/`、`.venv/`、`config/llm_config.json`、测试夹具文档由 `.gitignore` 排除 |
| 提交方式 | 直接在本仓库提交 |

### 1.2 迁移前基线（历史记录，勿据此操作）

| 项 | 值 |
|-|-|
| 寄居仓库 | `D:\Users\admin\Desktop\My_Project\Ella知识库数据集小工具`（GUI 爬虫工具，另一个项目） |
| 起点 commit | `963dab2` |
| 工作分支 | `feat/document-collection-mvp`，共 12 个提交；已导出为 git bundle 后从原仓库删除 |
| 工作区状态 | 原仓库起点上有大量未提交改动（GUI/workers 等），本任务未触碰、未提交这些文件 |

### 迁移前「本任务不动的既有模块」（历史记录；这些文件仍留在原仓库）

```text
core/datasource.py
core/doc_processor.py
core/smart_crawler.py
core/datasource_wikipedia.py
app/main_window.py
app/workflow_session.py
workers/
```

需要复用其中的能力时，一律通过新建适配器（例如 `core/document_collection/llm.py`），
不直接依赖私有函数（如 `_llm_call`、`_build_full_provider`），
理由：那些函数的签名与重试策略会随 GUI 需求变化，跨层依赖会把新流程绑死。

---

## 2. 运行环境基线（实测）

| 组件 | 版本 | 说明 |
|-|-|-|
| Python（.venv） | 3.12.6 | `.venv\Scripts\python.exe` |
| PyMuPDF | 1.27.2.3 | PDF 抽取 |
| python-docx | 1.2.0 | DOCX 抽取 |
| python-pptx | 1.0.2 | PPTX 抽取（本任务新增安装到 .venv） |
| openpyxl | 3.1.5 | XLSX 抽取 |
| litellm | 1.89.4 | 语义复核与 Query 生成 |
| pytest | 9.1.1 | 本任务新增安装到 .venv |
| ruff | 0.16.6 | 静态检查，本任务新增安装到 .venv |
| lark-cli | 1.0.85 | 全局 npm：`D:\Users\admin\AppData\Roaming\npm\lark-cli.CMD` |
| node | `C:\Program Files\nodejs\node.exe` | 见下面第 4.1 条踩坑记录 |

未使用：xlrd（旧版 .xls 本期直接标 `conversion_required`，不做转换）。

---

## 3. 需求基线（revision 已固定）

| 文档 | token | revision | 用途 |
|-|-|-|-|
| 内部需求文档（已脱敏） | `REDACTED_DOC_039` | **201** | 主口径（本任务所有业务数字的来源） |
| 内部需求文档（已脱敏） | `REDACTED_DOC_074` | 428 | 原始需求 |
| 内部需求文档（已脱敏） | `OtdnwZwPaiOhHfk43mYcpuAOndg` | 1140（draft v0.2） | 4.1.2 字数与大小要求 |

复核方式：

```powershell
lark-cli docs +fetch --doc REDACTED_FEISHU_URL `
  --doc-format markdown --detail full --format json
```

### 3.1 从 revision 201 抄录的关键口径

- P0 语种：英、俄、法、中、阿、西，每种至少 20 份，合计至少 120 份。
- P1 学科：法学、教育学/语言学、临床医学/护理、基础理科，每类至少 1 份，四类合计至少 4 份。
- 大小上限：PDF/DOC/DOCX/PPTX ≤ 100 MB；XLSX/CSV/TXT/MD ≤ 20 MB。超限直接淘汰，
  不通过压缩、拆分或截取规避。
- 在线 Docx/Sheet/Slides 无法确认源文件大小时先标记「待核验」。
- 快速理解只覆盖前 5 万字；超过 5 万字**不淘汰**，必须走全文理解并验证章节/语义单元/页码归属。
- 图文理解单文件 ≤ 20 页、每周每账号 3 次，不作为批量采集默认路径。
- 文字、表格、公式可进入评测；示意图、插图、电路图、受力图等图形主导内容本期排除。
- 每份文档 10 条 query：

  | 类型 | 条数 | 每条题量 | 用途 |
  |-|-|-|-|
  | 选择题 | 2 | 5 | 正常正向样本 |
  | 填空题 | 2 | 3 | 正常正向样本 |
  | 解答题 | 2 | 2 | 按产品规则使用短答 |
  | 判断题 | 2 | 7 | 正常正向样本 |
  | 混合题型 | 2 | 9 | 兼容性样本，标 `compatibility_only`，预期行为「丢弃或降级处理」 |

- 每条 query 题量必须 < 12；同文档内不同题型题量尽量不重复（建议 2、3、5、7、9）。
- query 面向**整篇文档**，不得指定片段、章节、页码、Sheet 或表格；
  定位信息只保留在内部台账用于覆盖检查与审计。
- query 语言跟随文档语言。
- 跨两个文件夹**统一去重**，不能按文件夹分别计数。
- 原始云盘文件保持不变，所有下载、解析、去重、筛选只在本地副本上完成。
- 格式分布 3:2:2:1:1:1（PDF 36 / DOC-DOCX 24 / PPTX 24 / XLSX-CSV 12 / TXT 12 / MD 12）
  是**软目标**，不是硬淘汰条件。

---

## 4. 实测确认的接口事实（不猜接口）

以下每一条都在 2026-09-09 用真实只读调用验证过。

### 4.1 lark-cli 的 Windows 垫片会吃掉分页 token（重要踩坑）

npm 在 Windows 上生成的是 `lark-cli.CMD`。用 `CreateProcess` 执行 `.cmd` 会经 `cmd.exe`
做二次解析，而飞书返回的 `next_page_token` 形如：

```text
v3|wpjCj2XDkR48CwPCjUQiwpBiMcKCw5nCiRAHwoPCq2VMwpXDi8K_wrvCnsO5U8K-w7kkIsOgwq5IZMOFw7LDjcObRSTCmcKZw5xJVDwSwo7CmVfCsMOdwphYwokWwqfDk8KOwos
```

其中的 `|` 被当成管道，导致第 2 页及以后全部失败：

```text
'v3|wpj...' is not recognized as an internal or external command, operable program or batch file.
```

**表现极具欺骗性**：第 1 页正常返回，整体看起来"跑通了"，但每个目录只拿到前 200 条。

解决：绕过垫片，直接执行

```text
node  D:\Users\admin\AppData\Roaming\npm\node_modules\@larksuite\cli\scripts\run.js  <args...>
```

参数由 Python 的 `list2cmdline` 正确引用，不经过任何 shell。
实现见 `core/document_collection/adapters/lark_cli.py::resolve_cli_argv()`，
回归用例 `tests/document_collection/test_adapters.py::test_page_token_with_pipe_is_passed_as_single_argv_element`。

### 4.2 清单与元数据接口都不返回文件大小

```powershell
lark-cli drive files list --folder-token <T> --page-size 5 --format json
lark-cli drive metas batch_query --data '{"request_docs":[{"doc_token":"<T>","doc_type":"file"}],"with_url":true}'
```

两者返回的字段里都**没有** `size` / `size_bytes`：

```text
files[]  : created_time, modified_time, name, owner_id, parent_token, token, type, url
metas[]  : create_time, doc_token, doc_type, latest_modify_time, latest_modify_user,
           owner_id, request_doc_info, title, url
```

结论：`SourceItem.remote_size` 在飞书数据源下只能是 `null`，
**大小门槛改为下载/导出后按本地字节数判定**。这不影响"超限直接淘汰"的结论，
只是判定时点后移；超限文件的本地副本默认删除，只保留大小证据。
回归用例：`test_adapters.py::test_files_list_response_has_no_size_field`。

### 4.3 下载与导出的输出路径必须是相对路径

```text
lark-cli drive +download --file-token <T> --output <PATH>
lark-cli drive +export   --token <T> --doc-type <TYPE> --file-extension <EXT> --output-dir <DIR>
```

传绝对路径会被直接拒绝：

```text
unsafe output path: --output must be a relative path within the current directory
```

结论：适配器统一以 run 工作区作为 `cwd`，并把目标路径转成相对路径。
越界（目标不在工作区内）会主动抛错。
回归用例：`test_adapters.py::test_download_output_is_relative_to_workdir`、
`test_download_target_outside_workdir_is_rejected`。

### 4.4 导出接口返回结构（已验证）

```json
{
  "ok": true,
  "data": {
    "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc_type": "docx",
    "file_extension": "docx",
    "file_name": "文档QA评测方法和标准拟.docx",
    "file_token": "REDACTED_DOC_036",
    "saved_path": "<绝对路径>",
    "size_bytes": 90617,
    "ticket": "7683488905350810839"
  }
}
```

`saved_path` 是绝对路径，可直接用于后续哈希与抽取。
docx 可导出为 `docx` 或 `pdf`；本任务默认导出为可抽取正文的格式（docx→docx、sheet→xlsx、slides→pptx）。

#### 4.4.1 导出产物不具备字节级可复现性（实测）

对同一批 80 条样本跑了两次完整采集（run `pilot-20260909-v4` 与 `pilot-20260909-v5`），
逐个比对 70 个本地产物的 SHA-256：

| 获取方式 | 数量 | SHA-256 一致 | 结论 |
|-|-|-|-|
| `download`（普通文件） | 60 | **60 / 60** | 逐字节可复现 |
| `export`（在线资源导出） | 10 | **0 / 10** | 每次都由服务端重新渲染 |

差异极小但真实存在，例如 `58435` vs `58471` 字节、`282343` vs `282327` 字节、
`82106` vs `82105` 字节。

影响与处理：

1. 「可以复跑」这条验收不能要求导出产物的字节一致，否则永远不通过；
   `audit` 命令把它拆成两条 —— 下载类硬校验，导出类只披露差异。
2. 内容寻址存储下，同一份在线资源两次导出会落成两个不同的 artifact 文件。
   跨运行的去重**必须**依赖规范化正文指纹，不能依赖 SHA-256。
   当前实现两者都做：SHA-256 抓完全相同的文件，正文指纹抓"同内容不同容器"。

#### 4.4.2 在线资源导出的两类失败（实测）

| 错误码 | 消息 | 性质 | 处理 |
|-|-|-|-|
| 600 | `hybrid resource expired` | **部分可重试**：同一份 docx 隔几秒重试即成功；某份 slides 连续重试 3 次仍失败 | 归入可重试，重试耗尽后标 `pending_export` |
| 1069902 | `no permission` | 永久 | 直接标 `pending_export`，需要业务方补授权 |
| 131005 | `not found` | 永久 | 同上 |
| validation | `--sub-id is required when exporting sheet/bitable as csv` | 参数问题 | 策略里 sheet/bitable 一律导出 xlsx，不用 csv |

修复错误分类后，同样 80 条样本的 `pending_export` 从 **19 条降到 1 条**，
剩下那 1 条是 `no permission`，程序重试解决不了。

### 4.5 目录 type 字段的实际取值

实测出现：`folder`、`file`、`docx`、`sheet`、`slides`、`bitable`、`shortcut`。
归一化规则见 `core/document_collection/inventory.py::classify_entry()`，
未知取值一律归 `unknown` 并进人工复核，不猜。

### 4.6 stdout 可能混有进度行

`drive +download` 会先往输出里打一行 `Downloading: xxxx...`，
因此 JSON 解析必须能在文本中定位第一个完整 JSON 对象。
实现见 `lark_cli.py::extract_json()`，用例 `test_extract_json_tolerates_progress_lines`。

### 4.7 Windows MAX_PATH 限制（部署注意）

产物路径形如：

```text
<repo>\output\document_collection\<run_id>\artifacts\<sha256[:2]>\<sha256>.<ext>
```

光是文件名就占 68 个字符。仓库路径稍深就会突破 Windows 传统的 260 字符上限，
报 `WinError 3 系统找不到指定的路径`。这个坑在本项目出现了三次
（item_id 派生名、artifact 落盘名、深层工作区）。

处理方式：

1. 所有派生文件名上限收紧到 64 字符；
2. 新增 `core/document_collection/winpath.py`，对**真正调用 OS 的地方**统一加
   `\\?\` 扩展长度前缀（上限约 32767），写进数据库和报告的路径仍是普通可读形式；
3. `sqlite3.connect` 也走扩展长度路径（SQLite 在 Windows 上用 CreateFileW，支持该前缀）。

**部署建议**：仍然把工作区放在浅路径下。超过 260 字符的文件虽然程序能读写，
但资源管理器和很多第三方工具打不开，人工复核会很痛苦。

### 4.8 授权有效期（运维注意）

```text
identity : user（Azhang2024-dev）
状态     : needs_refresh（下次调用自动刷新）
refresh  : 2026-09-16T18:27:22+08:00 到期
```

**2026-09-16 之后需要重新授权**，否则采集会批量失败。
批量失败时先跑 `lark-cli auth status`，不要盲目重跑。

---

## 5. 采集源基线（真实盘点结果）

盘点命令：

```powershell
.venv\Scripts\python scripts\collection_cli.py --run-id pilot-20260909-v2 inventory
```

结果（`status=completed`，0 错误 0 告警，耗时 74 秒）：

| 源文件夹名称 | 角色 | token | 非文件夹资源 | 子文件夹 |
|-|-|-|-|-|
| 知识库_2026 | primary | `REDACTED_DOC_007` | 3,818 | 见 run_manifest |
| docQA.llm | supplement | `REDACTED_DOC_029` | 1,074 | 见 run_manifest |
| **合计（去重前）** | | | **4,892** | |
| **两源按 token 合并去重后** | | | **4,892**（跨源同 token = 0） | |

### 5.1 每个源文件夹的文件类型数量

| 文件类型 | 知识库_2026 | docQA.llm | 合计（去重前） |
|-|-|-|-|
| PDF | 2,652 | 757 | 3,409 |
| DOC/DOCX | 456 | 68 | 524 |
| 在线 Docx | 0 | 31 | 31 |
| PPT/PPTX | 0 | 20 | 20 |
| XLSX/CSV | 38 | 0 | 38 |
| 在线 Sheet | 8 | 60 | 68 |
| TXT | 48 | 122 | 170 |
| MD | 601 | 0 | 601 |
| 在线 Slides | 10 | 10 | 20 |
| 其他/未知 | 5 | 6 | 11 |
| **合计** | **3,818** | **1,074** | **4,892** |

该表与在线采集计划 1.2 节的表格**逐格一致**，可作为盘点正确性的独立交叉验证。

### 5.2 口径说明

- 以上是**原始候选数量**，不等于最终合格数量；
- token 级合并只处理"同一个资源挂在两个文件夹"的情况；
  "同名不同 token"要等正文抽取后在 `deduplicate` 阶段按内容指纹判定；
- 需求第五章已指出 `docQA.llm` 里的 `french+chinese+russian` 目录与对应语种目录
  存在明显重复，这部分只能靠正文指纹识别，盘点阶段不做猜测。

---

## 6. 第一阶段支持的格式

| 优先级 | 格式 | 抽取器 | 定位信息 |
|-|-|-|-|
| P1 | PDF | PyMuPDF | 页码、每页字符数、图片面积占比 |
| P1 | TXT / MD | 多候选编码解码 | 行号区间、采用的编码 |
| P1 | DOCX | python-docx | 段落序号、样式、表格序号与行列数 |
| P2 | PPTX | python-pptx | 幻灯片编号、表格、备注页 |
| P2 | XLSX | openpyxl（read_only + data_only） | Sheet 名、单元格范围、行列数、是否截断 |
| P2 | CSV | 标准库 csv + 分隔符嗅探 | 行区间、分隔符、编码 |
| 在线 | docx / sheet / slides / bitable | `drive +export` 后按上表处理 | 同上 |
| 待处理 | DOC / PPT / XLS（OLE2） | 无批准转换器 | 标 `conversion_required`，不伪装成功 |
| 待处理 | 扫描 PDF（无文字层） | 可识别但无正文 | 标 `needs_review`，本期不做 OCR |

明确不做：为旧格式自行寻找转换器、对扫描件批量 OCR、图片型资源入库。

---

## 7. 只读范围声明

允许调用的 lark-cli 命令**只有**这 5 条：

```text
drive files list
drive metas batch_query
drive +inspect
drive +download
drive +export
```

强制方式（三层，不靠自觉）：

1. `config/collection_policy.json` → `safety.allowlisted_lark_commands`；
2. `adapters/lark_cli.py::_assert_allowed()` 在发命令前拦截，白名单外抛 `SafetyViolation`；
3. `SourceAdapter` 协议里根本不存在任何写方法。

回归覆盖：`test_adapters.py::test_write_commands_are_blocked`（9 种写操作）、
`test_skill_scenarios.py::test_scenario_7_*`、
`test_pipeline_e2e.py::test_no_source_write_operations`。

日志与录像统一脱敏：`adapters/recorded.py::redact_secrets()`，
`config/llm_config.json` 已在 `.gitignore` 中，不会被提交。

---

## 8. 本阶段明确不承诺

- 全量 4,892 个候选都完成高质量终选；
- 所有 Agent 平台完成接入；
- MCP Server 上线；
- Plugin 完成；
- Agent 自主编排全流程；
- 所有旧 Office 格式完成转换；
- 所有扫描 PDF 完成 OCR；
- GUI 接入；
- 云盘新文件定时监控。

两周窗口的交付目标是：一条可运行、只读、可复核、可复跑的流水线，
外加一次 60–100 份样本的真实 Pilot 结果。
