"""本地优先入口（ingest）与并发等价性测试。

覆盖三件事：
1. ingest 直接把本地文件夹登记成 acquired 条目，后续 extract 无需改动即可接上；
2. ingest 复用 acquire 的格式/大小闸门，结论口径一致（不支持格式仍被拒）；
3. --concurrency > 1 与串行跑出**完全相同**的结论与顺序（并发只提速不改结果）。
"""

from __future__ import annotations

import json
import os

import pytest

from core.document_collection import winpath
from core.document_collection.ingest import discover_files
from core.document_collection.service import CoreService


def _quiet(_msg: str) -> None:
    return None


def _write(path: str, data: bytes) -> None:
    winpath.makedirs(os.path.dirname(path))
    with open(path, "wb") as fh:
        fh.write(data)


@pytest.fixture
def local_docs(workdir, generated_documents):
    """一个本地文件夹，里面放几种真实格式 + 一个不支持格式 + 一个 Office 临时文件。"""
    root = os.path.join(str(workdir), "我的资料")
    os.makedirs(root, exist_ok=True)
    # 从会话夹具里拷真实文件（pdf/docx/xlsx/txt），保证抽取器能处理
    picks = {
        "A_PDF_1": "course.pdf",
        "A_FR_DOCX": "td.docx",
        "A_FR_XLSX": "exo.xlsx",
        "A_TXT_1": "notes.txt",
    }
    for token, name in picks.items():
        src = generated_documents[token]["path"]
        with open(src, "rb") as fh:
            _write(os.path.join(root, name), fh.read())
    # 放在子目录里一个，验证递归
    sub = os.path.join(root, "sub")
    with open(generated_documents["A_PDF_1"]["path"], "rb") as fh:
        _write(os.path.join(sub, "nested.pdf"), fh.read())
    # 不支持的格式（图片）与隐藏临时文件
    _write(os.path.join(root, "photo.jpg"), b"\xff\xd8\xff\xe0" + b"0" * 64)
    _write(os.path.join(root, "~$draft.docx"), b"lock")
    return root


def test_discover_files_is_recursive_and_skips_hidden(local_docs):
    files = discover_files(local_docs)
    rels = sorted(r for _p, r in files)
    assert "sub/nested.pdf" in rels, "必须递归子目录"
    assert not any(r.startswith("~$") for r in rels), "必须跳过 Office 临时文件"
    # 排序稳定，保证可复现
    assert [r for _p, r in files] == sorted(r for _p, r in files)


def test_ingest_registers_items_as_acquired(policy, workdir, local_docs):
    service = CoreService(policy, workspace_root=os.path.join(workdir, "ws"),
                          run_id="ingest-1", log=_quiet)
    try:
        result = service.ingest(root=local_docs)
        assert result.status in ("completed", "partial")
        counts = result.summary["status_counts"]
        assert counts.get("acquired", 0) >= 5, counts
        # 图片被格式闸门拒绝（与 acquire 同一口径）
        assert counts.get("rejected", 0) >= 1
        assert os.path.isfile(os.path.join(service.workspace_dir, "ingest_report.jsonl"))
        # 后续 extract 不改任何代码就能接上：item 状态是 acquired
        items = service.store.list_items(statuses=("acquired",))
        assert len(items) >= 5
        assert result.next_action == "collection.extract"
    finally:
        service.close()


def test_ingest_references_source_in_place_by_default(policy, workdir, local_docs):
    """默认不复制、不改动源文件：artifact 指向原路径。"""
    service = CoreService(policy, workspace_root=os.path.join(workdir, "ws"),
                          run_id="ingest-inplace", log=_quiet)
    try:
        service.ingest(root=local_docs)
        artifacts = service.store.list_artifacts(statuses=("acquired",))
        assert artifacts, "至少要有一条已登记产物"
        for art in artifacts:
            assert os.path.abspath(art["path"]).startswith(os.path.abspath(local_docs))
        # 源文件仍是原路径，内容未被动过
        item = service.store.list_items(statuses=("acquired",))[0]
        meta = item.get("metadata") or {}
        assert meta.get("ingest_source_root") == os.path.abspath(local_docs)
    finally:
        service.close()


def test_ingest_copy_mode_populates_content_store(policy, workdir, local_docs):
    service = CoreService(policy, workspace_root=os.path.join(workdir, "ws"),
                          run_id="ingest-copy", log=_quiet)
    try:
        service.ingest(root=local_docs, copy=True)
        arts = service.store.list_artifacts(statuses=("acquired",))
        assert arts
        for art in arts:
            assert os.sep + "artifacts" + os.sep in art["path"], art["path"]
            assert os.path.isfile(art["path"])
    finally:
        service.close()


def test_ingest_then_extract_and_deduplicate_works(policy, workdir, local_docs):
    """整条本地链路前四步必须能跑通。"""
    service = CoreService(policy, workspace_root=os.path.join(workdir, "ws"),
                          run_id="ingest-chain", log=_quiet)
    try:
        service.ingest(root=local_docs)
        ex = service.extract()
        assert ex.summary["status_counts"].get("extracted", 0) >= 4
        dd = service.deduplicate()
        assert dd.status == "completed"
    finally:
        service.close()


def test_ingest_missing_dir_fails_cleanly(policy, workdir):
    service = CoreService(policy, workspace_root=os.path.join(workdir, "ws"),
                          run_id="ingest-missing", log=_quiet)
    try:
        result = service.ingest(root=os.path.join(str(workdir), "does-not-exist"))
        assert result.status == "failed"
        assert result.errors
    finally:
        service.close()


# ---------------------------------------------------------------------------
# 并发等价性：--concurrency N 与串行结论必须逐条一致
# ---------------------------------------------------------------------------
def _run_local_chain(policy, workspace_root, run_id, concurrency):
    service = CoreService(policy, workspace_root=workspace_root, run_id=run_id,
                          log=_quiet, concurrency=concurrency)
    try:
        ex = service.extract().summary["status_counts"]
        sc = service.screen().summary["status_counts"]
        dd = service.deduplicate().summary
        return {"extract": ex, "screen": sc,
                "dedup_unique": dd["unique_items"], "dedup_dups": dd["duplicate_items"]}
    finally:
        service.close()


def test_concurrency_produces_identical_results(policy, workdir, local_docs):
    """并发只提速：串行与并发 4 的结论、顺序必须完全相同。"""
    serial_root = os.path.join(workdir, "ws-serial")
    conc_root = os.path.join(workdir, "ws-conc")

    s1 = CoreService(policy, workspace_root=serial_root, run_id="s", log=_quiet)
    s1.ingest(root=local_docs)
    s1.close()
    s2 = CoreService(policy, workspace_root=conc_root, run_id="c", log=_quiet)
    s2.ingest(root=local_docs)
    s2.close()

    serial = _run_local_chain(policy, serial_root, "s", 1)
    conc = _run_local_chain(policy, conc_root, "c", 4)
    assert serial == conc, f"并发结果与串行不一致：{serial} vs {conc}"

    # 报告行序也必须一致（并发回填按 index，不能随调度漂移）
    with open(os.path.join(serial_root, "s", "extraction_report.jsonl"), encoding="utf-8") as fh:
        serial_lines = [json.loads(x)["item_id"] for x in fh if x.strip()]
    with open(os.path.join(conc_root, "c", "extraction_report.jsonl"), encoding="utf-8") as fh:
        conc_lines = [json.loads(x)["item_id"] for x in fh if x.strip()]
    assert serial_lines == conc_lines


# ---------------------------------------------------------------------------
# 定向白名单（C1）：只给指定的 item_id 生成 query
# ---------------------------------------------------------------------------
def test_query_item_ids_whitelist_targets_only_listed_docs(policy, workdir, local_docs, fake_llm):
    """只给白名单里的 item_id 生成 query —— 这次 30 篇任务原本要靠一次性脚本绕开 CLI。"""
    from core.document_collection.query_generation import run_query_generation, QueryOptions

    service = CoreService(policy, workspace_root=os.path.join(workdir, "ws"),
                          run_id="whitelist", log=_quiet)
    try:
        service.ingest(root=local_docs)
        service.extract()
        service.screen()
        accepted = [k for k, v in service.store.latest_decisions("screen").items()
                    if v.get("status") == "accepted"]
        assert len(accepted) >= 2, "至少要有两份 accepted 才能验证白名单"

        # 白名单只放 1 条：即使库里有更多 accepted，也只应处理这 1 条
        target = accepted[:1]
        summary = run_query_generation(
            policy=policy, store=service.store, workspace_dir=service.workspace_dir,
            run_id="whitelist", llm=fake_llm,
            options=QueryOptions(item_ids=tuple(target)), log=_quiet)
        assert summary["total_documents"] == 1, summary["total_documents"]
        generated = {q["item_id"] for q in service.store.all_queries()}
        assert generated <= set(target), f"白名单外的条目也被生成了：{generated - set(target)}"
    finally:
        service.close()


def test_report_writes_delivery_summary(policy, workdir, local_docs):
    """report 必须产出一张人读汇总（文件名/类型/学科/语种/query 数）。"""
    service = CoreService(policy, workspace_root=os.path.join(workdir, "ws"),
                          run_id="delivery", log=_quiet)
    try:
        service.ingest(root=local_docs)
        service.extract()
        service.screen()
        result = service.report()
        csv_path = os.path.join(service.workspace_dir, "delivery_summary.csv")
        assert os.path.isfile(csv_path)
        with open(csv_path, encoding="utf-8-sig") as fh:
            header = fh.readline().strip()
        for col in ("文件名", "文件类型", "学科", "语种", "Query数", "状态"):
            assert col in header, f"汇总表缺少列 {col}"
        kinds = {a["kind"] for a in result.artifacts}
        assert "report_artifact" in kinds
    finally:
        service.close()
