# Plugin（本阶段不实现，只留边界）

> 状态：**边界预留**。两周 MVP 不实现任何平台 Plugin，也不创建 wrapper。
> 本文件的作用是把"什么算 Plugin、什么不算"写死，避免将来把 Core 复制进 Plugin。

## 1. Plugin 是什么，不是什么

```text
Plugin = 某个 Agent 宿主平台的安装外壳（清单 + 注册信息 + 资源声明）
Plugin ≠ 业务实现
```

Plugin 只允许做三件事：

1. 声明这个能力在某个宿主上叫什么、怎么触发；
2. 把宿主给的参数转成 CLI 参数或未来 MCP tool 参数；
3. 把 `RunResult` 转成宿主能展示的形态（文本、卡片、文件链接）。

Plugin **不允许**做的事：

- 复制任何筛选、去重、抽取、Query 生成逻辑；
- 自己读 `state.sqlite3`；
- 自己拼 `lark-cli` 命令；
- 自己判断文件大小、语种、题量是否合格；
- 缓存或改写业务结论。

一句话：**Plugin 只能调用 CLI 或未来的 MCP，不能绕过它们。**

## 2. 为什么现在不做

- 具体宿主平台还没确认（计划第 11 章的准入条件之一）；
- 宿主的认证方式、资源读取方式、审批机制都还没确认；
- CLI 契约刚跑通，还没经过真实 Pilot；
- 现在做 wrapper，等宿主确定后大概率要重写，等于白花工。

## 3. 未来每个平台都要单独验证的清单

不要假设"在一个平台跑通了，其他平台也一样"。每接一个宿主，都要单独过一遍：

| 项 | 要验证什么 |
|-|-|
| 安装 | 宿主能否发现并加载 Plugin；版本与依赖怎么声明 |
| 认证 | 宿主身份与 `lark-cli auth status` 的用户身份是否一致；谁持有 token |
| 工具调用 | 宿主能否传入长参数（例如含 `\|` 的分页 token）；是否二次转义 |
| 资源读取 | 宿主能否读取 `artifacts/` 下的本地文件；路径是否跨进程可见 |
| 审批 | `needs_review` 能否在宿主里形成人工待办，而不是被静默吞掉 |
| 长任务 | 一次全量 inventory 约 1–2 分钟，采集更久；宿主超时策略是什么 |
| 输出体积 | `screening_report.jsonl` 可能几 MB，宿主能否承载，要不要只回摘要 |
| 只读保证 | 宿主是否可能自动触发写操作；白名单是否仍然生效 |

## 4. 建议目录（未来）

```text
plugins/
    <host_name>/
        plugin.json          # 宿主清单：名称、版本、触发描述
        adapter.py           # 只做参数转换 + 调 CLI/MCP，不含业务逻辑
        README.md            # 该宿主的安装与验证记录
```

`adapter.py` 里如果出现 `if size_bytes > ...` 这类判断，说明已经越界了。

## 5. 与 Skill 的分工

| | Skill | Plugin |
|-|-|-|
| 面向 | Agent 的**判断**：何时用、用哪个能力、何时停 | 宿主的**接入**：怎么装、怎么调、怎么显示 |
| 内容 | 触发条件、停止条件、禁止动作、口径速查 | 清单、参数映射、结果渲染 |
| 是否有业务逻辑 | 无 | 无 |
| 当前状态 | 已实现（`skills/document_collection/`） | 未实现 |

两者都不允许持有业务规则；规则只在 `config/collection_policy.json` 与 Core 里。

## 6. 参考

- Skill 入口：`skills/document_collection/SKILL.md`
- 能力清单：`skills/document_collection/capability-manifest.json`
- MCP 预留：`mcp_server/README.md`
- OpenClaw Skills 说明：https://docs.openclaw.ai/tools/skills
