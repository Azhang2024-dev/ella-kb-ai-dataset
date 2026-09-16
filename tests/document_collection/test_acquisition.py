"""采集与抽样测试：内容寻址、原子落盘、续跑、在线导出、分层抽样确定性。"""

from __future__ import annotations

import json
import os

import pytest

from conftest import FakeCloudAdapter, build_fake_tree

from core.document_collection.acquisition import (
    DEFAULT_SAMPLE_STRATA,
    AcquireOptions,
    artifact_path_for,
    select_items,
    stratified_sample,
    stratum_key,
)
from core.document_collection.contracts import (
    ITEM_ACQUIRED,
    ITEM_ACQUIRE_FAILED,
    ITEM_CONVERSION_REQUIRED,
    ITEM_NEEDS_REVIEW,
    ITEM_PENDING_EXPORT,
    ITEM_REJECTED,
)
from core.document_collection.service import CoreService
from core.document_collection.state import StateStore
from core.document_collection.textstats import sha256_file


def _quiet(_msg: str) -> None:
    return None


# ---------------------------------------------------------------------------
# 分层抽样
# ---------------------------------------------------------------------------
def make_items(spec):
    """spec: [(item_id, source, rel_path, extension)]"""
    return [{"item_id": i, "item_token": i, "source_name": s, "relative_path": r,
             "extension": e, "item_type": "local_file", "metadata": {"relative_path": r}}
            for i, s, r, e in spec]


def test_stratum_key_extracts_the_requested_dimensions():
    item = make_items([("T", "知识库_2026", "DOC-FR/a.pdf", "pdf")])[0]
    assert stratum_key(item, ("source_name", "top_folder", "extension")) == \
        ("知识库_2026", "DOC-FR", "pdf")
    assert stratum_key(item, ("item_type",)) == ("local_file",)
    root_item = make_items([("T2", "docQA.llm", "b.txt", "txt")])[0]
    assert stratum_key(root_item, ("top_folder",)) == ("<root>",)


def test_stratified_sample_is_deterministic():
    spec = [(f"T{i:03d}", "A" if i % 2 else "B", f"f{i % 3}/x{i}.pdf", "pdf")
            for i in range(60)]
    items = make_items(spec)
    first = stratified_sample(items, 20, seed=42)
    second = stratified_sample(items, 20, seed=42)
    assert [i["item_id"] for i in first] == [i["item_id"] for i in second]
    other = stratified_sample(items, 20, seed=43)
    assert [i["item_id"] for i in other] != [i["item_id"] for i in first], \
        "换种子应该换样本，否则种子参数是假的"


def test_stratified_sample_covers_every_stratum():
    spec = []
    for source in ("知识库_2026", "docQA.llm"):
        for folder in ("DOC-FR", "DOC-RU", "DOC-EN"):
            for ext in ("pdf", "txt", "docx"):
                for n in range(10):
                    spec.append((f"{source[:1]}{folder[-2:]}{ext}{n}", source,
                                 f"{folder}/x{n}.{ext}", ext))
    items = make_items(spec)
    picked = stratified_sample(items, 18, DEFAULT_SAMPLE_STRATA, seed=1)
    keys = {stratum_key(i, DEFAULT_SAMPLE_STRATA) for i in picked}
    assert len(keys) == 18, "18 个层各取一条，不应该有重复层"


def test_stratified_sample_does_not_starve_small_groups():
    """真实目录大小极不均匀，按比例分配会让小语种一份都抽不到。"""
    spec = [(f"BIG{i}", "A", "big/x.pdf", "pdf") for i in range(500)]
    spec += [("SMALL_RU", "A", "ru/x.pdf", "pdf")]
    spec += [("SMALL_AR", "A", "ar/x.pdf", "pdf")]
    picked = stratified_sample(make_items(spec), 10, seed=7)
    ids = {p["item_id"] for p in picked}
    assert "SMALL_RU" in ids and "SMALL_AR" in ids


def test_stratified_sample_caps_at_available_items():
    items = make_items([("T1", "A", "x.pdf", "pdf"), ("T2", "A", "y.pdf", "pdf")])
    assert len(stratified_sample(items, 100, seed=1)) == 2
    assert stratified_sample(items, 0, seed=1) == []


def test_select_items_applies_sample_and_limit_together(real_policy, workdir):
    store = StateStore(os.path.join(workdir, "s.sqlite3"))
    try:
        spec = [(f"T{i:03d}", "A" if i % 2 else "B", f"f{i % 4}/x{i}.pdf", "pdf")
                for i in range(40)]
        for item in make_items(spec):
            store.upsert_item({**item, "run_id": "r", "name": item["relative_path"],
                               "source_folder_path": "/", "status": "discovered",
                               "stage": "inventory"})
        store.commit()
        sampled = select_items(store, AcquireOptions(sample=10, sample_seed=3))
        assert len(sampled) == 10
        limited = select_items(store, AcquireOptions(sample=10, sample_seed=3, limit=4))
        assert len(limited) == 4
        assert [i["item_id"] for i in limited] == [i["item_id"] for i in sampled][:4]
        filtered = select_items(store, AcquireOptions(sample=10, extensions=("txt",)))
        assert filtered == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 采集行为
# ---------------------------------------------------------------------------
@pytest.fixture
def svc(policy, fake_adapter, fake_llm, workdir):
    service = CoreService(policy, workspace_root=os.path.join(workdir, "ws"),
                          run_id="acq", adapter=fake_adapter, llm=fake_llm, log=_quiet)
    service.inventory()
    yield service
    service.close()


def test_artifacts_are_content_addressed(svc):
    svc.acquire()
    artifacts = [a for a in svc.store.list_artifacts() if a["path"]]
    assert artifacts
    for artifact in artifacts:
        expected = artifact_path_for(svc.workspace_dir, artifact["sha256"],
                                     os.path.splitext(artifact["path"])[1].lstrip("."))
        assert os.path.abspath(artifact["path"]) == os.path.abspath(expected)
        assert os.path.isfile(artifact["path"])
        assert sha256_file(artifact["path"]) == artifact["sha256"], "记录的哈希必须与文件一致"


def test_no_partial_files_are_left_behind(svc):
    svc.acquire()
    leftovers = []
    for root, _dirs, files in os.walk(svc.workspace_dir):
        for name in files:
            if name.endswith(".part"):
                leftovers.append(os.path.join(root, name))
    assert leftovers == [], f"留下了半成品文件：{leftovers}"


def test_same_content_in_two_places_is_stored_once(svc):
    """夹具里 A_FR_DOCX 与 A_ONLINE_DOC 等内容相同，必须落到同一个物理文件。"""
    svc.acquire()
    artifacts = [a for a in svc.store.list_artifacts() if a["path"] and a["sha256"]]
    by_sha: dict[str, set] = {}
    for artifact in artifacts:
        by_sha.setdefault(artifact["sha256"], set()).add(os.path.abspath(artifact["path"]))
    conflict = {sha: paths for sha, paths in by_sha.items() if len(paths) != 1}
    assert not conflict, f"同一 sha256 落了多个路径：{conflict}"
    assert len(by_sha) < len(artifacts), "夹具里存在同内容不同条目，应被合并到同一物理文件"
    physical = set()
    for root, _dirs, files in os.walk(os.path.join(svc.workspace_dir, "artifacts")):
        for name in files:
            physical.add(os.path.join(root, name))
    assert len(physical) == len(by_sha), "磁盘上的物理文件数应等于唯一 sha256 数"


def test_dry_run_does_not_download(policy, fake_adapter, fake_llm, workdir):
    service = CoreService(policy, workspace_root=os.path.join(workdir, "ws-dry"),
                          run_id="dry", adapter=fake_adapter, llm=fake_llm, log=_quiet)
    try:
        service.inventory()
        result = service.acquire(dry_run=True)
        assert result.status == "needs_review"
        assert not [a for a in service.store.list_artifacts() if a["path"]]
        assert not [c for c in fake_adapter.calls if c.startswith(("download:", "export:"))]
    finally:
        service.close()


def test_online_resources_are_exported(svc, fake_adapter):
    svc.acquire()
    exports = [c for c in fake_adapter.calls if c.startswith("export:")]
    assert exports, "在线 docx/sheet/slides 应该走导出"
    for token in ("A_ONLINE_DOC", "B_ONLINE_SHEET", "B_ONLINE_SLIDES"):
        artifact = svc.store.get_artifact(token)
        assert artifact and artifact["status"] == ITEM_ACQUIRED, (token, artifact)
        assert artifact["acquire_mode"] == "export"
        assert os.path.isfile(artifact["path"])


def test_export_failure_becomes_pending_export_not_silent_success(policy, file_provider,
                                                                  fake_llm, workdir):
    tree = build_fake_tree()
    adapter = FakeCloudAdapter(tree, file_provider, page_size_cap=50,
                              fail_tokens={"B_ONLINE_SHEET": "permanent"})
    service = CoreService(policy, workspace_root=os.path.join(workdir, "ws-exp"),
                          run_id="exp", adapter=adapter, llm=fake_llm, log=_quiet)
    try:
        service.inventory()
        service.acquire()
        artifact = service.store.get_artifact("B_ONLINE_SHEET")
        assert artifact["status"] == ITEM_PENDING_EXPORT
        assert not artifact["path"] or not os.path.exists(artifact["path"])
        decision = service.store.latest_decisions("acquire")["B_ONLINE_SHEET"]
        assert decision["status"] == ITEM_PENDING_EXPORT
        assert "pending_export" in decision["reason_codes"]
    finally:
        service.close()


def test_download_claiming_success_without_a_file_is_a_failure(policy, file_provider,
                                                               fake_llm, workdir):
    tree = build_fake_tree()
    adapter = FakeCloudAdapter(tree, file_provider, page_size_cap=50,
                              fail_tokens={"B_RU_PDF": "missing"})
    service = CoreService(policy, workspace_root=os.path.join(workdir, "ws-missing"),
                          run_id="missing", adapter=adapter, llm=fake_llm, log=_quiet)
    try:
        service.inventory()
        service.acquire()
        artifact = service.store.get_artifact("B_RU_PDF")
        assert artifact["status"] == ITEM_ACQUIRE_FAILED
        assert "不存在" in (artifact["error"] or "")
    finally:
        service.close()


def test_acquisition_report_has_one_line_per_item(svc):
    result = svc.acquire()
    path = result.summary["report_path"]
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    assert len(rows) == result.summary["total_selected"]
    for row in rows:
        assert row["status"]
        assert isinstance(row["reason_codes"], list)
        assert row["source_name"] in ("知识库_2026", "docQA.llm")
        assert row["relative_path"]


def test_extension_gate_results_are_correct(svc):
    svc.acquire()
    decisions = svc.store.latest_decisions("acquire")
    assert decisions["A_OLD"]["status"] == ITEM_CONVERSION_REQUIRED
    assert decisions["A_IMG"]["status"] == ITEM_REJECTED
    assert "extension_unsupported" in decisions["A_IMG"]["reason_codes"]
    assert decisions["B_SHORTCUT"]["status"] == ITEM_REJECTED
    assert decisions["A_UNKNOWN"]["status"] == ITEM_NEEDS_REVIEW
    assert "extension_missing" in decisions["A_UNKNOWN"]["reason_codes"]
    assert decisions["A_PDF_1"]["status"] == ITEM_ACQUIRED


def test_run_acquire_records_selection_criteria(svc):
    result = svc.acquire(sample=5, sample_seed=11)
    selection = result.summary["selection"]
    assert selection["sample"] == 5
    assert selection["sample_seed"] == 11
    assert selection["sample_strata"] == list(DEFAULT_SAMPLE_STRATA)
    assert selection["strata_covered"] >= 1
    assert result.summary["total_selected"] == 5
