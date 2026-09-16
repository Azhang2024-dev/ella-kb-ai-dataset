"""策略加载与校验测试。

重点验证「业务约束自相矛盾时必须报错」，因为这些数字一旦写错，
后面所有筛选结果都会静默偏差。
"""

from __future__ import annotations

import json
import os

import pytest

from core.document_collection.config import (
    CollectionPolicy,
    PolicyError,
    describe_policy,
    load_policy,
    validate_policy,
)

POLICY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "collection_policy.json")


def _mutate(raw, **changes):
    out = json.loads(json.dumps(raw, ensure_ascii=False))
    for key, value in changes.items():
        target = out
        parts = key.split("__")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    return out


def test_real_policy_loads_and_validates():
    policy = load_policy(POLICY_PATH)
    assert validate_policy(policy) == []


def test_real_policy_encodes_the_authoritative_requirements(real_policy):
    """与在线采集计划 revision 201 的口径逐条对齐。"""
    assert [s["name"] for s in real_policy.sources] == ["知识库_2026", "docQA.llm"]
    assert real_policy.size_limits_mb["pdf"] == 100
    assert real_policy.size_limits_mb["docx"] == 100
    assert real_policy.size_limits_mb["pptx"] == 100
    assert real_policy.size_limits_mb["xlsx"] == 20
    assert real_policy.size_limits_mb["csv"] == 20
    assert real_policy.size_limits_mb["txt"] == 20
    assert real_policy.size_limits_mb["md"] == 20
    assert real_policy.target_languages == ("en", "ru", "fr", "zh", "ar", "es")
    assert real_policy.min_documents_per_language == 20
    assert real_policy.queries_per_document == 10
    assert real_policy.full_text_review_threshold_chars == 50000
    assert real_policy.source_readonly is True
    revisions = real_policy.section("requirement_revisions")
    assert revisions["collection_plan_doc"]["revision_id"] == 201


def test_query_type_table_matches_the_plan_exactly(real_policy):
    table = {t["type_key"]: (t["count"], t["question_count"], bool(t.get("compatibility_only")))
             for t in real_policy.query_types}
    assert table == {
        "multiple_choice": (2, 5, False),
        "fill_in_blank": (2, 3, False),
        "short_answer": (2, 2, False),
        "true_false": (2, 7, False),
        "mixed": (2, 9, True),
    }
    assert sum(c for c, _q, _m in table.values()) == 10
    question_counts = [q for _c, q, _m in table.values()]
    assert len(set(question_counts)) == len(question_counts), "题量必须互不重复"
    assert max(question_counts) < 12, "题量必须小于 12"


@pytest.mark.parametrize("changes,expected_fragment", [
    ({"query_plan__queries_per_document": 12}, "不一致"),
    ({"languages__min_documents_per_language": 0}, "必须大于 0"),
    ({"safety__source_readonly": False}, "source_readonly"),
    ({"query_plan__whole_document_only": False}, "whole_document_only"),
    ({"size_limits_mb": {"pdf": 100}}, "缺少 size_limits_mb"),
    ({"formats__conversion_required_extensions": ["pdf"]}, "同时出现"),
])
def test_validation_catches_contradictions(real_policy, changes, expected_fragment):
    raw = _mutate(real_policy.raw, **changes)
    policy = CollectionPolicy(path=POLICY_PATH, raw=raw)
    errors = validate_policy(policy)
    assert errors, f"{changes} 应该被判为非法"
    assert any(expected_fragment in e for e in errors), errors


def test_question_count_above_11_is_rejected(real_policy):
    raw = json.loads(json.dumps(real_policy.raw, ensure_ascii=False))
    raw["query_plan"]["query_types"][0]["question_count"] = 12
    policy = CollectionPolicy(path=POLICY_PATH, raw=raw)
    errors = validate_policy(policy)
    assert any("超出允许区间" in e for e in errors), errors


def test_repeated_question_counts_are_rejected(real_policy):
    raw = json.loads(json.dumps(real_policy.raw, ensure_ascii=False))
    raw["query_plan"]["query_types"][1]["question_count"] = 5   # 与选择题重复
    policy = CollectionPolicy(path=POLICY_PATH, raw=raw)
    errors = validate_policy(policy)
    assert any("题量重复" in e for e in errors), errors


def test_source_without_readonly_flag_is_rejected(real_policy):
    raw = json.loads(json.dumps(real_policy.raw, ensure_ascii=False))
    raw["sources"][0]["readonly"] = False
    policy = CollectionPolicy(path=POLICY_PATH, raw=raw)
    assert any("readonly" in e for e in validate_policy(policy))


def test_source_without_name_is_rejected(real_policy):
    raw = json.loads(json.dumps(real_policy.raw, ensure_ascii=False))
    raw["sources"][1]["name"] = ""
    policy = CollectionPolicy(path=POLICY_PATH, raw=raw)
    assert any("name" in e for e in validate_policy(policy))


def test_load_policy_raises_with_all_errors(tmp_path_factory=None):
    broken = {"schema_version": "0.1", "sources": []}
    policy = CollectionPolicy(path="broken.json", raw=broken)
    errors = validate_policy(policy)
    assert len(errors) > 3


def test_load_policy_missing_file():
    with pytest.raises(PolicyError):
        load_policy(os.path.join("config", "does_not_exist_policy.json"))


def test_load_policy_invalid_json(workdir):
    path = os.path.join(workdir, "bad.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    with pytest.raises(PolicyError) as exc:
        load_policy(path)
    assert "不是合法 JSON" in str(exc.value)


def test_build_run_config_rejects_business_overrides(real_policy):
    with pytest.raises(PolicyError):
        real_policy.build_run_config("run-1", max_size_mb={"pdf": 999})
    with pytest.raises(PolicyError):
        real_policy.build_run_config("run-1", query_plan={})


def test_build_run_config_allows_operational_overrides(real_policy):
    cfg = real_policy.build_run_config("run-2", dry_run=True, llm_enabled=True,
                                       workspace_dir="output/x")
    assert cfg.dry_run is True
    assert cfg.llm_enabled is True
    assert cfg.workspace_dir == "output/x"
    assert cfg.max_size_mb["pdf"] == 100
    assert cfg.queries_per_document == 10


def test_run_id_must_be_filesystem_safe(real_policy):
    for bad in ("", "a/b", "..", ".", "x" * 200, "run id with space", "-lead", "_lead"):
        with pytest.raises(PolicyError):
            real_policy.build_run_config(bad)
    for good in ("pilot-20260909", "run_1", "A.b-c", "2026"):
        assert real_policy.build_run_config(good).run_id == good


def test_describe_policy_never_leaks_secrets(real_policy):
    text = json.dumps(describe_policy(real_policy), ensure_ascii=False)
    for secret_key in ("llm_api_key", "mineru_token", "sk-"):
        assert secret_key not in text
