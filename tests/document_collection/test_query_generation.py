"""Query 生成与校验测试（需求第四章：10 条 / 题量 < 12 / 整篇文档 / 语言跟随）。"""

from __future__ import annotations

import os

import pytest

from conftest import QUERY_TEMPLATES, build_valid_queries

from core.document_collection.llm import (
    LLMOutputInvalid,
    OfflineLLMAdapter,
    build_model_name,
    extract_json_object,
)
from core.document_collection.query_generation import (
    QueryOptions,
    find_fragment_references,
    generate_for_item,
    validate_queries,
    write_query_validation_report,
)


def validate(policy, queries, language="fr", **opts):
    return validate_queries(queries, policy=policy, document_language=language,
                            opts=QueryOptions(**opts))


def test_valid_ten_query_set_passes_for_every_p0_language(real_policy):
    for language in real_policy.target_languages:
        queries = build_valid_queries(real_policy.query_plan, language)
        report = validate(real_policy, queries, language)
        assert report["ok"] is True, (language, report["errors"])
        assert report["query_count"] == 10
        assert report["type_counts"] == {"multiple_choice": 2, "fill_in_blank": 2,
                                         "short_answer": 2, "true_false": 2, "mixed": 2}


def test_question_counts_are_all_below_12_and_distinct(real_policy):
    queries = build_valid_queries(real_policy.query_plan, "fr")
    counts = [q["question_count"] for q in queries]
    assert max(counts) < 12
    assert sorted(set(counts)) == [2, 3, 5, 7, 9]
    report = validate(real_policy, queries, "fr")
    assert not any(w["code"] == "question_count_repeated" for w in report["warnings"])


def test_wrong_total_count_is_an_error(real_policy):
    queries = build_valid_queries(real_policy.query_plan, "fr")[:9]
    report = validate(real_policy, queries, "fr")
    assert report["ok"] is False
    assert any(e["code"] == "query_count_mismatch" for e in report["errors"])


def test_wrong_per_type_count_is_an_error(real_policy):
    queries = build_valid_queries(real_policy.query_plan, "fr")
    queries[1]["type_key"] = "true_false"      # 选择题少一条、判断题多一条
    report = validate(real_policy, queries, "fr")
    assert report["ok"] is False
    messages = " ".join(e["message"] for e in report["errors"])
    assert "multiple_choice" in messages and "true_false" in messages


def test_unknown_query_type_is_an_error(real_policy):
    queries = build_valid_queries(real_policy.query_plan, "fr")
    queries[0]["type_key"] = "essay"
    report = validate(real_policy, queries, "fr")
    assert report["ok"] is False
    assert any("未定义题型" in e["message"] for e in report["errors"])


@pytest.mark.parametrize("bad_count", [1, 12, 15, 100])
def test_question_count_must_be_below_12(real_policy, bad_count):
    queries = build_valid_queries(real_policy.query_plan, "fr")
    queries[0]["question_count"] = bad_count
    report = validate(real_policy, queries, "fr")
    assert report["ok"] is False
    assert any(e["code"] == "question_count_out_of_range" for e in report["errors"])


def test_repeated_question_counts_across_types_warn(real_policy):
    queries = build_valid_queries(real_policy.query_plan, "fr")
    for query in queries:
        query["question_count"] = 5
    report = validate(real_policy, queries, "fr")
    assert any(w["code"] == "question_count_repeated" for w in report["warnings"])


# ---------------------------------------------------------------------------
# 整篇文档：定位词黑名单
# ---------------------------------------------------------------------------
FRAGMENT_SAMPLES = {
    "zh": "请根据第三章第 2 节的内容出 5 道选择题。",
    "en": "See chapter 3 and page 12, then create 5 multiple-choice questions.",
    "fr": "Voir le chapitre 3 et la page 12, puis creez 5 questions a choix multiples.",
    "es": "Consulta el capitulo 3 y la pagina 12 y genera 5 preguntas de opcion multiple.",
    "ru": "См. глава 3 и страница 12, составьте 5 вопросов с выбором ответа.",
    "ar": "راجع الفصل 3 والصفحة 12 ثم أنشئ 5 أسئلة اختيار من متعدد.",
}


@pytest.mark.parametrize("language", sorted(FRAGMENT_SAMPLES))
def test_fragment_references_are_detected_in_every_p0_language(real_policy, language):
    hits = find_fragment_references(FRAGMENT_SAMPLES[language])
    assert hits, f"{language} 的定位词没被识别出来"
    queries = build_valid_queries(real_policy.query_plan, language)
    queries[0]["text"] = FRAGMENT_SAMPLES[language]
    report = validate(real_policy, queries, language)
    assert report["ok"] is False
    assert any(e["code"] == "query_references_fragment" for e in report["errors"])


def test_table_and_figure_references_are_detected(real_policy):
    for text in ("如表 3 所示，请出 5 道选择题。",
                 "根据下表内容出题。",
                 "As shown in figure 4, create 5 questions.",
                 "Use the data in table 2 to create 5 questions.",
                 "Voir le tableau 5 et la figure 2."):
        assert find_fragment_references(text), text


def test_whole_document_wording_is_allowed(real_policy):
    for text in ("请根据这份文档整体出 5 道选择题。",
                 "Generate 5 multiple-choice questions based on this whole document.",
                 "Redigez 5 questions a partir de ce document entier.",
                 "Составьте 5 вопросов по всему этому документу."):
        assert find_fragment_references(text) == [], text


def test_language_mismatch_is_an_error(real_policy):
    queries = build_valid_queries(real_policy.query_plan, "en")   # 英文文案
    report = validate(real_policy, queries, "ru")                 # 文档是俄文
    assert report["ok"] is False
    assert sum(1 for e in report["errors"] if e["code"] == "query_language_mismatch") == 10


def test_declared_language_mismatch_is_a_warning(real_policy):
    queries = build_valid_queries(real_policy.query_plan, "fr")
    queries[0]["language"] = "de"
    report = validate(real_policy, queries, "fr")
    assert any(w["code"] == "query_language_mismatch" for w in report["warnings"])


def test_duplicate_query_text_is_an_error(real_policy):
    queries = build_valid_queries(real_policy.query_plan, "fr")
    queries[1]["text"] = queries[0]["text"]
    report = validate(real_policy, queries, "fr")
    assert any(e["code"] == "query_duplicate" for e in report["errors"])


def test_punctuation_and_case_differences_still_count_as_duplicate(real_policy):
    queries = build_valid_queries(real_policy.query_plan, "fr")
    queries[1]["text"] = queries[0]["text"].upper().replace(".", " ! ")
    report = validate(real_policy, queries, "fr")
    assert any(e["code"] == "query_duplicate" for e in report["errors"])


def test_missing_evidence_locator_is_an_error(real_policy):
    queries = build_valid_queries(real_policy.query_plan, "fr")
    queries[3]["evidence_locator"] = {}
    report = validate(real_policy, queries, "fr")
    assert any(e["code"] == "query_missing_evidence" for e in report["errors"])
    # 内部台账可以关掉，但那是显式选择
    relaxed = validate(real_policy, queries, "fr", require_evidence_locator=False)
    assert not any(e["code"] == "query_missing_evidence" for e in relaxed["errors"])


def test_mixed_type_must_be_flagged_compatibility_only(real_policy):
    queries = build_valid_queries(real_policy.query_plan, "fr")
    mixed = [q for q in queries if q["type_key"] == "mixed"]
    assert len(mixed) == 2
    assert all(q["compatibility_only"] for q in mixed)
    mixed[0]["compatibility_only"] = False
    report = validate(real_policy, queries, "fr")
    assert any(w["code"] == "compatibility_flag_missing" for w in report["warnings"])


# ---------------------------------------------------------------------------
# generate_for_item
# ---------------------------------------------------------------------------
def _seed_item(store, workspace_dir, item_id="T1", language="fr", text=None):
    from conftest import FR_TEXT
    body = text if text is not None else FR_TEXT
    store.upsert_item({
        "item_id": item_id, "run_id": "r", "source_name": "知识库_2026",
        "source_folder_path": "DOC-FR", "item_token": item_id, "name": "cours.pdf",
        "item_type": "local_file", "extension": "pdf", "relative_path": "DOC-FR/cours.pdf",
        "status": "accepted", "stage": "screen",
        "metadata": {"relative_path": "DOC-FR/cours.pdf", "source_role": "primary"},
    })
    store.upsert_extraction(item_id, extractor="pdf", status="extracted",
                            char_count=len(body), language=language, page_count=12)
    text_dir = os.path.join(workspace_dir, "extracted")
    os.makedirs(text_dir, exist_ok=True)
    with open(os.path.join(text_dir, f"{item_id}.txt"), "w", encoding="utf-8") as fh:
        fh.write(body)
    store.commit()
    return store.get_item(item_id), store.get_extraction(item_id)


def test_generate_for_item_produces_validated_queries(real_policy, fake_llm, workdir):
    from core.document_collection.state import StateStore
    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        item, extraction = _seed_item(store, workdir)
        record = generate_for_item(item=item, extraction=extraction, policy=real_policy,
                                   llm=fake_llm, workspace_dir=workdir,
                                   opts=QueryOptions())
        assert record["status"] == "validated", record
        assert len(record["queries"]) == 10
        assert record["validation"]["ok"] is True
    finally:
        store.close()


def test_generate_for_item_without_llm_does_not_fabricate(real_policy, workdir):
    from core.document_collection.state import StateStore
    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        item, extraction = _seed_item(store, workdir)
        record = generate_for_item(item=item, extraction=extraction, policy=real_policy,
                                   llm=OfflineLLMAdapter(), workspace_dir=workdir,
                                   opts=QueryOptions())
        assert record["status"] == "needs_review"
        assert record["queries"] == []
        assert "llm_unavailable" in record["reason_codes"]
    finally:
        store.close()


def test_generate_for_item_handles_malformed_model_output(real_policy, workdir):
    from core.document_collection.state import StateStore

    class BrokenLLM:
        name = "broken"
        available = True

        def classify_document(self, *_a, **_k):
            return {}

        def generate_queries(self, *_a, **_k):
            return {"not_queries": True}

    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        item, extraction = _seed_item(store, workdir)
        record = generate_for_item(item=item, extraction=extraction, policy=real_policy,
                                   llm=BrokenLLM(), workspace_dir=workdir,
                                   opts=QueryOptions())
        assert record["status"] == "needs_review"
        assert "llm_output_invalid" in record["reason_codes"]
    finally:
        store.close()


def test_budget_exhausted_is_classified_separately_from_bad_output(real_policy, workdir):
    """推理预算被吃光（finish_reason=length）必须报成 llm_budget_exhausted，
    而不是 llm_output_invalid —— 前者要调大 max_tokens，后者要修提示词。"""
    from core.document_collection.state import StateStore
    from core.document_collection.llm import LLMBudgetExhausted

    class BudgetExhaustedLLM:
        name = "budget_exhausted"
        available = True

        def classify_document(self, *_a, **_k):
            return {}

        def generate_queries(self, *_a, **_k):
            raise LLMBudgetExhausted("finish_reason=length，max_tokens=16000")

    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        item, extraction = _seed_item(store, workdir)
        record = generate_for_item(item=item, extraction=extraction, policy=real_policy,
                                   llm=BudgetExhaustedLLM(), workspace_dir=workdir,
                                   opts=QueryOptions())
        assert record["status"] == "needs_review"
        assert record["reason_codes"] == ["llm_budget_exhausted"], record["reason_codes"]
        assert "finish_reason=length" in record["error"]
    finally:
        store.close()


def test_generate_for_item_without_text_goes_to_review(real_policy, fake_llm, workdir):
    from core.document_collection.state import StateStore
    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        item, extraction = _seed_item(store, workdir, item_id="T_EMPTY", text="")
        record = generate_for_item(item=item, extraction=extraction, policy=real_policy,
                                   llm=fake_llm, workspace_dir=workdir,
                                   opts=QueryOptions())
        assert record["status"] == "needs_review"
        assert "no_text_for_query_generation" in record["reason_codes"]
    finally:
        store.close()


def test_validation_report_is_written(real_policy, workdir):
    records = [{
        "item_id": "T1", "name": "cours.pdf", "source_name": "知识库_2026",
        "document_language": "fr", "status": "validated", "reason_codes": [],
        "queries": build_valid_queries(real_policy.query_plan, "fr"),
        "validation": {"ok": True, "errors": [], "warnings": []},
        "error": None,
    }]
    path = os.path.join(workdir, "query_validation_report.md")
    write_query_validation_report(path, records=records, policy=real_policy,
                                  run_id="r", elapsed_sec=1.5,
                                  llm_stats={"calls": 1, "ok_calls": 1,
                                             "prompt_chars": 100, "output_chars": 200,
                                             "latency_ms": 900})
    text = open(path, encoding="utf-8").read()
    assert "Query 生成校验报告" in text
    assert "校验通过：1" in text
    assert "模型调用：1 次" in text


# ---------------------------------------------------------------------------
# LLM 输出解析
# ---------------------------------------------------------------------------
def test_extract_json_object_handles_fences_and_prose():
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json_object('好的，结果如下：\n{"a": 3}\n希望有帮助') == {"a": 3}
    assert extract_json_object('[{"a": 4}]') == {"items": [{"a": 4}]}
    with pytest.raises(LLMOutputInvalid):
        extract_json_object("完全没有 JSON")
    with pytest.raises(LLMOutputInvalid):
        extract_json_object("")


def test_build_model_name_matches_existing_convention():
    assert build_model_name({"llm_provider": "openai", "llm_model": "gpt-4o"}) == "openai/gpt-4o"
    assert build_model_name({"llm_provider": "custom", "llm_model": "m"}) == "openai/m"
    assert build_model_name({"llm_provider": "openai", "llm_model": ""}) == "openai/gpt-4o-mini"
    # 已经带 provider 前缀时不要重复拼
    assert build_model_name({"llm_provider": "alibaba",
                             "llm_model": "alibaba/qwen3.7-plus"}) == "alibaba/qwen3.7-plus"


def test_offline_adapter_never_pretends_to_work():
    from core.document_collection.llm import LLMUnavailable
    adapter = OfflineLLMAdapter()
    assert adapter.available is False
    with pytest.raises(LLMUnavailable):
        adapter.classify_document({}, {})
    with pytest.raises(LLMUnavailable):
        adapter.generate_queries({}, {})


def test_litellm_adapter_raises_budget_exhausted_on_length_finish(monkeypatch):
    """真实适配器遇到 finish_reason=length 且正文为空时，必须抛 LLMBudgetExhausted。"""
    import sys
    import types as _types
    from core.document_collection.llm import LiteLLMAdapter, LLMBudgetExhausted

    class _Msg:
        content = ""            # 推理吃光预算 -> 正文为空

    class _Choice:
        message = _Msg()
        finish_reason = "length"

    class _Usage:
        class _Details(dict):
            pass

        completion_tokens_details = {"reasoning_tokens": 11900}

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    fake = _types.SimpleNamespace(completion=lambda **_k: _Resp())
    monkeypatch.setitem(sys.modules, "litellm", fake)

    adapter = LiteLLMAdapter({"llm_model": "alibaba/deepseek-v4-flash-0731",
                              "llm_api_key": "sk-test"}, max_tokens=300,
                             max_retries=0, sleep=lambda _s: None)
    assert adapter.available is True
    with pytest.raises(LLMBudgetExhausted) as exc:
        adapter.complete("ping", scene="query_gen")
    assert "finish_reason=length" in str(exc.value)


def test_query_templates_exist_for_all_p0_languages(real_policy):
    for language in real_policy.target_languages:
        assert language in QUERY_TEMPLATES
        assert set(QUERY_TEMPLATES[language]) == {t["type_key"] for t in real_policy.query_types}
