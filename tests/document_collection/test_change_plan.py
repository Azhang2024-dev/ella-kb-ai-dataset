"""改动计划（2026-09-13 P0 六语种批）的实施验证。

覆盖 P1 输入窗口 / P2 内容审核分类 / P3 键名归一化 / P4 状态库同写 /
P5 acquire 过滤 / P6 语种阈值 / P7 元数据兜底 / P8 出题能力。
"""

from __future__ import annotations

import json
import os

from conftest import FakeLLM, build_valid_queries

from core.document_collection.acquisition import (
    AcquireOptions,
    language_folder_names,
    select_items,
)
from core.document_collection.llm import (
    LLMBudgetExhausted,
    build_query_prompt,
    normalize_question,
)
from core.document_collection.metadata import (
    infer_education_level,
    infer_subject,
    metadata_hints,
)
from core.document_collection.query_generation import (
    QueryOptions,
    _classify_llm_failure,
    build_document_ref,
    generate_for_item,
)
from core.document_collection.question_generation import (
    QuestionOptions,
    run_question_generation,
)
from core.document_collection.screening import (
    ScreeningOptions,
    hard_screen,
    semantic_review,
)
from core.document_collection.state import StateStore


def _quiet(_m: str) -> None:
    return None


# ---------------------------------------------------------------------------
# P1 输入窗口 16000 -> 50000（单一来源）
# ---------------------------------------------------------------------------
def test_prompt_text_chars_comes_from_policy(real_policy):
    assert real_policy.prompt_text_chars == 50000
    # 与端侧/云侧入库窗口同口径
    assert real_policy.prompt_text_chars == real_policy.full_text_review_threshold_chars


def test_build_document_ref_truncates_once_and_records_evidence():
    text = "字" * 120000
    ref = build_document_ref({"item_id": "T", "name": "big.pdf"}, {"language": "zh"},
                             text, 50000)
    # 头 50% + 中 25% + 尾 25% + 两段标记，长度略大于窗口
    assert ref["text_chars"] == 120000
    assert ref["truncated"] is True
    assert ref["prompt_chars"] < 60000
    assert "TRUNCATED_MIDDLE" in ref["text"]


def test_short_document_is_not_truncated():
    ref = build_document_ref({"item_id": "T"}, {"language": "fr"}, "court texte", 50000)
    assert ref["truncated"] is False
    assert ref["prompt_chars"] == len("court texte")


def test_query_prompt_does_not_truncate_again(real_policy):
    """提示词模板不得再切一刀：入模正文必须等于 document_ref 里的 text。"""
    body = "x" * 50000
    ref = {"language": "fr", "text": body}
    prompt = build_query_prompt(ref, {**real_policy.query_plan,
                                      "max_tokens": 32000})
    assert body in prompt, "提示词模板又截断了一次（双截断回归）"


def test_generate_for_item_uses_policy_window(real_policy, workdir):
    """窗口来自 policy，不再是类里写死的 16000。"""
    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        body = "Ceci est un cours de comptabilite generale. " * 3000  # ~120k 字符
        store.upsert_item({
            "item_id": "BIG", "run_id": "r", "source_name": "知识库_2026",
            "source_folder_path": "DOC-FR", "item_token": "BIG", "name": "cours.pdf",
            "item_type": "local_file", "extension": "pdf",
            "relative_path": "DOC-FR/cours.pdf", "status": "accepted", "stage": "screen",
            "metadata": {"relative_path": "DOC-FR/cours.pdf"},
        })
        store.upsert_extraction("BIG", extractor="pdf", status="extracted",
                                char_count=len(body), language="fr", page_count=100)
        os.makedirs(os.path.join(workdir, "extracted"), exist_ok=True)
        with open(os.path.join(workdir, "extracted", "BIG.txt"), "w", encoding="utf-8") as fh:
            fh.write(body)
        store.commit()

        captured = {}

        class CaptureLLM:
            name = "capture"
            available = True

            def generate_queries(self, document_ref, policy):
                captured["prompt_chars"] = document_ref.get("prompt_chars")
                captured["truncated"] = document_ref.get("truncated")
                return {"queries": build_valid_queries(policy, "fr")}

        gen = generate_for_item(item=store.get_item("BIG"), extraction=store.get_extraction("BIG"),
                                policy=real_policy, llm=CaptureLLM(), workspace_dir=workdir,
                                opts=QueryOptions())
        # 4 万字符以下不该被截；这里 >5 万，应被截到 ~5 万而非 1.6 万
        assert captured["truncated"] is True
        assert captured["prompt_chars"] > 45000, captured
        assert gen["status"] == "validated"
        assert gen["truncated"] is True
    finally:
        store.close()


# ---------------------------------------------------------------------------
# P2 内容审核拦截单列
# ---------------------------------------------------------------------------
def test_content_audit_rejection_is_classified_separately():
    class BadRequest(Exception):
        pass

    exc = BadRequest("litellm.BadRequestError: Input text data may contain inappropriate content")
    assert _classify_llm_failure(exc) == "content_audit_rejected"
    assert _classify_llm_failure(LLMBudgetExhausted("finish_reason=length")) == \
        "llm_budget_exhausted"
    assert _classify_llm_failure(RuntimeError("模型返回空内容")) == "llm_output_invalid"


def test_generate_for_item_marks_content_audit(real_policy, workdir):
    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        from conftest import FR_TEXT
        store.upsert_item({
            "item_id": "AUDIT", "run_id": "r", "source_name": "知识库_2026",
            "source_folder_path": "DOC-FR", "item_token": "AUDIT", "name": "cours.pdf",
            "item_type": "local_file", "extension": "pdf", "relative_path": "DOC-FR/cours.pdf",
            "status": "accepted", "stage": "screen",
            "metadata": {"relative_path": "DOC-FR/cours.pdf"},
        })
        store.upsert_extraction("AUDIT", extractor="pdf", status="extracted",
                                char_count=len(FR_TEXT), language="fr", page_count=5)
        os.makedirs(os.path.join(workdir, "extracted"), exist_ok=True)
        with open(os.path.join(workdir, "extracted", "AUDIT.txt"), "w", encoding="utf-8") as fh:
            fh.write(FR_TEXT)
        store.commit()

        class AuditLLM:
            name = "audit"
            available = True

            def generate_queries(self, *_a, **_k):
                raise RuntimeError("BadRequestError: Input text data may contain inappropriate content")

        record = generate_for_item(item=store.get_item("AUDIT"),
                                   extraction=store.get_extraction("AUDIT"),
                                   policy=real_policy, llm=AuditLLM(),
                                   workspace_dir=workdir, opts=QueryOptions())
        assert record["status"] == "needs_review"
        assert record["reason_codes"] == ["content_audit_rejected"]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# P3 键名归一化
# ---------------------------------------------------------------------------
def test_normalize_question_folds_explanation_aliases():
    q = normalize_question({"stem": "s", "explication": "因为…", "options": {"A": "1"}})
    assert q["explanation"] == "因为…"
    assert "explication" not in q
    assert q["answer"] == ""
    ar = normalize_question({"السؤال": "س", "الإجابة": "ج", "شرح": "ش"})
    assert ar["stem"] == "س" and ar["answer"] == "ج" and ar["explanation"] == "ش"


def test_question_generation_normalizes_and_writes_state(real_policy, workdir, fake_llm):
    """P3+P4+P8：出题产物落盘、键名归一化、状态库 questions 表同写。"""
    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        from conftest import FR_TEXT
        store.upsert_item({
            "item_id": "Q1", "run_id": "r", "source_name": "知识库_2026",
            "source_folder_path": "DOC-FR", "item_token": "Q1", "name": "cours.pdf",
            "item_type": "local_file", "extension": "pdf", "relative_path": "DOC-FR/cours.pdf",
            "status": "accepted", "stage": "screen",
            "metadata": {"relative_path": "DOC-FR/cours.pdf"},
        })
        store.upsert_extraction("Q1", extractor="pdf", status="extracted",
                                char_count=len(FR_TEXT), language="fr", page_count=5)
        os.makedirs(os.path.join(workdir, "extracted"), exist_ok=True)
        with open(os.path.join(workdir, "extracted", "Q1.txt"), "w", encoding="utf-8") as fh:
            fh.write(FR_TEXT)
        store.commit()
        # query 阶段的产物（questions 阶段消费它）
        record = {"item_id": "Q1", "name": "cours.pdf", "document_language": "fr",
                  "status": "validated",
                  "queries": build_valid_queries(real_policy.query_plan, "fr")}
        with open(os.path.join(workdir, "query_records.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        summary = run_question_generation(policy=real_policy, store=store, workspace_dir=workdir,
                                         run_id="r", llm=fake_llm, options=QuestionOptions(), log=_quiet)
        assert summary["status_counts"].get("ok") == 1
        # 产物
        assert os.path.isfile(os.path.join(workdir, "questions", "Q1.json"))
        assert os.path.isfile(os.path.join(workdir, "question_records.jsonl"))
        # 状态库同写（P4）
        rows = store.all_questions()
        assert len(rows) == 10
        assert all("explication" not in json.dumps(r["payload"]) for r in rows)
        # 键名归一化（P3）
        for row in rows:
            for q in row["payload"]["questions"]:
                assert "explanation" in q and q["explanation"]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# P4 reconcile（jsonl -> 状态库）
# ---------------------------------------------------------------------------
def test_reconcile_queries_from_jsonl(real_policy, workdir):
    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        records = [{
            "item_id": "R1", "name": "x.pdf", "document_language": "fr",
            "status": "validated", "generated_at": "2026-09-13T00:00:00",
            "queries": build_valid_queries(real_policy.query_plan, "fr"),
            "validation": {"ok": True, "errors": [], "warnings": []},
        }]
        path = os.path.join(workdir, "query_records.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        assert store.all_queries() == []
        stats = store.reconcile_queries_from_jsonl(path)
        assert stats["records"] == 1 and stats["queries"] == 10
        assert len(store.all_queries()) == 10
        # 幂等：再跑一次不翻倍
        store.reconcile_queries_from_jsonl(path)
        assert len(store.all_queries()) == 10
    finally:
        store.close()


# ---------------------------------------------------------------------------
# P5 acquire 语种 / 目录过滤
# ---------------------------------------------------------------------------
def test_language_folder_names_from_policy(real_policy):
    assert "doc-en" in language_folder_names(real_policy, "en")
    assert "english" in language_folder_names(real_policy, "en")
    assert "doc-es" in language_folder_names(real_policy, "es")


def test_select_items_filters_by_language_and_top_folder(real_policy, workdir):
    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        for item_id, rel in (("E1", "DOC-EN/a.pdf"), ("Z1", "DOC-ZH/b.pdf"),
                             ("F1", "french+c/d.pdf")):
            store.upsert_item({
                "item_id": item_id, "run_id": "r", "source_name": "知识库_2026",
                "source_folder_path": rel.split("/")[0], "item_token": item_id,
                "name": rel.split("/")[-1], "item_type": "file", "extension": "pdf",
                "relative_path": rel, "status": "discovered", "metadata": {"relative_path": rel},
            })
        store.commit()
        en = select_items(store, AcquireOptions(languages=("en",)), real_policy)
        assert [i["item_id"] for i in en] == ["E1"]
        two = select_items(store, AcquireOptions(languages=("en", "zh")), real_policy)
        assert {i["item_id"] for i in two} == {"E1", "Z1"}
        top = select_items(store, AcquireOptions(top_folders=("DOC-ZH",)), real_policy)
        assert [i["item_id"] for i in top] == ["Z1"]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# P6 语种阈值按语种
# ---------------------------------------------------------------------------
def test_non_latin_languages_get_lower_threshold(real_policy):
    assert real_policy.min_language_confidence_for("zh") == 0.55
    assert real_policy.min_language_confidence_for("ru") == 0.55
    assert real_policy.min_language_confidence_for("ar") == 0.55
    assert real_policy.min_language_confidence_for("en") == 0.70
    assert real_policy.min_language_confidence_for("xx") == 0.70


def _artifact_extraction(language, confidence):
    return {"status": "extracted", "char_count": 30000, "language": language,
            "language_confidence": confidence, "page_count": 30}


def _screen_item():
    return {"item_id": "S", "extension": "pdf", "status": "extracted",
            "name": "cours.pdf", "metadata": {"relative_path": "DOC-ZH/cours.pdf"}}


def test_chinese_with_confidence_between_thresholds_is_accepted(real_policy):
    art = {"status": "acquired", "path": "p", "size_bytes": 1000}
    # 0.62：低于全局 0.70，但高于 zh 的 0.55 -> 应放行
    decision = hard_screen(item=_screen_item(), artifact=art,
                           extraction=_artifact_extraction("zh", 0.62),
                           dedup_decision=None, policy=real_policy, opts=ScreeningOptions())
    assert decision["status"] == "accepted"
    assert decision["evidence"]["language_confidence_threshold"] == 0.55
    assert decision["evidence"]["language_threshold_source"] == "per_language"


def test_english_still_uses_global_threshold(real_policy):
    art = {"status": "acquired", "path": "p", "size_bytes": 1000}
    item = {**_screen_item(), "metadata": {"relative_path": "DOC-EN/cours.pdf"}}
    decision = hard_screen(item=item, artifact=art,
                           extraction=_artifact_extraction("en", 0.62),
                           dedup_decision=None, policy=real_policy, opts=ScreeningOptions())
    assert decision["status"] == "needs_review"
    assert decision["evidence"]["language_confidence_threshold"] == 0.70
    assert decision["evidence"]["language_threshold_source"] == "policy_default"


# ---------------------------------------------------------------------------
# P7 元数据兜底（含阿拉伯语）
# ---------------------------------------------------------------------------
def test_arabic_subject_keywords_are_covered():
    subject, hits = infer_subject("القانون المدني", "هذا الكتاب في القانون المدني والعقود")
    assert subject == "law", hits
    med, _ = infer_subject("الطب", "مقدمة في الطب السريري والتشخيص")
    assert med == "clinical_medicine_nursing"
    biz, _ = infer_subject("الأعمال", "إدارة الأعمال والتسويق والمحاسبة")
    assert biz == "economics_management_accounting_finance"


def test_education_level_hint_detects_university_material():
    level, hits = infer_education_level("课程大纲.pdf", "本课程为大学本科教材，包含讲义与习题")
    assert level == "university" and hits


def test_metadata_hints_shape():
    hints = metadata_hints("microeconomics.pdf", "Chapter on demand and supply")
    assert hints["subject_hint"] == "economics_management_accounting_finance"
    assert hints["subject_hint_keywords"]


def test_subject_hint_fills_unknown_but_never_changes_gate(real_policy):
    """关键词兜底只补学科展示，教育属性为 unknown 时仍转复核（不得自动放行）。"""
    base = {
        "status": "accepted", "reason_codes": ["ok"],
        "evidence": {"subject_hint": "law", "subject_hint_keywords": ["law"]},
        "hard_failed": False,
    }
    llm = FakeLLM(classify={
        "education_level": "unknown", "subject": "unknown", "queryability": "high",
        "exclude_kind": None, "evidence": ["x"], "confidence": 0.9,
        "needs_human_review": False,
    })
    out = semantic_review(decision=base, llm=llm, document_ref={},
                          policy=real_policy, opts=ScreeningOptions())
    assert out["status"] == "needs_review"           # 门槛未被兜底绕过
    assert out["evidence"]["subject"] == "law"        # 学科被兜底补上
    assert out["evidence"]["subject_source"] == "keyword_hint"


# ---------------------------------------------------------------------------
# P4 定向重跑不得覆盖汇总 jsonl（新环境实测缺陷 N1）
# ---------------------------------------------------------------------------
def test_targeted_question_rerun_merges_records_jsonl(real_policy, workdir, fake_llm):
    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        from conftest import FR_TEXT
        ids = ["M1", "M2", "M3"]
        for item_id in ids:
            store.upsert_item({
                "item_id": item_id, "run_id": "r", "source_name": "知识库_2026",
                "source_folder_path": "DOC-FR", "item_token": item_id, "name": f"{item_id}.pdf",
                "item_type": "local_file", "extension": "pdf",
                "relative_path": f"DOC-FR/{item_id}.pdf", "status": "accepted", "stage": "screen",
                "metadata": {"relative_path": f"DOC-FR/{item_id}.pdf"},
            })
            store.upsert_extraction(item_id, extractor="pdf", status="extracted",
                                    char_count=len(FR_TEXT), language="fr", page_count=5)
        os.makedirs(os.path.join(workdir, "extracted"), exist_ok=True)
        for item_id in ids:
            with open(os.path.join(workdir, "extracted", f"{item_id}.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write(FR_TEXT)
        store.commit()
        with open(os.path.join(workdir, "query_records.jsonl"), "w", encoding="utf-8") as fh:
            for item_id in ids:
                fh.write(json.dumps({
                    "item_id": item_id, "name": f"{item_id}.pdf", "document_language": "fr",
                    "status": "validated",
                    "queries": build_valid_queries(real_policy.query_plan, "fr"),
                }, ensure_ascii=False) + "\n")

        run_question_generation(policy=real_policy, store=store, workspace_dir=workdir,
                                run_id="r", llm=fake_llm, options=QuestionOptions(), log=_quiet)
        # 定向只重跑 1 篇：汇总 jsonl 必须合并，不能只剩这 1 篇
        run_question_generation(policy=real_policy, store=store, workspace_dir=workdir,
                                run_id="r", llm=fake_llm,
                                options=QuestionOptions(item_ids=("M2",)), log=_quiet)
        rows = [json.loads(l) for l in open(os.path.join(workdir, "question_records.jsonl"),
                                            encoding="utf-8") if l.strip()]
        assert {r["item_id"] for r in rows} == set(ids), "定向重跑把汇总 jsonl 覆盖成了子集"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# P8 出题能力（service 层）
# ---------------------------------------------------------------------------
def test_generate_questions_capability_end_to_end(policy, fake_adapter, fake_llm, workdir):
    from core.document_collection.service import CoreService
    svc = CoreService(policy, workspace_root=os.path.join(workdir, "ws"),
                      run_id="q-e2e", adapter=fake_adapter, llm=fake_llm, log=_quiet)
    try:
        svc.inventory()
        svc.acquire()
        svc.extract()
        svc.deduplicate()
        svc.screen(semantic=True)
        q = svc.generate_queries(limit=2)
        assert q.summary["status_counts"].get("validated", 0) >= 1
        result = svc.generate_questions(limit=2)
        assert result.status in ("completed", "needs_review")
        assert result.summary["total_questions"] > 0
        assert svc.store.all_questions(), "出题必须写进状态库 questions 表"
    finally:
        svc.close()
