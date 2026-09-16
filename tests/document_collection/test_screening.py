"""硬筛选规则测试：顺序、不可覆盖性、可解释性。

对应验收检查点 3「没有误放行」和检查点 4「结果可解释」。
"""

from __future__ import annotations

import pytest

from core.document_collection.contracts import (
    ITEM_ACCEPTED,
    ITEM_CONVERSION_REQUIRED,
    ITEM_NEEDS_REVIEW,
    ITEM_PENDING_EXPORT,
    ITEM_REJECTED,
)
from core.document_collection.screening import ScreeningOptions, hard_screen

MB = 1024 * 1024


def make_item(**over):
    base = {
        "item_id": "T1", "item_token": "T1", "name": "cours.pdf",
        "source_name": "知识库_2026", "item_type": "local_file",
        "extension": "pdf", "status": "extracted",
        "metadata": {"relative_path": "DOC-FR/cours.pdf"},
        "url": "https://example.feishu.cn/file/T1",
    }
    base.update(over)
    return base


def make_artifact(size_bytes=1 * MB, status="acquired", path="/tmp/x.pdf", **over):
    base = {"item_id": "T1", "size_bytes": size_bytes, "status": status,
            "path": path, "sha256": "a" * 64, "error": None}
    base.update(over)
    return base


def make_extraction(char_count=8000, language="fr", confidence=0.9, status="extracted",
                    page_count=10, image_area_ratio=0.1, **over):
    base = {"item_id": "T1", "char_count": char_count, "language": language,
            "language_confidence": confidence, "status": status,
            "page_count": page_count, "image_area_ratio": image_area_ratio,
            "extractor": "pdf", "error": None}
    base.update(over)
    return base


_UNSET = object()


def screen(policy, *, item=_UNSET, artifact=_UNSET, extraction=_UNSET,
           dedup=None, acquire=None, opts=None):
    """artifact/extraction 传 None 表示"确实没有"，不能和"用默认值"混为一谈。"""
    return hard_screen(
        item=make_item() if item is _UNSET else item,
        artifact=make_artifact() if artifact is _UNSET else artifact,
        extraction=make_extraction() if extraction is _UNSET else extraction,
        dedup_decision=dedup, acquire_decision=acquire,
        policy=policy, opts=opts or ScreeningOptions())


def test_clean_document_is_accepted_with_reason_trail(real_policy):
    decision = screen(real_policy)
    assert decision["status"] == ITEM_ACCEPTED
    assert decision["hard_failed"] is False
    assert "ok" in decision["reason_codes"]
    assert "extension_supported" in decision["reason_codes"]
    assert "size_within_limit" in decision["reason_codes"]
    assert "language_in_scope" in decision["reason_codes"]
    assert decision["evidence"]["size_bytes"] == 1 * MB
    assert decision["evidence"]["limit_mb"] == 100


@pytest.mark.parametrize("size_bytes,ext,limit", [
    (101 * MB, "pdf", 100),
    (21 * MB, "txt", 20),
    (21 * MB, "csv", 20),
    (101 * MB, "pptx", 100),
])
def test_oversize_is_hard_rejected(real_policy, size_bytes, ext, limit):
    decision = screen(real_policy, item=make_item(extension=ext),
                      artifact=make_artifact(size_bytes=size_bytes))
    assert decision["status"] == ITEM_REJECTED
    assert decision["hard_failed"] is True
    assert "size_over_limit" in decision["reason_codes"]
    assert decision["evidence"]["limit_mb"] == limit


def test_size_limit_boundary_is_inclusive(real_policy):
    """正好等于上限应该通过（需求写的是"上限"，不是"必须小于"）。"""
    decision = screen(real_policy, artifact=make_artifact(size_bytes=100 * MB))
    assert decision["status"] == ITEM_ACCEPTED
    one_over = screen(real_policy, artifact=make_artifact(size_bytes=100 * MB + 1))
    assert one_over["status"] == ITEM_REJECTED


@pytest.mark.parametrize("ext,expected", [
    ("jpg", ITEM_REJECTED),
    ("zip", ITEM_REJECTED),
    ("html", ITEM_REJECTED),
    ("doc", ITEM_CONVERSION_REQUIRED),
    ("ppt", ITEM_CONVERSION_REQUIRED),
    ("xls", ITEM_CONVERSION_REQUIRED),
])
def test_unsupported_and_legacy_formats(real_policy, ext, expected):
    decision = screen(real_policy, item=make_item(extension=ext))
    assert decision["status"] == expected


def test_shortcut_and_unknown_type_never_reach_accepted(real_policy):
    shortcut = screen(real_policy, item=make_item(item_type="shortcut", extension="url"))
    assert shortcut["status"] == ITEM_REJECTED
    unknown = screen(real_policy, item=make_item(item_type="unknown", extension=None))
    assert unknown["status"] in (ITEM_NEEDS_REVIEW, ITEM_REJECTED)


def test_out_of_scope_language_is_rejected(real_policy):
    for lang in ("de", "it", "pt", "tr", "ja", "ko", "hi", "bn"):
        decision = screen(real_policy, extraction=make_extraction(language=lang))
        assert decision["status"] == ITEM_REJECTED, lang
        assert "language_not_in_scope" in decision["reason_codes"]


@pytest.mark.parametrize("lang", ["en", "ru", "fr", "zh", "ar", "es"])
def test_all_p0_languages_pass_the_language_gate(real_policy, lang):
    assert screen(real_policy, extraction=make_extraction(language=lang))["status"] == ITEM_ACCEPTED


def test_language_confidence_threshold_comes_from_policy(real_policy):
    """门槛写在策略里，不在代码里写死；默认 0.70。"""
    assert real_policy.min_language_confidence == 0.70


def test_in_scope_language_with_medium_confidence_goes_to_review(real_policy):
    """真实 Pilot 教训：斯瓦希里语论文以 0.54 置信度被判成 es 并误放行。

    P0 语种但置信度不够时，既不能放行（会污染语种配额），
    也不该直接淘汰（可能真的是 P0 样本），转人工复核最稳。
    """
    decision = screen(real_policy, extraction=make_extraction(language="es", confidence=0.54))
    assert decision["status"] == ITEM_NEEDS_REVIEW
    assert "language_ambiguous" in decision["reason_codes"]
    assert decision["evidence"]["language_confidence_threshold"] == 0.70
    assert decision["evidence"]["language_ambiguous_reason"] == "confidence<0.7"


def test_in_scope_language_with_high_confidence_is_accepted(real_policy):
    decision = screen(real_policy, extraction=make_extraction(language="es", confidence=0.95))
    assert decision["status"] == ITEM_ACCEPTED
    assert "language_in_scope" in decision["reason_codes"]


def test_out_of_scope_language_with_high_confidence_is_rejected(real_policy):
    for lang in ("sw", "ur", "bn", "ta", "gu", "id", "tl", "ha", "hi"):
        decision = screen(real_policy, extraction=make_extraction(language=lang, confidence=1.0))
        assert decision["status"] == ITEM_REJECTED, lang
        assert "language_not_in_scope" in decision["reason_codes"]


def test_caller_can_override_the_confidence_threshold(real_policy):
    strict = screen(real_policy, extraction=make_extraction(language="fr", confidence=0.6),
                    opts=ScreeningOptions(language_confidence_threshold=0.9))
    assert strict["status"] == ITEM_NEEDS_REVIEW
    loose = screen(real_policy, extraction=make_extraction(language="fr", confidence=0.6),
                   opts=ScreeningOptions(language_confidence_threshold=0.3))
    assert loose["status"] == ITEM_ACCEPTED


def test_ambiguous_language_goes_to_review_not_reject(real_policy):
    decision = screen(real_policy, extraction=make_extraction(language="unknown", confidence=0.0))
    assert decision["status"] == ITEM_NEEDS_REVIEW
    assert "language_ambiguous" in decision["reason_codes"]
    low_conf = screen(real_policy, extraction=make_extraction(language="fr", confidence=0.1))
    assert low_conf["status"] == ITEM_NEEDS_REVIEW


def test_graphics_dominated_is_rejected_with_evidence(real_policy):
    decision = screen(real_policy, extraction=make_extraction(
        char_count=0, status="empty", page_count=20, image_area_ratio=0.95,
        language="unknown", confidence=0.0))
    assert decision["status"] == ITEM_REJECTED
    assert "graphics_dominated" in decision["reason_codes"]
    assert decision["hard_failed"] is True
    assert decision["evidence"]["graphics_reasons"]


def test_sparse_text_without_image_evidence_goes_to_review(real_policy):
    """不能把"文字少"当成"图形主导"淘汰，理由是错的。"""
    decision = screen(real_policy, extraction=make_extraction(
        char_count=200, page_count=1, image_area_ratio=0.05))
    assert decision["status"] == ITEM_NEEDS_REVIEW
    assert "text_too_sparse" in decision["reason_codes"]
    assert "graphics_dominated" not in decision["reason_codes"]


def test_illustrated_textbook_is_not_rejected(real_policy):
    decision = screen(real_policy, extraction=make_extraction(
        char_count=60000, page_count=120, image_area_ratio=0.7))
    assert decision["status"] == ITEM_ACCEPTED


def test_long_document_is_flagged_not_rejected(real_policy):
    """需求 2.2 / 默认口径 1：超过 5 万字不淘汰，走全文理解。"""
    decision = screen(real_policy, extraction=make_extraction(char_count=120000))
    assert decision["status"] == ITEM_ACCEPTED
    assert "requires_full_text_review" in decision["reason_codes"]
    assert decision["evidence"]["is_long_document"] is True


def test_exact_duplicate_is_rejected(real_policy):
    dedup = {"status": ITEM_REJECTED, "reason_codes": ["exact_duplicate"],
             "evidence": {"representative": "T0"}}
    decision = screen(real_policy, dedup=dedup)
    assert decision["status"] == ITEM_REJECTED
    assert "exact_duplicate" in decision["reason_codes"]
    assert decision["evidence"]["duplicate_of"] == "T0"


def test_normalized_duplicate_is_rejected(real_policy):
    dedup = {"status": ITEM_REJECTED, "reason_codes": ["normalized_duplicate"],
             "evidence": {"representative": "T0"}}
    assert screen(real_policy, dedup=dedup)["status"] == ITEM_REJECTED


def test_near_duplicate_suspect_is_not_rejected(real_policy):
    """计划 Task 5：疑似近重复只标记，不自动删除。"""
    dedup = {"status": "near_duplicate_suspect", "reason_codes": ["near_duplicate_suspect"],
             "evidence": {"marked_only": True}}
    decision = screen(real_policy, dedup=dedup)
    assert decision["status"] == ITEM_ACCEPTED


def test_pending_export_and_conversion_required_pass_through(real_policy):
    pending = screen(real_policy, artifact=make_artifact(status=ITEM_PENDING_EXPORT, path=None))
    assert pending["status"] == ITEM_PENDING_EXPORT
    conv = screen(real_policy, artifact=make_artifact(status=ITEM_CONVERSION_REQUIRED, path=None),
                  item=make_item(extension="doc"))
    assert conv["status"] == ITEM_CONVERSION_REQUIRED


def test_acquire_hard_rejection_is_not_softened(real_policy):
    """回归：超限文件的 blob 已删，不能因为 path 为空就变成 needs_review。"""
    acquire = {"status": ITEM_REJECTED, "reason_codes": ["size_over_limit"],
               "evidence": {"size_bytes": 200 * MB, "limit_mb": 100}}
    decision = screen(real_policy, acquire=acquire,
                      artifact=make_artifact(status=ITEM_REJECTED, path=None, size_bytes=None),
                      extraction=None)
    assert decision["status"] == ITEM_REJECTED
    assert decision["hard_failed"] is True
    assert "size_over_limit" in decision["reason_codes"]


def test_extract_failure_goes_to_review(real_policy):
    decision = screen(real_policy, extraction=make_extraction(
        status="failed", char_count=0, error="打开 PDF 失败"))
    assert decision["status"] == ITEM_NEEDS_REVIEW
    assert "extract_failed" in decision["reason_codes"]


def test_missing_artifact_goes_to_review(real_policy):
    decision = screen(real_policy, artifact=None)
    assert decision["status"] == ITEM_NEEDS_REVIEW
    assert "artifact_missing" in decision["reason_codes"]


def test_every_decision_carries_the_four_required_fields(real_policy):
    """验收检查点 4：status / reason_codes / source_path / artifact_path。"""
    for kwargs in ({}, {"artifact": make_artifact(size_bytes=200 * MB)},
                   {"extraction": make_extraction(language="de")},
                   {"artifact": None}):
        decision = screen(real_policy, **kwargs)
        assert decision["status"]
        assert isinstance(decision["reason_codes"], list) and decision["reason_codes"]
        assert isinstance(decision["evidence"], dict)


def test_semantic_review_needs_evidence_to_accept(real_policy):
    from conftest import FakeLLM
    from core.document_collection.screening import semantic_review
    base = screen(real_policy)
    no_evidence = FakeLLM(classify={
        "education_level": "university", "subject": "law", "queryability": "high",
        "exclude_kind": None, "evidence": [], "confidence": 0.99,
        "needs_human_review": False,
    })
    out = semantic_review(decision=base, llm=no_evidence, document_ref={},
                          policy=real_policy, opts=ScreeningOptions())
    assert out["status"] == ITEM_NEEDS_REVIEW
    assert "llm_no_evidence" in out["reason_codes"]


def test_semantic_review_rejects_excluded_kinds(real_policy):
    from conftest import FakeLLM
    from core.document_collection.screening import semantic_review
    base = screen(real_policy)
    llm = FakeLLM(classify={
        "education_level": "university", "subject": "law", "queryability": "high",
        "exclude_kind": "annual_report", "evidence": ["x"], "confidence": 0.9,
        "needs_human_review": False,
    })
    out = semantic_review(decision=base, llm=llm, document_ref={},
                          policy=real_policy, opts=ScreeningOptions())
    assert out["status"] == ITEM_REJECTED
    assert "excluded_kind=annual_report" in out["reason_codes"]


def test_semantic_review_rejects_non_university_material(real_policy):
    from conftest import FakeLLM
    from core.document_collection.screening import semantic_review
    base = screen(real_policy)
    for level, expected in (("school", ITEM_REJECTED), ("general", ITEM_REJECTED),
                            ("vocational", ITEM_NEEDS_REVIEW), ("unknown", ITEM_NEEDS_REVIEW)):
        llm = FakeLLM(classify={
            "education_level": level, "subject": "law", "queryability": "high",
            "exclude_kind": None, "evidence": ["x"], "confidence": 0.9,
            "needs_human_review": False,
        })
        out = semantic_review(decision=base, llm=llm, document_ref={},
                              policy=real_policy, opts=ScreeningOptions())
        assert out["status"] == expected, level


def test_semantic_review_low_queryability(real_policy):
    from conftest import FakeLLM
    from core.document_collection.screening import semantic_review
    base = screen(real_policy)
    for level, expected in (("low", ITEM_REJECTED), ("medium", ITEM_NEEDS_REVIEW)):
        llm = FakeLLM(classify={
            "education_level": "university", "subject": "law", "queryability": level,
            "exclude_kind": None, "evidence": ["x"], "confidence": 0.9,
            "needs_human_review": False,
        })
        out = semantic_review(decision=base, llm=llm, document_ref={},
                              policy=real_policy, opts=ScreeningOptions())
        assert out["status"] == expected, level


def test_semantic_review_failure_is_recoverable(real_policy):
    from conftest import FakeLLM
    from core.document_collection.screening import semantic_review
    base = screen(real_policy)
    llm = FakeLLM(raise_on="classify")
    out = semantic_review(decision=base, llm=llm, document_ref={},
                          policy=real_policy, opts=ScreeningOptions())
    assert out["status"] == ITEM_NEEDS_REVIEW
    assert "llm_output_invalid" in out["reason_codes"]
    assert out["evidence"]["llm_error"]


def test_semantic_review_does_not_touch_hard_rejected(real_policy):
    from conftest import FakeLLM
    from core.document_collection.screening import semantic_review
    rejected = screen(real_policy, artifact=make_artifact(size_bytes=200 * MB))
    llm = FakeLLM()
    out = semantic_review(decision=rejected, llm=llm, document_ref={},
                          policy=real_policy, opts=ScreeningOptions())
    assert out["status"] == ITEM_REJECTED
    assert llm.calls == [], "硬失败不应该浪费模型调用"
