# Skill 场景示例（Task 8 要求的 7 个场景）

下面每条都是**可以直接复制执行**的命令。`$PY` / `$CLI` 从本 Skill 目录的
`runtime.json` 推导（读 `repo` 字段），在任何 cwd 下都能跑，**不需要先 `cd`**
（CLI 自己按文件位置解析仓库根）：

```powershell
# PowerShell
$repo = (Get-Content "<本 Skill 目录>\runtime.json" -Raw | ConvertFrom-Json).repo
$PY  = "$repo\.venv\Scripts\python.exe"
$CLI = "$repo\scripts\collection_cli.py"
# 用一个**新** run id。不要拿 output\document_collection 下已有的历史 run 当例子跑：
# inventory / acquire / extract / audit / resume 都会写运行目录，会把证据改掉（SKILL.md §7 第 8 条）。
$RUN = "demo-20260910"
```

```bash
# bash / 类 Unix（.venv 目录结构相同，解释器路径不同）
repo=$(python3 -c "import json;print(json.load(open('<本 Skill 目录>/runtime.json',encoding='utf-8'))['repo'])")
PY="$repo/.venv/bin/python.exe"   # Windows 上的 .venv 用 .venv/Scripts/python.exe
CLI="$repo/scripts/collection_cli.py"
RUN="demo-20260910"
```

这 7 个场景由 `tests/document_collection/test_skill_scenarios.py` 自动验证，
不是纸面文档：任何一条被破坏，测试会失败。

---

## 场景 1：只做 inventory（不下载任何正文）

```powershell
& $PY $CLI --run-id $RUN inventory
```

期望：

- `status=completed` 或 `partial`（有告警时）；
- 产出 `source_inventory.jsonl`、`source_type_counts.csv`、`inventory_summary.md`；
- 报告里出现真实文件夹名 `知识库_2026` 与 `docQA.llm`，不出现"原始/新增"这类含糊说法；
- `next_action=collection.acquire`；
- `artifacts/` 目录为空 —— inventory 阶段不落任何文件正文。

小批量试跑（每个源只盘 30 条）：

```powershell
& $PY $CLI --run-id probe inventory --per-source-limit 30
```

---

## 场景 2：检查一个候选（它为什么被判成这个结果）

```powershell
& $PY $CLI --run-id $RUN explain --item-id <item_id>
& $PY $CLI --run-id $RUN --json explain --item-id <item_id>
```

`explain` 是只读诊断命令，一次打印：

- 来源路径与**全部来源**（跨源重复时会列出每一处）；
- 原始链接、类型、扩展名；
- 当前状态与所处阶段；
- 本地产物路径、大小、SHA-256；
- 抽取结果、字符数、语言与置信度、抽取错误；
- **完整判定链**：每个阶段的 `status` + `reason_codes` + `evidence`；
- 已生成的 query 与所属去重分组。

`item_id` 默认等于飞书 token；本地目录适配器下是归一后的 `id-<hash>`，
这时也可以直接传原始 token，命令会自动回退按 token 查找。

不知道 `item_id` 时先看汇总表：

```text
output/document_collection/<run_id>/all_decisions.csv
output/document_collection/<run_id>/human_review_queue.csv
output/document_collection/<run_id>/accepted_documents.csv
```

期望：每个条目至少有 `status`、`reason_codes`、来源路径、产物路径四项可查。

## 场景 3：拒绝超大小文件

大小上限写在 `config/collection_policy.json` 的 `size_limits_mb`，不能在命令行放宽。

```powershell
& $PY $CLI --run-id $RUN acquire --extension txt
```

期望：

- 超过 20 MB 的 TXT / XLSX / CSV / MD → `status=rejected`，`reason_codes` 含 `size_over_limit`；
- 超过 100 MB 的 PDF / DOC / DOCX / PPTX → 同上；
- 超限文件的本地副本默认被删除（`oversized_blob_deleted`），只留下大小证据；
- 后续 `screen` 阶段不会把它软化成 `needs_review`（回归用例已覆盖）。

飞书清单接口不返回文件大小，所以大小只能在下载后判定；这一点在
`docs/document_collection_scope.md` 里有实测记录。

---

## 场景 4：未知 MIME 进入人工复核

```powershell
& $PY $CLI --run-id $RUN extract
```

期望：

| 情况 | 结果 |
|-|-|
| 签名与扩展名不一致（`.txt` 实际是 PDF） | `needs_review` + `mime_extension_mismatch` |
| 没有任何抽取器能处理 | `needs_review` + `mime_unsupported` + `no_extractor` |
| 图片文件 | `needs_review` + `mime_unsupported` + `image_file` |
| 无扩展名 | `needs_review` + `extension_missing` |
| 旧版 `.doc/.ppt/.xls`（OLE2 签名） | `conversion_required`，不伪装成功 |

这些条目会出现在 `human_review_queue.csv`，不会被自动放行。

---

## 场景 5：采集失败后恢复

先看还剩什么没做：

```powershell
& $PY $CLI --run-id @latest --json resume
```

只重跑失败与待导出的条目：

```powershell
& $PY $CLI --run-id @latest acquire --status acquire_failed --status pending_export
```

授权过期导致的批量失败，先恢复授权再重跑：

```powershell
lark-cli auth status
& $PY $CLI --run-id @latest acquire --status acquire_failed
```

期望：

- 已经采到的文件不会被重复下载（`resumed_existing_artifact`）；
- 429 / 5xx / 超时会自动退避重试，最多 3 次；
- 权限不足这类永久错误不重试，直接记录；
- 重跑后 `source_inventory.jsonl` 与各项计数不翻倍（幂等）。

---

## 场景 6：为一份合格文档生成 Query

先小批量试跑（计划 Task 7 要求先 10–20 份）：

```powershell
& $PY $CLI --run-id $RUN query --limit 10
```

期望：

- 每份文档 10 条：选择题 2（题量 5）、填空题 2（3）、解答题 2（2）、判断题 2（7）、混合题型 2（9）；
- 所有题量 < 12，且同文档内不同题型题量互不重复；
- query 文案里不出现片段/章节/页码/Sheet/表格（六语种黑名单校验）；
- query 语言跟随文档语言；
- 定位信息只出现在 `query_evidence.jsonl`，不出现在 query 文案里；
- 混合题型标 `compatibility_only=true`，预期行为记为"丢弃或降级处理"；
- 校验结论见 `query_validation_report.md`。

模型不可用时：

- 命令返回 `status=needs_review`，`total_queries=0`；
- **不会**产出任何伪造的 query。

---

## 场景 7：拒绝修改云盘源文件的请求

如果用户说"把这个文件改名 / 移到另一个文件夹 / 删掉重复的那份 / 传个新版本上去"，
正确回应是**拒绝并解释**，不是执行：

> 本期采集源是只读的。流水线只做列举、下载、导出三种读操作；
> 删除、移动、重命名、改标题、上传、建文件夹、加快捷方式都在适配器白名单之外，
> 调用会直接抛 `SafetyViolation`。需要整理云盘请走单独的流程和审批。

技术保证（不是靠自觉）：

- `config/collection_policy.json` → `safety.allowlisted_lark_commands` 只有 5 条只读命令；
- `core/document_collection/adapters/lark_cli.py::_assert_allowed()` 在发命令前拦截；
- 适配器协议 `SourceAdapter` 里根本没有任何写方法；
- 回归用例 `test_adapters.py::test_write_commands_are_blocked` 覆盖 7 种写操作；
- 回归用例 `test_pipeline_e2e.py::test_no_source_write_operations` 断言整条流水线只发生过只读调用。

去重要求"删掉重复的那份"时，正确做法是：在本地把非代表版本判为 `rejected`，
云盘上的文件一个都不动，并在 `dedup_groups.csv` 里留下推荐保留版本与原因。
