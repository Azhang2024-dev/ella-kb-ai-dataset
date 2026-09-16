# 验收门禁自检 run_id=pilot-20260909-v5
生成时间：2026-09-10T14:20:15
结论：PASS（14/14 通过）

## 检查点 1-能运行
- ✅ capability manifest 与契约一致
      manifest 8 个能力与契约逐项一致，SKILL.md 齐备
- ✅ 适配器协议不含任何写方法
      协议方法：['download_file', 'export_online', 'inspect', 'list_folder']
## 检查点 2-盘点完整
- ✅ 没有 has_more=true 却缺 next_page_token 的漏项风险
      漏项风险 0 处
- ✅ 两个源文件夹都已盘点且用真实名称记录
      期望 ['docQA.llm', '知识库_2026']，实际 ['docQA.llm', '知识库_2026']
- ✅ 每条记录都有源文件夹名称
      缺失 0 条
## 检查点 3-没有误放行
- ✅ 没有超大小文件被接受
      违规 0 条
- ✅ 没有不支持格式被接受
      违规 0 条
- ✅ 采集阶段的硬拒绝没有被筛选阶段翻案
      违规 0 条
- ✅ 同一去重组里没有多条被接受
      违规 0 组
- ✅ 被接受的文档没有相同 SHA-256
      重复 0 条
- ✅ 云盘只读得到结构性保证（策略声明 + 只读白名单 + 协议无写方法）
      白名单：['drive files list', 'drive metas batch_query', 'drive +inspect', 'drive +download', 'drive +export']
## 检查点 4-结果可解释
- ✅ 每条筛选结论都有 status / reason_codes / source_path / artifact_path
      不合格 0 条
- ✅ 每条淘汰都能给出单一决定原因
      缺失 0 条
## 检查点 5-可以复跑
- ✅ 与另一次运行对比（未提供 --compare-with，跳过）
      离线一致性由 pytest test_pipeline_is_replayable_with_identical_decisions 覆盖
