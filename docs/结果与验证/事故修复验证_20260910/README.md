# 事故修复验证：历史 run 被误写后的还原与复验（2026-09-10）

对应开发日志 **D23-2**。这里放的是还原动作的证据与脚本原件，不是新的功能产物。

## 1. 事故是什么

为了验证「装好的 SKILL.md 照着跑能不能用」，把 SKILL.md 里的示例命令整批执行了一遍。
其中 `audit` / `resume` / `inventory` / `acquire` 都会**写运行目录**，于是污染了两个历史 run：

| run | 时间 | 被写的内容 |
|-|-|-|
| `repro-v6` | 14:08:14 | `audit_report.json/.md`、`result_audit.json`、`state.sqlite3` 被改写；多出 `result_resume.json` |
| `pilot-20260909` | 14:09:59-14:10:45 | 盘点五件套 + `run_manifest.json` + `state.sqlite3` 被改写；多出 35 个文件（14 artifacts、14 artifacts_meta、`acquisition_report.jsonl`、`result_inventory.json`、sqlite `-wal`/`-shm`、1 个 `.part`、空 `tmp/`） |

`pilot-20260909-v5`、`repro-v7`、v2/v3/v4 未受影响（`@latest` 解析到 repro-v6）。
最危险的一处：`pilot-20260909` 是缺陷 D5「lark-cli 垫片吃掉分页 token」的对照证据，
盘点只有 **3,478** 条；用修好后的代码重跑会变成 4,892 条，证据就消失了 —— 而这正是当时发生的事。

acquire 在第 15 个文件下载中途被杀掉（pwsh 循环 + venv 启动器 + 基础解释器三个进程）。

## 2. 恢复源与规则

- 恢复源：`D:\Users\admin\_migration_trash_20260910\旧仓库_Ella知识库数据集小工具\output\document_collection\`
  （迁移前的原始副本；迁移记录 §6 当时也是用它还原的）。
- 改写规则完全对齐迁移记录 §3：文本文件同时替换单反斜杠与 JSON 转义两种形式；
  `state.sqlite3` 按列 `UPDATE ... SET col = REPLACE(col, 旧, 新)`，**不做二进制替换**
  （新旧仓库根 13→14 字符，直接改字节会破坏 SQLite 页结构）。
- **先做对照组再动手**：挑 12 个当天没被碰过的同名文本文件，把旧副本按同一规则改写后与现状逐字节比对，
  12/12 一致才继续；对不上就中止。这是本次还原可信度的关键。
- 我新增的 36 个文件全部移入
  `D:\Users\admin\_migration_trash_20260910\事故_20260910_误跑历史run\`（27.0MB），不直接删。

## 3. 本目录文件

| 文件 | 说明 |
|-|-|
| `repair_runs.py` | 还原脚本原件：对照组校验 → 隔离新增文件 → 从旧副本还原改写文件 |
| `verify_restore.py` | 复验脚本原件：文件清单、旧路径残留、sqlite 关键行、业务计数 |
| `audit_repro-v6_vs_v7_修复后.json/.md` | 还原后在**副本**上重跑的门禁：**19 / 19 PASS** |
| `audit_pilot-v5_修复后.json/.md` | 还原后在**副本**上重跑的最终基线门禁：**14 / 14 PASS** |

## 4. 复验结论

| 项 | 结果 |
|-|-|
| `pilot-20260909` 文件清单 | **80 个**，与原始副本完全一致，无多余、无缺失（`tmp/` 空目录已清除） |
| `repro-v6` 文件清单 | **97 个**；缺的 210 个只有 artifacts/extracted/segments，与迁移记录 §3「只搬报告与状态库」一致 |
| 旧仓库根残留 | 文本 **0** 处；sqlite **0** 处（逐表逐列 LIKE 扫描） |
| `repro-v6` 的 `runs` 行 | `started_at=2026-09-09T22:11:50+0800`、`updated_at=2026-09-09T22:16:46+0800`、`stage_state_json` **12 键**（与迁移记录 §6/§7 记录一致） |
| `pilot-20260909` 的 `runs` 行 | `started_at=2026-09-09T19:45:07+0800`、`updated_at=2026-09-09T19:46:12+0800`、items **3,478**（D5 对照值） |
| 门禁（可复跑） | `repro-v6 audit --compare-with repro-v7` → **19 / 19 PASS** |
| 门禁（最终基线） | `pilot-20260909-v5 audit` → **14 / 14 PASS** |
| 原始 run 是否被复验再次触碰 | 否：门禁全部在 `output\_verify_20260910_incident\` 的副本上跑，跑完副本已移出 `output/` |

## 5. 两处「看起来未通过」其实是复验脚本的期望值写错

`verify_restore.py` 当时报了两项 fail，都**不是**还原失败：

1. `repro-v6 updated_at 应为 2026-09-09T22:16:46，实际 2026-09-09T22:16:46+0800`
   —— 迁移记录 §6 引用时省略了时区后缀，值本身一致。
2. `pilot-20260909/source_inventory.jsonl = 3478 行（期望 4892）`
   —— 3,478 正是 D5 修复**前**的数值，是这个 run 存在的意义；4,892 是修复后的数值。
   期望值写错的是脚本，不是数据。

脚本原件按「证据不事后修饰」的原则保留原样，未修正这两处期望值。

## 6. 因此新增的操作纪律

`audit` / `resume` / 任何阶段命令都会写运行目录与 `state.sqlite3`。
复核历史 run 必须先复制到临时工作区，再用 `--workspace` 指过去：

```powershell
Copy-Item -Recurse "D:\Users\admin\Desktop\My_Project\Ella知识库AI生题数据集\output\document_collection\<历史 run>" "<临时工作区>\<历史 run>"
& "D:\Users\admin\Desktop\My_Project\Ella知识库AI生题数据集\.venv\Scripts\python.exe" "D:\Users\admin\Desktop\My_Project\Ella知识库AI生题数据集\scripts\collection_cli.py" --workspace "<临时工作区>" --run-id <历史 run> audit
```

已写进 `skills/document_collection/SKILL.md` §3/§4/§6 与 `README.md`。
**未做**代码级护栏（例如给基线 run 打标记、让写操作直接拒绝）：那属于状态库语义变更，需要单独确认。
