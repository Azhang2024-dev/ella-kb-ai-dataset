"""报告与判定轨迹可读性测试。

第一次真实 Pilot 的覆盖报告里出现过「淘汰原因：extension_supported 37 次」，
原因是 reason_codes 是完整轨迹（含已通过的门禁），报告不加区分地全部计数。
本文件把这个口径固化下来。
"""

from __future__ import annotations

import csv
import json
import os

import pytest

from core.document_collection import contracts as C
from core.document_collection.reporting import collect_facts, quota_gap
from core.document_collection.service import CoreService


def _quiet(_m: str) -> None:
    return None


# ---------------------------------------------------------------------------
# 原因码分类
# ---------------------------------------------------------------------------
def test_deciding_reason_skips_positive_and_informational_codes():
    trail = ["extension_supported", "size_unknown_remote", "size_within_limit",
             "language_in_scope", "ok", "requires_full_text_review"]
    assert C.deciding_reason(trail) is None, "全部是通过/信息性标记时不应编出原因"

    rejected = ["extension_supported", "size_within_limit", "size_over_limit"]
    assert C.deciding_reason(rejected) == "size_over_limit"

    semantic = ["extension_supported", "size_within_limit", "language_in_scope",
                "ok", "education_level=school", "subject=unknown"]
    assert C.deciding_reason(semantic) == "subject=unknown"

    review = ["extension_supported", "size_within_limit", "language_ambiguous"]
    assert C.deciding_reason(review) == "language_ambiguous"

    dup = ["extension_supported", "size_within_limit", "language_in_scope",
           "ok", "exact_duplicate"]
    assert C.deciding_reason(dup) == "exact_duplicate"


def test_deciding_reason_takes_the_last_explanatory_code():
    trail = ["graphics_dominated", "extract_empty_text"]
    assert C.deciding_reason(trail) == "extract_empty_text"
    assert C.deciding_reason([]) is None
    assert C.deciding_reason(None) is None


@pytest.mark.parametrize("code", [
    "ok", "extension_supported", "size_within_limit", "language_in_scope",
    "extract_ok", "unique", "requires_full_text_review", "long_document",
    "size_unknown_remote", "content_already_stored", "resumed_existing_artifact",
])
def test_positive_and_informational_codes_are_not_explanatory(code):
    assert C.is_explanatory_reason(code) is False


@pytest.mark.parametrize("code", [
    "size_over_limit", "extension_unsupported", "language_not_in_scope",
    "language_ambiguous", "graphics_dominated", "text_too_sparse",
    "exact_duplicate", "normalized_duplicate", "extract_failed",
    "extract_empty_text", "acquire_failed", "pending_export",
    "conversion_required", "mime_extension_mismatch", "artifact_missing",
    "excluded_kind=annual_report", "education_level=school",
    "queryability=low", "llm_no_evidence", "llm_output_invalid",
])
def test_real_reasons_are_explanatory(code):
    assert C.is_explanatory_reason(code) is True, code


def test_extractor_tag_is_not_counted_as_a_reason():
    assert C.is_explanatory_reason("extractor=pdf") is True   # 有信息量，保留
    assert C.is_explanatory_reason("mime_verdict=mismatch") is False


# ---------------------------------------------------------------------------
# 端到端报告口径
# ---------------------------------------------------------------------------
@pytest.fixture
def ran_service(policy, fake_adapter, fake_llm, workdir):
    service = CoreService(policy, workspace_root=os.path.join(workdir, "ws"),
                          run_id="report-test", adapter=fake_adapter, llm=fake_llm,
                          log=_quiet)
    service.inventory()
    service.acquire()
    service.extract()
    service.deduplicate()
    service.screen()
    yield service
    service.close()


def test_rejected_reason_distribution_contains_no_positive_codes(ran_service):
    facts = collect_facts(ran_service.store, ran_service.policy)
    rejected = facts["rejected_reason_counts"]
    assert rejected, "样本里必须有淘汰项"
    for code in rejected:
        assert C.is_explanatory_reason(code), f"淘汰原因分布里混进了 {code}"
    for banned in ("extension_supported", "size_within_limit", "language_in_scope", "ok"):
        assert banned not in rejected


def test_review_reason_distribution_contains_no_positive_codes(ran_service):
    facts = collect_facts(ran_service.store, ran_service.policy)
    review = facts["review_reason_counts"]
    for code in review:
        assert C.is_explanatory_reason(code), f"复核原因分布里混进了 {code}"


def test_coverage_report_tables_are_readable(ran_service):
    result = ran_service.report()
    path = os.path.join(ran_service.workspace_dir, "coverage_report.md")
    text = open(path, encoding="utf-8").read()
    assert "淘汰原因分布" in text
    assert "只统计真正决定结论的原因" in text
    # 淘汰原因小节里不能出现正向门禁码
    section = text.split("### 3.1")[1].split("###")[0] if "### 3.1" in text else ""
    for banned in ("extension_supported", "size_within_limit", "language_in_scope"):
        assert banned not in section, f"{banned} 不应出现在淘汰原因表里"
    assert result.summary["quota_gap"]["languages"], "配额缺口必须逐语种列出"


def test_quota_gap_arithmetic(ran_service):
    facts = collect_facts(ran_service.store, ran_service.policy)
    gaps = quota_gap(facts, ran_service.policy)
    for row in gaps["languages"]:
        assert row["gap"] == max(0, row["required"] - row["accepted"])
        assert row["met"] == (row["accepted"] >= row["required"])
    assert gaps["total_required_documents"] == 20 * len(ran_service.policy.target_languages)


def test_all_decisions_csv_has_deciding_reason(ran_service):
    ran_service.report()
    path = os.path.join(ran_service.workspace_dir, "all_decisions.csv")
    assert os.path.isfile(path)
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows
    assert "deciding_reason" in rows[0]
    for row in rows:
        assert row["source_path"].startswith(("知识库_2026/", "docQA.llm/"))
        assert row["reason_codes"]
        if row["status"] == "rejected":
            assert row["deciding_reason"], "淘汰行必须能一眼看出原因"
            assert C.is_explanatory_reason(row["deciding_reason"])


def test_screening_csv_has_deciding_reason(ran_service):
    for name in ("accepted_documents.csv", "rejected_documents.csv", "human_review_queue.csv"):
        path = os.path.join(ran_service.workspace_dir, name)
        assert os.path.isfile(path), name
        with open(path, encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            assert "deciding_reason" in (reader.fieldnames or []), name
            for row in reader:
                assert row["source_path"]
                assert row["reason_codes"]


def test_cost_and_runtime_records_llm_usage(ran_service):
    ran_service.report()
    path = os.path.join(ran_service.workspace_dir, "cost_and_runtime.json")
    payload = json.load(open(path, encoding="utf-8"))
    assert payload["run_id"] == "report-test"
    assert "llm" in payload and "counts" in payload and "stages" in payload
    assert payload["counts"]["items"] == 22


def test_report_is_idempotent(ran_service):
    first = ran_service.report().summary["facts"]
    second = ran_service.report().summary["facts"]
    assert first == second
