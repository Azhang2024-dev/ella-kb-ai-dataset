# MCP Server（本阶段不实现，只留契约）

> 状态：**设计预留**。两周 MVP 不实现 MCP Server，也不安装 MCP SDK。
> 这里只记录未来接入时必须遵守的边界，避免到时候把 Core 的业务逻辑复制一份出来。

## 1. 为什么现在不做

计划第 11 章的准入条件还没满足：

- CLI 命令还需要经过一次真实 Pilot 才算稳定；
- Core 的输入输出结构还没被真实数据验证过；
- 目前只有一个 Agent 宿主（Codex/Claude 类 CLI Agent），它可以直接调 CLI；
- 目标宿主还没有出现"无法直接调用 CLI"的硬约束。

过早接 MCP 的代价是：契约还没验证就要为协议层返工。

## 2. 未来实现时必须遵守的边界

```text
Core（core/document_collection/）  = 唯一的业务实现
CLI（scripts/collection_cli.py）   = 稳定调用入口
MCP Server                         = 只做协议适配
```

硬性规则：

1. **MCP 不复制任何筛选规则。** 大小上限、语种配额、题型题量、去重口径全部只存在于
   `config/collection_policy.json` + Core，MCP 层不允许出现第二份。
2. **MCP 不持有状态。** 状态只在 Core 的 SQLite（`output/document_collection/<run_id>/state.sqlite3`）
   和本地产物里。MCP 进程重启不应丢任何东西。
3. **MCP tool 与 capability 一一对应。** 映射关系已经写在
   `skills/document_collection/capability-manifest.json` 的 `future_mcp_tool` 字段里。
4. **参数结构直接复用 manifest 的 `inputs`。** 不要另起一套命名。
5. **返回结构就是 `RunResult`。** 字段见 manifest 的 `result_envelope`：
   `status / run_id / summary / artifacts / review_items / errors / next_action`。
6. **副作用声明照搬 manifest 的 `side_effects`。** MCP 的 tool annotation
   （readOnlyHint / destructiveHint / openWorldHint）应由它推导，不要手工另写：
   - `read_source` + `write_local` → `readOnlyHint=false`（会写本地）、`destructiveHint=false`；
   - 任何能力都不得出现 `modify_source` 等禁止副作用（见 `contracts.FORBIDDEN_SIDE_EFFECTS`）。
7. **MCP Server 是可选依赖。** 没装 MCP SDK 时，CLI 与 Core 必须照常可用。

## 3. 映射表（未来直接照此生成）

| capability | CLI | 未来 MCP tool | 需要 LLM | 幂等 |
|-|-|-|-|-|
| `collection.inventory` | `inventory` | `collection_inventory` | 否 | 是 |
| `collection.acquire` | `acquire` | `collection_acquire` | 否 | 是 |
| `collection.extract` | `extract` | `collection_extract` | 否 | 是 |
| `collection.screen` | `screen` | `collection_screen` | 可选 | 是 |
| `collection.deduplicate` | `deduplicate` | `collection_deduplicate` | 否 | 是 |
| `collection.generate_queries` | `query` | `collection_generate_queries` | 是 | 否 |
| `collection.report` | `report` | `collection_report` | 否 | 是 |
| `collection.resume` | `resume` | `collection_resume` | 否 | 是 |

`collection.generate_queries` 是唯一不幂等的能力（模型输出会变），
MCP 层必须把 `run_id` 透传出去，让调用方能追溯是哪一次生成的。

## 4. 建议实现方式（未来）

```text
mcp_server/
    __init__.py
    server.py          # 只做：manifest -> tools 注册 + 参数校验 + 调 CoreService
    requirements.txt   # mcp SDK，独立于主 requirements.txt
```

`server.py` 的正确写法是读 `capability-manifest.json` 动态注册 tool，
然后每个 tool 的 handler 只有一行：调用 `CoreService` 的对应方法并返回 `result.to_dict()`。
如果 handler 里出现了 if/else 业务判断，说明已经越界了。

## 5. 参考

- MCP 架构：https://modelcontextprotocol.io/docs/2026-07-28/learn/architecture
- MCP Python SDK：https://py.sdk.modelcontextprotocol.io/
- 能力清单：`skills/document_collection/capability-manifest.json`
