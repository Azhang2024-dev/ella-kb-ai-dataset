---
description: 只读采集云盘资源到本地（download/export，支持定向批次与分层抽样）
argument-hint: "[--limit N | --sample N --sample-seed S | --source 知识库_2026 --extension pdf]"
skills: ella-collect
---
Use the `ella-collect` skill for this request. 先按 skill 说明解析 `$PY`/`$CLI`，确认同 run 已完成 inventory，再执行。

$ARGUMENTS

跑完汇报：acquired / rejected / pending_export 计数与产物路径。acquire_failed 偏高时先查 `lark-cli auth status`，不要盲目重跑；pending_export 出现时向用户说明需业务方补授权。
