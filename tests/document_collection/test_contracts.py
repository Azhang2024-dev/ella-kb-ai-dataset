"""契约层测试：结构稳定 = 未来 MCP/Plugin 不用改 Core。"""

from __future__ import annotations

import json

import pytest

from core.document_collection import contracts as C


def test_capability_table_is_complete_and_consistent():
    assert len(C.CAPABILITIES) == 11
    names = [c["name"] for c in C.CAPABILITIES]
    assert names == [
        "collection.inventory", "collection.acquire", "collection.extract",
        "collection.screen", "collection.deduplicate", "collection.generate_queries",
        "collection.generate_questions", "collection.report", "collection.resume",
        "collection.ingest", "collection.deliver",
    ]
    cli = [c["cli_command"] for c in C.CAPABILITIES]
    assert len(set(cli)) == 11, "CLI 命令名不能重复"
    mcp = [c["future_mcp_tool"] for c in C.CAPABILITIES]
    assert len(set(mcp)) == 11
    for cap in C.CAPABILITIES:
        # future_mcp_tool 由能力名派生（query/questions 的 CLI 命令名比能力名短）
        assert cap["future_mcp_tool"] == "collection_" + cap["name"].split(".", 1)[1]
    assert set(C.CAPABILITY_NAMES) == set(names)
    assert set(C.CLI_COMMANDS) == set(cli)


def test_run_statuses_are_exactly_the_five_planned_values():
    assert set(C.RUN_STATUSES) == {"completed", "partial", "needs_review", "failed", "cancelled"}


def test_item_output_statuses_match_the_plan():
    assert set(C.ITEM_OUTPUT_STATUSES) == {
        "accepted", "rejected", "needs_review", "pending_export", "conversion_required"}


def test_forbidden_side_effects_cover_all_source_writes():
    assert {"modify_source", "delete_source", "move_source", "rename_source",
            "upload_to_source"} <= set(C.FORBIDDEN_SIDE_EFFECTS)


def test_run_config_roundtrip():
    cfg = C.RunConfig(
        run_id="r1", source_folders=("A", "B"), allowed_extensions=("pdf", "txt"),
        max_size_mb={"pdf": 100, "txt": 20}, target_languages=("en", "fr"),
        queries_per_document=10, dry_run=False, approval_mode="read_only",
        llm_enabled=False,
    )
    restored = C.RunConfig.from_dict(json.loads(json.dumps(cfg.to_dict())))
    assert restored == cfg


def test_run_config_from_dict_ignores_unknown_keys():
    cfg = C.RunConfig.from_dict({
        "run_id": "r2", "source_folders": ["A"], "allowed_extensions": ["pdf"],
        "max_size_mb": {"PDF": 100}, "target_languages": ["en"],
        "queries_per_document": 10, "dry_run": True, "approval_mode": "read_only",
        "llm_enabled": True, "some_future_field": "ignored",
    })
    assert cfg.run_id == "r2"
    assert cfg.max_size_mb == {"pdf": 100}, "扩展名必须归一化成小写"
    assert cfg.source_folders == ("A",)


def test_run_result_rejects_unknown_status():
    with pytest.raises(ValueError):
        C.RunResult(status="half_done", run_id="r")


def test_run_result_roundtrip_keeps_tuples():
    result = C.RunResult(
        status=C.STATUS_COMPLETED, run_id="r3",
        summary={"a": 1},
        artifacts=(C.artifact_record("p1", "report"),),
        review_items=({"item_id": "x"},),
        errors=(C.error_record("screen", "boom"),),
        next_action="collection.report", capability="collection.screen",
    )
    restored = C.RunResult.from_dict(json.loads(result.to_json()))
    assert restored.status == result.status
    assert restored.artifacts == result.artifacts
    assert restored.errors[0]["stage"] == "screen"
    assert restored.next_action == "collection.report"


def test_source_item_source_path_uses_real_folder_name():
    item = C.SourceItem(
        source_name="知识库_2026", source_folder_path="DOC-FR", item_token="T",
        name="cours.pdf", item_type="local_file", extension="pdf",
        metadata={"relative_path": "DOC-FR/cours.pdf"},
    )
    assert item.source_path == "知识库_2026/DOC-FR/cours.pdf"


def test_source_item_roundtrip():
    item = C.SourceItem(source_name="docQA.llm", source_folder_path="/", item_token="T2",
                        name="x.txt", item_type="local_file", extension="txt",
                        remote_size=None, url="https://example/x")
    restored = C.SourceItem.from_dict(json.loads(json.dumps(item.to_dict())))
    assert restored == item


def test_summarize_counts_and_sorts():
    rows = [{"status": "accepted"}, {"status": "rejected"}, {"status": "accepted"}, {}]
    out = C.summarize(rows)
    assert out == {"accepted": 2, "rejected": 1, "unknown": 1}
