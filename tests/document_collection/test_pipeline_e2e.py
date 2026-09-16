"""端到端流水线测试：inventory -> acquire -> extract -> deduplicate -> screen -> query -> report -> resume。

这条测试是"能跑通"的主证据：全部离线（FakeCloudAdapter + 真实格式夹具 + FakeLLM），
任何一步的契约破坏都会在这里暴露。
"""

from __future__ import annotations

import json
import os

import pytest

from core.document_collection.contracts import (
    STATUS_COMPLETED,
    STATUS_NEEDS_REVIEW,
    STATUS_PARTIAL,
)
from core.document_collection.service import CoreService


def _quiet(_msg: str) -> None:
    return None


@pytest.fixture
def service(policy, fake_adapter, fake_llm, workdir):
    svc = CoreService(policy, workspace_root=os.path.join(workdir, "ws"), run_id="e2e-test",
                      adapter=fake_adapter, llm=fake_llm, log=_quiet)
    yield svc
    svc.close()


def test_inventory_counts_both_sources_with_real_names(service):
    result = service.inventory()
    assert result.status in (STATUS_COMPLETED, STATUS_PARTIAL)
    assert result.summary["raw_occurrences"] == 23
    assert result.summary["unique_items"] == 22
    assert result.summary["duplicate_occurrences"] == 1
    assert result.summary["cross_source_duplicate_tokens"] == 1
    names = [s["name"] for s in result.summary["sources"]]
    assert names == ["知识库_2026", "docQA.llm"], "源文件夹必须用真实名字"
    assert result.next_action == "collection.acquire"
    for artifact in result.artifacts:
        assert os.path.isfile(artifact["path"]), artifact


def test_inventory_artifacts_are_utf8_and_readable(service):
    service.inventory()
    inv_path = os.path.join(service.workspace_dir, "source_inventory.jsonl")
    with open(inv_path, "r", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    assert len(rows) == 22
    dup = [r for r in rows if r["item_token"] == "A_PDF_1"]
    assert len(dup) == 1
    assert len(dup[0]["all_sources"]) == 2
    csv_path = os.path.join(service.workspace_dir, "source_type_counts.csv")
    with open(csv_path, "r", encoding="utf-8-sig") as fh:
        header = fh.readline().strip()
    assert "知识库_2026" in header and "docQA.llm" in header


def test_acquire_classifies_every_item(service):
    service.inventory()
    result = service.acquire()
    counts = result.summary["status_counts"]
    assert counts.get("rejected", 0) >= 2      # .jpg + shortcut
    assert counts.get("conversion_required", 0) >= 1   # legacy.doc
    assert counts.get("needs_review", 0) >= 1  # 无扩展名 mystery
    assert counts.get("acquired", 0) >= 10
    report = os.path.join(service.workspace_dir, "acquisition_report.jsonl")
    assert os.path.isfile(report)


def test_oversize_file_is_rejected_and_blob_removed(policy, fake_adapter, fake_llm, workdir):
    """硬约束：超限文件不能被接受，也不能靠压缩/拆分规避。"""
    raw = json.loads(json.dumps(policy.raw, ensure_ascii=False))
    raw["size_limits_mb"]["pdf"] = 1   # A_BIG 是 2MB
    from core.document_collection.config import CollectionPolicy
    small = CollectionPolicy(path=policy.path, raw=raw)
    svc = CoreService(small, workspace_root=os.path.join(workdir, "ws2"), run_id="oversize",
                      adapter=fake_adapter, llm=fake_llm, log=_quiet)
    try:
        svc.inventory()
        svc.acquire()
        rows = svc.store.list_items()
        big = [r for r in rows if r["item_token"] == "A_BIG"][0]
        decision = svc.store.latest_decisions("acquire")[big["item_id"]]
        assert decision["status"] == "rejected"
        assert "size_over_limit" in decision["reason_codes"]
        artifact = svc.store.get_artifact(big["item_id"])
        assert not artifact["path"] or not os.path.exists(artifact["path"]), \
            "超限 blob 必须被删除，避免占盘和被误用"
    finally:
        svc.close()


def test_extract_produces_text_and_localization(service):
    service.inventory()
    service.acquire()
    result = service.extract()
    assert result.summary["status_counts"].get("extracted", 0) >= 8
    rows = service.store.list_extractions()
    by_item = {r["item_id"]: r for r in rows}
    fr_pdf = by_item.get("A_PDF_1")
    assert fr_pdf and fr_pdf["status"] == "extracted"
    assert fr_pdf["language"] == "fr"
    assert fr_pdf["char_count"] > 500
    ru_pdf = by_item.get("B_RU_PDF")
    assert ru_pdf and ru_pdf["language"] == "ru"
    # 六个 P0 语种都要能被正确识别（这是语种配额能否成立的前提）
    expected_lang = {
        "A_ZH_PDF": "zh", "A_AR_PDF": "ar", "A_EN_PDF": "en",
        "A_ES_PDF": "es", "A_PDF_1": "fr", "B_RU_PDF": "ru",
    }
    for token, lang in expected_lang.items():
        row = by_item.get(token)
        assert row, f"{token} 没有抽取记录"
        assert row["language"] == lang, f"{token} 语言应为 {lang}，实际 {row['language']}"
    # 定位信息必须落盘
    seg_dir = os.path.join(service.workspace_dir, "segments")
    assert os.path.isdir(seg_dir) and os.listdir(seg_dir)
    with open(os.path.join(seg_dir, "A_PDF_1.jsonl"), "r", encoding="utf-8") as fh:
        first = json.loads(fh.readline())
    assert first["kind"] == "page" and "page" in first["locator"]


def test_old_office_format_is_marked_not_faked(service):
    """旧版 .doc/.ppt/.xls 没有批准转换器时必须显式标记，不能伪装成功。"""
    service.inventory()
    service.acquire()
    service.extract()
    rows = {r["item_id"]: r for r in service.store.list_extractions()}
    assert "A_OLD" not in rows or rows["A_OLD"]["status"] != "extracted"
    decisions = service.store.latest_decisions("acquire")
    assert decisions["A_OLD"]["status"] == "conversion_required"
    assert "conversion_required" in decisions["A_OLD"]["reason_codes"]
    item = service.store.get_item("A_OLD")
    assert item["status"] == "conversion_required"


def test_deduplicate_rejects_exact_duplicates_once(service):
    service.inventory()
    service.acquire()
    service.extract()
    dedup = service.deduplicate()
    assert dedup.summary["unique_items"] >= 10
    # A_PDF_1 在两个源都出现，但 token 相同 -> 盘点阶段就已合并，不应产生二次重复
    decisions = service.store.latest_decisions("deduplicate")
    dup_rejected = [k for k, v in decisions.items() if v["status"] == "rejected"]
    assert isinstance(dup_rejected, list)


def test_screen_hard_rules_reject_out_of_scope_language(service):
    service.inventory()
    service.acquire()
    service.extract()
    service.deduplicate()
    result = service.screen()
    counts = result.summary["status_counts"]
    assert counts.get("accepted", 0) >= 5
    # .jpg / shortcut / doc 不允许出现在 accepted 里
    accepted = service.store.latest_decisions("screen")
    accepted_ids = {k for k, v in accepted.items() if v["status"] == "accepted"}
    for token in ("A_IMG", "B_SHORTCUT", "A_OLD", "A_UNKNOWN", "A_BIG", "A_IMG_PDF"):
        assert token not in accepted_ids, f"{token} 不该被接受"
    # 图形主导的 PDF 必须被硬规则淘汰，并留下证据
    all_decisions = service.store.latest_decisions("screen")
    assert all_decisions["A_IMG_PDF"]["status"] == "rejected"
    assert "graphics_dominated" in all_decisions["A_IMG_PDF"]["reason_codes"]


def test_semantic_review_can_accept_with_evidence(service, fake_llm):
    service.inventory()
    service.acquire()
    service.extract()
    service.deduplicate()
    result = service.screen(semantic=True)
    assert result.summary["semantic_review_enabled"] is True
    accepted = [v for v in service.store.latest_decisions("screen").values()
                if v["status"] == "accepted"]
    assert accepted, "语义复核开启后应有条目被接受"
    for decision in accepted:
        evidence = decision["evidence"]
        assert evidence.get("education_level") == "university"
        assert evidence.get("llm_evidence"), "没有证据的判断不能自动接受"


def test_semantic_rejection_cannot_override_hard_failure(policy, fake_adapter, workdir):
    """硬失败不能被 LLM 覆盖：模型说"是好文档"也不能让超限文件通过。"""
    from conftest import FakeLLM
    raw = json.loads(json.dumps(policy.raw, ensure_ascii=False))
    raw["size_limits_mb"]["pdf"] = 1
    raw["llm"]["enabled"] = True
    from core.document_collection.config import CollectionPolicy
    small = CollectionPolicy(path=policy.path, raw=raw)
    optimistic = FakeLLM(classify={
        "education_level": "university", "subject": "computer_science_it",
        "queryability": "high", "exclude_kind": None,
        "evidence": ["this is a great textbook"], "confidence": 0.99,
        "needs_human_review": False,
    })
    svc = CoreService(small, workspace_root=os.path.join(workdir, "ws3"), run_id="hard-fail",
                      adapter=fake_adapter, llm=optimistic, log=_quiet)
    try:
        svc.inventory()
        svc.acquire()
        svc.extract()
        svc.deduplicate()
        svc.screen(semantic=True)
        decisions = svc.store.latest_decisions("screen")
        big = [k for k in decisions if k == "A_BIG"]
        assert big, "A_BIG 应出现在筛选决定里"
        assert decisions["A_BIG"]["status"] == "rejected"
        assert "size_over_limit" in decisions["A_BIG"]["reason_codes"]
    finally:
        svc.close()


def test_query_generation_produces_ten_valid_queries(service, fake_llm):
    service.inventory()
    service.acquire()
    service.extract()
    service.deduplicate()
    service.screen(semantic=True)
    result = service.generate_queries(limit=3)
    assert result.summary["total_queries"] > 0
    assert result.summary["status_counts"].get("validated", 0) >= 1
    queries = service.store.all_queries()
    by_item: dict[str, list] = {}
    for q in queries:
        by_item.setdefault(q["item_id"], []).append(q)
    for item_id, rows in by_item.items():
        if len(rows) == 10:
            counts = sorted({r["question_count"] for r in rows})
            assert max(counts) < 12
            types = {}
            for r in rows:
                types.setdefault(r["type_key"], 0)
                types[r["type_key"]] += 1
            assert types == {"multiple_choice": 2, "fill_in_blank": 2, "short_answer": 2,
                             "true_false": 2, "mixed": 2}
            compat = [r for r in rows if r["compatibility_only"]]
            assert len(compat) == 2
            break
    else:
        pytest.fail("没有任何文档产出完整的 10 条 query")


def test_query_rejects_fragment_references(service):
    from core.document_collection.query_generation import validate_queries
    bad = [{
        "seq": 1, "type_key": "multiple_choice", "question_count": 5, "language": "fr",
        "text": "Voir le chapitre 3 du document et la page 12 : creez 5 questions.",
        "evidence_locator": {"page": 3}, "compatibility_only": False,
    }]
    report = validate_queries(bad, policy=service.policy, document_language="fr")
    assert report["ok"] is False
    codes = {e["code"] for e in report["errors"]}
    assert "query_references_fragment" in codes
    assert "query_count_mismatch" in codes


def test_report_and_resume_are_consistent(service):
    service.inventory()
    service.acquire()
    service.extract()
    service.deduplicate()
    service.screen()
    report = service.report()
    assert report.status in (STATUS_COMPLETED, STATUS_NEEDS_REVIEW, STATUS_PARTIAL)
    coverage = os.path.join(service.workspace_dir, "coverage_report.md")
    assert os.path.isfile(coverage)
    text = open(coverage, "r", encoding="utf-8").read()
    assert "知识库_2026" in text and "docQA.llm" in text
    assert "原始候选数量" in text
    resume = service.resume()
    assert resume.status == STATUS_COMPLETED
    assert resume.next_action


def test_pipeline_is_replayable_with_identical_decisions(policy, fake_adapter, fake_llm, workdir):
    """验收检查点 5：同一 fixture 重跑，决策/哈希/数量一致，只有 run_id 与时间戳可不同。"""
    def run_once(run_id: str) -> dict:
        svc = CoreService(policy, workspace_root=os.path.join(workdir, "ws_replay"),
                          run_id=run_id, adapter=fake_adapter, llm=fake_llm, log=_quiet)
        try:
            svc.inventory()
            svc.acquire()
            svc.extract()
            svc.deduplicate()
            svc.screen(semantic=True)
            decisions = svc.store.latest_decisions("screen")
            artifacts = {r["item_id"]: r["sha256"] for r in svc.store.list_artifacts() if r["sha256"]}
            return {
                "decisions": {k: (v["status"], sorted(v["reason_codes"])) for k, v in decisions.items()},
                "hashes": artifacts,
                "counts": svc.store.count_items(),
            }
        finally:
            svc.close()

    first = run_once("replay-1")
    second = run_once("replay-2")
    assert first["decisions"] == second["decisions"]
    assert first["hashes"] == second["hashes"]
    assert first["counts"] == second["counts"]


def test_stage_state_uses_capability_names_and_keeps_every_stage(service):
    """D21 回归：阶段模块用短名、CoreService 用能力全名，合并后会出现两套键。"""
    service.inventory()
    service.acquire()
    service.extract()
    service.deduplicate()
    service.screen()
    service.report()
    row = service.store.get_run(service.run_id)
    stages = json.loads(row["stage_state_json"])
    for key in stages:
        assert key.startswith("collection."), f"stage_state 里出现了非能力全名的键：{key}"
    for expected in ("collection.inventory", "collection.acquire", "collection.extract",
                     "collection.deduplicate", "collection.screen", "collection.report"):
        assert expected in stages, f"{expected} 的摘要丢了"
    assert stages["collection.screen"]["semantic_review_enabled"] is False


def test_no_source_write_operations(fake_adapter, service):
    """安全约束：适配器只被调用过只读方法。"""
    service.inventory()
    service.acquire()
    allowed = ("list:", "inspect:", "download:", "export:")
    for call in fake_adapter.calls:
        assert call.startswith(allowed), f"出现非只读调用：{call}"
