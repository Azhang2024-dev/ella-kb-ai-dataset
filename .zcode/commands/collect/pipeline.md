---
description: 编排整条采集流水线（半自动模式：每步后汇报状态，gate 点等放行）
argument-hint: "[目标描述，如：从盘点跑到 query，P0 定向批次 EN/FR 各 40 份]"
skills: document_collection
---
Use the `document_collection` skill（编排模式）for this request. 按 skill §3 执行**半自动**编排：按 inventory → acquire → extract → deduplicate → screen → query → questions → report 顺序逐步执行（本地文件夹入口则把前两步换成 `ingest --dir`），每步跑完用 `status` 命令把矩阵汇报给用户。

$ARGUMENTS

固定 gate 点（停下等用户放行）：`screen` 之后（看 needs_review 队列）、`query` 之后（看校验报告）、交付前（必跑 `audit`）。任何时候触发 skill §6 停止规则立即停下。用户明确说"全自动"才切换为无 gate 连跑，且停止规则仍然生效。
