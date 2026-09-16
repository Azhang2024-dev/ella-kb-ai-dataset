"""去重测试：SHA-256 精确重复、规范化正文重复、近重复只标记、来源路径全保留。"""

from __future__ import annotations

import csv
import json
import os

from core.document_collection.deduplication import (
    DedupOptions,
    find_near_duplicates,
    group_rows,
    run_deduplicate,
)
from core.document_collection.state import StateStore
from core.document_collection.textstats import text_fingerprint


# 与真实策略一致：知识库_2026 是 primary，docQA.llm 是 supplement
ROLE_BY_SOURCE = {"知识库_2026": "primary", "docQA.llm": "supplement"}


def seed(store: StateStore, rows):
    """rows: (item_id, source, rel_path, sha, char_count, text, lang, pages)"""
    for item_id, source, rel, sha, chars, text, lang, pages in rows:
        store.upsert_item({
            "item_id": item_id, "run_id": "r", "source_name": source,
            "source_folder_path": os.path.dirname(rel) or "/", "item_token": item_id,
            "name": os.path.basename(rel), "item_type": "local_file",
            "extension": os.path.splitext(rel)[1].lstrip("."), "url": f"https://x/{item_id}",
            "relative_path": rel, "status": "extracted", "stage": "extract",
            "metadata": {"relative_path": rel, "source_role": ROLE_BY_SOURCE.get(source, ""),
                         "all_sources": [{"source_name": source, "relative_path": rel}]},
        })
        store.upsert_artifact(item_id, sha256=sha, path=f"/artifacts/{sha}",
                              size_bytes=chars * 2, status="acquired")
        store.upsert_extraction(item_id, extractor="pdf", status="extracted",
                                char_count=chars, language=lang, page_count=pages)
        store.add_decision(item_id, "extract", "extracted", ["ok"],
                           {"normalized_fingerprint": text_fingerprint(text)})
    store.commit()


TEXT_A = "Chapitre 1 Introduction a la comptabilite generale. " * 20
TEXT_B = "Глава 1 Введение в линейную алгебру. " * 20


def test_exact_sha_duplicates_keep_one_representative(real_policy, workdir):
    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        seed(store, [
            ("T1", "知识库_2026", "DOC-FR/cours.pdf", "a" * 64, 5000, TEXT_A, "fr", 10),
            ("T2", "docQA.llm", "French/cours_copy.pdf", "a" * 64, 5000, TEXT_A, "fr", 10),
            ("T3", "知识库_2026", "DOC-RU/lekciya.pdf", "b" * 64, 4000, TEXT_B, "ru", 8),
        ])
        summary = run_deduplicate(policy=real_policy, store=store, workspace_dir=workdir,
                                  run_id="r", options=DedupOptions(), log=lambda _m: None)
        assert summary["sha256_groups"] == 1
        assert summary["duplicate_items"] == 1
        assert summary["unique_items"] == 2
        decisions = store.latest_decisions("deduplicate")
        # T1 来自 primary 源（知识库_2026），按代表版本规则应当保留 T1、淘汰 T2
        assert decisions["T1"]["status"] == "unique"
        assert decisions["T2"]["status"] == "rejected"
        assert "exact_duplicate" in decisions["T2"]["reason_codes"]
        assert decisions["T2"]["evidence"]["representative"] == "T1"
        assert decisions["T3"]["status"] == "unique"
    finally:
        store.close()


def test_representative_choice_is_deterministic_and_prefers_richer_text(real_policy, workdir):
    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        # 同 sha，但 T_short 字符数更少 -> 必须保留 T_long
        seed(store, [
            ("T_short", "知识库_2026", "a/x.pdf", "c" * 64, 100, TEXT_A, "fr", 1),
            ("T_long", "docQA.llm", "b/x.pdf", "c" * 64, 9000, TEXT_A, "fr", 20),
        ])
        run_deduplicate(policy=real_policy, store=store, workspace_dir=workdir, run_id="r",
                        options=DedupOptions(), log=lambda _m: None)
        decisions = store.latest_decisions("deduplicate")
        assert decisions["T_short"]["status"] == "rejected"
        assert decisions["T_short"]["evidence"]["representative"] == "T_long"
        # 排序规则必须在报告里可查
        from core.document_collection.deduplication import REPRESENTATIVE_RULE
        assert "正文更长" in REPRESENTATIVE_RULE
    finally:
        store.close()


def test_normalized_text_duplicate_across_containers(real_policy, workdir):
    """同一讲义的 pdf 与 docx 导出：sha 不同但正文规范化后一致。"""
    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        seed(store, [
            ("T_pdf", "知识库_2026", "DOC-FR/cours.pdf", "d" * 64, 5000, TEXT_A, "fr", 10),
            ("T_docx", "docQA.llm", "French/cours.docx", "e" * 64, 4980, TEXT_A, "fr", None),
        ])
        summary = run_deduplicate(policy=real_policy, store=store, workspace_dir=workdir,
                                  run_id="r", options=DedupOptions(), log=lambda _m: None)
        assert summary["normalized_groups"] == 1
        decisions = store.latest_decisions("deduplicate")
        rejected = [k for k, v in decisions.items() if v["status"] == "rejected"]
        assert len(rejected) == 1
        assert "normalized_duplicate" in decisions[rejected[0]]["reason_codes"]
    finally:
        store.close()


def test_near_duplicates_are_marked_only(real_policy, workdir):
    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        seed(store, [
            ("N1", "知识库_2026", "DOC-FR/v1.pdf", "1" * 64, 10000, TEXT_A + "alpha", "fr", 20),
            ("N2", "知识库_2026", "DOC-FR/v2.pdf", "2" * 64, 10050, TEXT_A + "beta", "fr", 20),
        ])
        summary = run_deduplicate(policy=real_policy, store=store, workspace_dir=workdir,
                                  run_id="r", options=DedupOptions(), log=lambda _m: None)
        assert summary["near_duplicate_pairs"] == 1
        assert summary["duplicate_items"] == 0, "近重复不能被自动淘汰"
        decisions = store.latest_decisions("deduplicate")
        assert decisions["N1"]["status"] != "rejected", "近重复不能被淘汰"
        all_decisions = store.decisions_for("N1")
        assert any(d["status"] == "unique" for d in all_decisions)
        assert any("near_duplicate_suspect" in d["reason_codes"] for d in all_decisions)
        suspects = os.path.join(workdir, "near_duplicate_suspects.jsonl")
        assert os.path.isfile(suspects)
        rows = [json.loads(l) for l in open(suspects, encoding="utf-8") if l.strip()]
        assert rows and rows[0]["reason"] == "near_duplicate_suspect"
    finally:
        store.close()


def test_tolerance_controls_near_duplicate_sensitivity(real_policy):
    rows = [
        {"item_id": "A", "sha256": "1", "normalized_fingerprint": "x", "language": "fr",
         "page_count": 10, "char_count": 10000},
        {"item_id": "B", "sha256": "2", "normalized_fingerprint": "y", "language": "fr",
         "page_count": 10, "char_count": 10500},
    ]
    assert len(find_near_duplicates(rows, tolerance=0.02)) == 0
    assert len(find_near_duplicates(rows, tolerance=0.10)) == 1


def test_items_without_artifacts_do_not_participate(real_policy, workdir):
    """之前就被 reject 的条目不该在去重阶段拿到 unique 决定，否则报告全是噪声。"""
    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        store.upsert_item({"item_id": "R1", "run_id": "r", "source_name": "知识库_2026",
                           "source_folder_path": "/", "item_token": "R1", "name": "x.jpg",
                           "item_type": "local_file", "extension": "jpg",
                           "relative_path": "x.jpg", "status": "rejected", "stage": "acquire",
                           "metadata": {"relative_path": "x.jpg"}})
        store.add_decision("R1", "acquire", "rejected", ["extension_unsupported"], {})
        store.commit()
        summary = run_deduplicate(policy=real_policy, store=store, workspace_dir=workdir,
                                  run_id="r", options=DedupOptions(), log=lambda _m: None)
        assert summary["total_rows"] == 0
        assert store.latest_decisions("deduplicate") == {}
    finally:
        store.close()


def test_all_source_paths_are_preserved(real_policy, workdir):
    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        seed(store, [
            ("T1", "知识库_2026", "DOC-FR/cours.pdf", "a" * 64, 5000, TEXT_A, "fr", 10),
            ("T2", "docQA.llm", "French/cours_copy.pdf", "a" * 64, 5000, TEXT_A, "fr", 10),
        ])
        run_deduplicate(policy=real_policy, store=store, workspace_dir=workdir, run_id="r",
                        options=DedupOptions(), log=lambda _m: None)
        path = os.path.join(workdir, "dedup_groups.csv")
        assert os.path.isfile(path)
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert rows, "重复分组必须写进 CSV"
        listing = rows[0]["成员清单（源文件夹/路径）"]
        assert "知识库_2026/DOC-FR/cours.pdf" in listing
        assert "docQA.llm/French/cours_copy.pdf" in listing
        assert rows[0]["推荐原因"]
    finally:
        store.close()


def test_keep_duplicates_flag_only_marks(real_policy, workdir):
    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        seed(store, [
            ("T1", "知识库_2026", "DOC-FR/cours.pdf", "a" * 64, 5000, TEXT_A, "fr", 10),
            ("T2", "docQA.llm", "French/cours_copy.pdf", "a" * 64, 5000, TEXT_A, "fr", 10),
        ])
        summary = run_deduplicate(policy=real_policy, store=store, workspace_dir=workdir,
                                  run_id="r",
                                  options=DedupOptions(reject_exact_duplicates=False),
                                  log=lambda _m: None)
        assert summary["duplicate_items"] == 1
        items = {i["item_id"]: i for i in store.list_items()}
        assert items["T2"]["status"] != "rejected", "只标记模式下不应改写条目状态"
        groups = store.dedup_groups("sha256")
        assert groups, "分组关系仍要落库"
    finally:
        store.close()


def test_group_rows_separates_sha_and_normalized(real_policy):
    rows = [
        {"item_id": "A", "sha256": "s1", "normalized_fingerprint": "f1", "char_count": 10},
        {"item_id": "B", "sha256": "s1", "normalized_fingerprint": "f1", "char_count": 10},
        {"item_id": "C", "sha256": "s2", "normalized_fingerprint": "f1", "char_count": 10},
        {"item_id": "D", "sha256": "s3", "normalized_fingerprint": "f3", "char_count": 10},
    ]
    groups = group_rows(rows)
    assert set(groups["sha"]) == {"s1"}
    assert set(groups["normalized"]) == {"f1"}
    assert len(groups["normalized"]["f1"]) == 3
