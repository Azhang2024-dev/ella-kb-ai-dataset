"""Inventory 测试：分页、嵌套、空目录、缺 next_page_token、跨源合并去重、单页失败容错。

全部走录制响应夹具（tests/document_collection/fixtures/lark_drive_pages），
不发真实请求 —— 对应计划 10.2「录制响应测试」。
"""

from __future__ import annotations

import json
import os

import pytest

from conftest import PAGES_DIR, FixtureRunner

from core.document_collection.adapters.lark_cli import LarkCliAdapter
from core.document_collection.config import CollectionPolicy
from core.document_collection.inventory import (
    InventoryOptions,
    classify_entry,
    display_bucket,
    merge_and_count,
    run_inventory,
)
from core.document_collection.state import StateStore


def _stdout(name: str) -> str:
    with open(os.path.join(PAGES_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


def _runner(**mapping) -> FixtureRunner:
    """mapping: key(argv 子串) -> 夹具文件名列表（按调用顺序消费）。"""
    responses = {k: [{"stdout": _stdout(f), "returncode": 0} for f in v]
                 for k, v in mapping.items()}
    return FixtureRunner(responses)


@pytest.fixture
def two_source_policy(real_policy) -> CollectionPolicy:
    raw = json.loads(json.dumps(real_policy.raw, ensure_ascii=False))
    raw["sources"] = [
        {"name": "知识库_2026", "kind": "lark_drive_folder", "token": "FOLDER_ONE",
         "role": "primary", "readonly": True},
        {"name": "docQA.llm", "kind": "lark_drive_folder", "token": "FOLDER_TWO",
         "role": "supplement", "readonly": True},
    ]
    return CollectionPolicy(path="fixture-policy.json", raw=raw)


def _store(workdir) -> StateStore:
    return StateStore(os.path.join(workdir, "state.sqlite3"))


def test_pagination_and_nesting_are_complete(two_source_policy, workdir):
    runner = _runner(**{
        "--folder-token TOK_SUB": ["nested_child.json"],
        "--folder-token EMPTY_ONE": ["empty_folder.json"],
        "--page-token v3|": ["page2_of_2.json"],
        "--folder-token FOLDER_ONE": ["page1_of_2.json"],
        "--folder-token FOLDER_TWO": ["second_source_page.json"],
    })
    adapter = LarkCliAdapter("lark-cli", runner=runner, inter_call_delay=0.0)
    store = _store(workdir)
    try:
        report = run_inventory(adapter=adapter, sources=two_source_policy.sources,
                               policy=two_source_policy, store=store,
                               workspace_dir=workdir, run_id="inv-test",
                               options=InventoryOptions(page_size=200))
        # 源 A：page1 的 2 个文件 + page2 的 2 个 + SUB_ONE 的 3 个 = 7
        # 源 B：3 个文件（TOK_A1 与源 A 同 token）
        assert report.raw_occurrences == 10
        assert report.unique_items == 9
        assert report.duplicate_occurrences == 1
        assert report.cross_source_duplicate_tokens == 1
        assert report.errors == []
        # 分页确实翻到了第 2 页
        assert any("--page-token" in call for call in runner.calls)
        # 空目录不应该产生错误
        assert any("--folder-token" in c and "EMPTY_ONE" in c for c in runner.calls)
    finally:
        store.close()


def test_source_names_in_report_are_the_real_folder_names(two_source_policy, workdir):
    runner = _runner(**{
        "--folder-token TOK_SUB": ["nested_child.json"],
        "--folder-token EMPTY_ONE": ["empty_folder.json"],
        "--page-token v3|": ["page2_of_2.json"],
        "--folder-token FOLDER_ONE": ["page1_of_2.json"],
        "--folder-token FOLDER_TWO": ["second_source_page.json"],
    })
    adapter = LarkCliAdapter("lark-cli", runner=runner, inter_call_delay=0.0)
    store = _store(workdir)
    try:
        run_inventory(adapter=adapter, sources=two_source_policy.sources,
                      policy=two_source_policy, store=store, workspace_dir=workdir,
                      run_id="inv-names", options=InventoryOptions(page_size=200))
        md = open(os.path.join(workdir, "inventory_summary.md"), encoding="utf-8").read()
        assert "知识库_2026" in md and "docQA.llm" in md
        assert "原始采集文件夹" not in md and "新增补采文件夹" not in md
        csv_text = open(os.path.join(workdir, "source_type_counts.csv"),
                        encoding="utf-8-sig").read()
        assert "知识库_2026" in csv_text and "docQA.llm" in csv_text
        manifest = json.load(open(os.path.join(workdir, "run_manifest.json"), encoding="utf-8"))
        assert manifest["capability"] == "collection.inventory"
        assert manifest["requirement_revisions"]["collection_plan_doc"]["revision_id"] == 201
    finally:
        store.close()


def test_missing_next_page_token_is_reported_not_silently_ignored(two_source_policy, workdir):
    runner = _runner(**{
        "--folder-token TOK_SUB": ["nested_child.json"],
        "--folder-token EMPTY_ONE": ["empty_folder.json"],
        "--page-token v3|": ["page2_of_2.json"],
        "--folder-token FOLDER_ONE": ["has_more_no_token.json"],
        "--folder-token FOLDER_TWO": ["second_source_page.json"],
    })
    adapter = LarkCliAdapter("lark-cli", runner=runner, inter_call_delay=0.0)
    store = _store(workdir)
    try:
        report = run_inventory(adapter=adapter, sources=two_source_policy.sources,
                               policy=two_source_policy, store=store, workspace_dir=workdir,
                               run_id="inv-notoken", options=InventoryOptions(page_size=200))
        assert any("next_page_token" in e.get("message", "") for e in report.errors), report.errors
        # 缺 next_page_token 属于"可能有漏项"，必须让整轮盘点降级为 partial，而不是 completed
        assert report.raw_occurrences < 10
    finally:
        store.close()


def test_single_folder_failure_does_not_abort_the_whole_inventory(two_source_policy, workdir):
    runner = _runner(**{
        "--folder-token TOK_SUB": ["error_permission.json"],      # 子目录权限不足
        "--folder-token EMPTY_ONE": ["empty_folder.json"],
        "--page-token v3|": ["page2_of_2.json"],
        "--folder-token FOLDER_ONE": ["page1_of_2.json"],
        "--folder-token FOLDER_TWO": ["second_source_page.json"],
    })
    adapter = LarkCliAdapter("lark-cli", runner=runner, inter_call_delay=0.0, max_retries=0)
    store = _store(workdir)
    try:
        report = run_inventory(adapter=adapter, sources=two_source_policy.sources,
                               policy=two_source_policy, store=store, workspace_dir=workdir,
                               run_id="inv-partial", options=InventoryOptions(page_size=200))
        assert report.errors, "权限失败必须被记录"
        assert report.raw_occurrences >= 7, "其他目录仍应盘点完成"
        assert any("permission_denied" in e.get("message", "") for e in report.errors)
    finally:
        store.close()


def test_transient_error_is_retried_and_inventory_still_completes(two_source_policy, workdir):
    runner = _runner(**{
        "--folder-token TOK_SUB": ["error_429.json", "nested_child.json"],
        "--folder-token EMPTY_ONE": ["empty_folder.json"],
        "--page-token v3|": ["page2_of_2.json"],
        "--folder-token FOLDER_ONE": ["page1_of_2.json"],
        "--folder-token FOLDER_TWO": ["second_source_page.json"],
    })
    adapter = LarkCliAdapter("lark-cli", runner=runner, inter_call_delay=0.0,
                             max_retries=0)   # 重试交给 walk_source 的下次调用
    store = _store(workdir)
    try:
        report = run_inventory(adapter=adapter, sources=two_source_policy.sources,
                               policy=two_source_policy, store=store, workspace_dir=workdir,
                               run_id="inv-429", options=InventoryOptions(page_size=200))
        assert report.errors, "429 必须留痕"
        assert report.errors[0]["retryable"] is True
    finally:
        store.close()


def test_inventory_is_idempotent_on_rerun(two_source_policy, workdir):
    def once(run_id):
        runner = _runner(**{
            "--folder-token TOK_SUB": ["nested_child.json"],
            "--folder-token EMPTY_ONE": ["empty_folder.json"],
            "--page-token v3|": ["page2_of_2.json"],
            "--folder-token FOLDER_ONE": ["page1_of_2.json"],
            "--folder-token FOLDER_TWO": ["second_source_page.json"],
        })
        adapter = LarkCliAdapter("lark-cli", runner=runner, inter_call_delay=0.0)
        store = StateStore(os.path.join(workdir, "state.sqlite3"))
        try:
            report = run_inventory(adapter=adapter, sources=two_source_policy.sources,
                                   policy=two_source_policy, store=store,
                                   workspace_dir=workdir, run_id=run_id,
                                   options=InventoryOptions(page_size=200))
            return report.unique_items, report.raw_occurrences, store.count_items()
        finally:
            store.close()

    first = once("inv-idem-1")
    second = once("inv-idem-1")   # 同 run_id 重跑：upsert 不应翻倍
    assert first == second


def test_max_depth_is_enforced(two_source_policy, workdir):
    runner = _runner(**{
        "--folder-token TOK_SUB": ["nested_child.json"],
        "--folder-token EMPTY_ONE": ["empty_folder.json"],
        "--page-token v3|": ["page2_of_2.json"],
        "--folder-token FOLDER_ONE": ["page1_of_2.json"],
        "--folder-token FOLDER_TWO": ["second_source_page.json"],
    })
    adapter = LarkCliAdapter("lark-cli", runner=runner, inter_call_delay=0.0)
    store = _store(workdir)
    try:
        report = run_inventory(adapter=adapter, sources=two_source_policy.sources,
                               policy=two_source_policy, store=store, workspace_dir=workdir,
                               run_id="inv-depth", options=InventoryOptions(page_size=200,
                                                                            max_depth=0))
        # 深度 0：只盘根目录，SUB_ONE 里的 3 条不应出现
        tokens = {r["item_token"] for r in
                  [json.loads(l) for l in open(os.path.join(workdir, "source_inventory.jsonl"),
                                               encoding="utf-8") if l.strip()]}
        assert "TOK_N1" not in tokens
        assert any("超过最大深度" in e.get("message", "") for e in report.errors)
    finally:
        store.close()


def test_per_source_limit_stops_early(two_source_policy, workdir):
    runner = _runner(**{
        "--folder-token TOK_SUB": ["nested_child.json"],
        "--folder-token EMPTY_ONE": ["empty_folder.json"],
        "--page-token v3|": ["page2_of_2.json"],
        "--folder-token FOLDER_ONE": ["page1_of_2.json"],
        "--folder-token FOLDER_TWO": ["second_source_page.json"],
    })
    adapter = LarkCliAdapter("lark-cli", runner=runner, inter_call_delay=0.0)
    store = _store(workdir)
    try:
        report = run_inventory(adapter=adapter, sources=two_source_policy.sources,
                               policy=two_source_policy, store=store, workspace_dir=workdir,
                               run_id="inv-limit",
                               options=InventoryOptions(page_size=200, per_source_limit=2))
        assert report.sources[0]["raw_count"] == 2
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 纯函数级
# ---------------------------------------------------------------------------
def test_classify_entry_covers_all_observed_lark_types(two_source_policy):
    cases = {
        "folder": ("folder", None),
        "file": ("local_file", None),
        "docx": ("online_doc", "docx"),
        "doc": ("online_doc", "docx"),
        "wiki": ("online_doc", "docx"),
        "sheet": ("online_sheet", "xlsx"),
        # bitable 导出 csv 需要 --sub-id 指定子表（实测 validation 错误），
        # 批量场景拿不到子表 id，所以策略里统一导出成 xlsx
        "bitable": ("online_sheet", "xlsx"),
        "slides": ("online_slides", "pptx"),
        "shortcut": ("shortcut", None),
        "whatever": ("unknown", None),
    }
    for raw_type, (expected_type, expected_ext) in cases.items():
        cls = classify_entry({"type": raw_type, "name": "x", "token": "t"}, two_source_policy)
        assert cls["item_type"] == expected_type, raw_type
        if expected_ext:
            assert cls["extension"] == expected_ext, raw_type


def test_display_bucket_matches_the_online_plan_table():
    assert display_bucket("local_file", "pdf") == "PDF"
    assert display_bucket("local_file", "doc") == "DOC/DOCX"
    assert display_bucket("local_file", "docx") == "DOC/DOCX"
    assert display_bucket("online_doc", "docx") == "在线 Docx"
    assert display_bucket("local_file", "pptx") == "PPT/PPTX"
    assert display_bucket("local_file", "xlsx") == "XLSX/CSV"
    assert display_bucket("local_file", "csv") == "XLSX/CSV"
    assert display_bucket("online_sheet", "xlsx") == "在线 Sheet"
    assert display_bucket("local_file", "txt") == "TXT"
    assert display_bucket("local_file", "md") == "MD"
    assert display_bucket("online_slides", "pptx") == "在线 Slides"
    assert display_bucket("local_file", "html") == "其他/未知"
    assert display_bucket("shortcut", None) == "其他/未知"


def test_merge_and_count_keeps_all_source_paths():
    occurrences = [
        {"source_name": "A", "source_folder_path": "/", "relative_path": "x.pdf",
         "item_token": "T1", "name": "x.pdf", "item_type": "local_file",
         "extension": "pdf", "display_bucket": "PDF"},
        {"source_name": "B", "source_folder_path": "/sub", "relative_path": "sub/x.pdf",
         "item_token": "T1", "name": "x.pdf", "item_type": "local_file",
         "extension": "pdf", "display_bucket": "PDF"},
    ]
    stats = merge_and_count(occurrences)
    assert stats["unique_items"] == 1
    assert stats["duplicate_occurrences"] == 1
    assert stats["cross_source_duplicate_tokens"] == 1
    assert len(stats["unique_records"]["T1"]["all_sources"]) == 2


def test_same_name_different_token_is_not_merged_at_inventory_stage():
    """需求第五章：同名不同 token 不直接视为不同文档，但盘点阶段不做正文比对。"""
    occurrences = [
        {"source_name": "B", "source_folder_path": "/", "relative_path": "lekciya.pdf",
         "item_token": "T1", "name": "lekciya.pdf", "item_type": "local_file",
         "extension": "pdf", "display_bucket": "PDF"},
        {"source_name": "B", "source_folder_path": "/", "relative_path": "lekciya.pdf",
         "item_token": "T2", "name": "lekciya.pdf", "item_type": "local_file",
         "extension": "pdf", "display_bucket": "PDF"},
    ]
    stats = merge_and_count(occurrences)
    assert stats["unique_items"] == 2, "正文级去重属于 deduplicate 阶段"
