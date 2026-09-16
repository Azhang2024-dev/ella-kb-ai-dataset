# 迁移后验证证据（2026-09-10）

本目录保存的是**项目迁入独立仓库之后现场重跑**的门禁报告，用来证明迁移没有破坏任何东西。
`output/document_collection/` 里的历史 run 已还原为迁移前的原始内容（只改了路径前缀），
所以"迁移后是否仍然通过"的证据单独放在这里，不与历史 run 混在一起。

| 文件 | 是什么 | 结果 |
|-|-|-|
| `audit_pilot-v5_迁移后.json` / `.md` | 对最终基线 run `pilot-20260909-v5` 跑 `audit`（不带对比） | **14 / 14 PASS** |
| `audit_repro-v6_vs_v7_迁移后.json` / `.md` | `--run-id repro-v6 audit --compare-with output/document_collection/repro-v7`，即"可复跑"检查点 | **19 / 19 PASS** |

同批现场验证（未产生报告文件的）：

| 验证 | 结果 |
|-|-|
| `pytest tests/document_collection -q` | 356 passed / 63.63s |
| `ruff check --select F,E9` | All checks passed |
| `compileall` | exit 0 |
| `validate-config` | ok（schema 0.1），两个采集源与规则打印正确 |
| `explain --item-id REDACTED_DOC_013` | 判定链完整，本地产物路径指向新仓库且文件存在 |
| artifact 抽查 3 条 | `exists=True`、`size_ok=True`、`sha_ok=True` |
| 历史 run 还原校验 | 8 个文件按路径前缀归一后与原始库**逐字节/逐行一致** |
| LLM 适配器可用性 | `LiteLLMAdapter`，`available=True`（未实际调用模型） |

## 一个容易踩的用法坑

`audit --compare-with` 接收的是**另一次运行的目录**，不是 run id；
而且不能拿"启用了语义复核的 run"去比"只过硬规则的 run"——两者的 accepted 数天然不同，
检查点 5 会如实报 FAIL。正确的对照是 `repro-v6` vs `repro-v7`（同代码、同 seed、同开关）。
