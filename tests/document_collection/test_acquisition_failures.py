"""计划 Task 3 要求模拟的失败分支：截断、校验和错误、磁盘空间不足。

这几条以前只有"429 / 5xx / 超时 / 权限 / 导出失败"的覆盖，
计划里明确列出的另外三种没有用例，属于验证方案的缺口，这里补齐。
"""

from __future__ import annotations

import os

import pytest

from conftest import FakeCloudAdapter, build_fake_tree

from core.document_collection import acquisition as acq
from core.document_collection.contracts import ITEM_ACQUIRE_FAILED
from core.document_collection.service import CoreService


def _quiet(_m: str) -> None:
    return None


class DeclaredSizeAdapter(FakeCloudAdapter):
    """导出时谎报字节数，用来验证截断能被检出。"""

    def __init__(self, *a, declared_delta: int = 100000, **kw):
        super().__init__(*a, **kw)
        self.declared_delta = declared_delta

    def export_online(self, item_token, item_type, dest_rel_dir, *, workdir,
                      extension=None, overwrite=True, timeout=None, capability=""):
        payload = super().export_online(item_token, item_type, dest_rel_dir,
                                        workdir=workdir, extension=extension,
                                        overwrite=overwrite, timeout=timeout)
        payload["size_bytes"] = int(payload.get("size_bytes") or 0) + self.declared_delta
        payload["raw"] = dict(payload.get("raw") or {})
        payload["raw"]["size_bytes"] = payload["size_bytes"]
        return payload


class DiskFullAdapter(FakeCloudAdapter):
    """下载时抛磁盘满，用来验证不会让整批崩掉。"""

    def __init__(self, *a, fail_with: Exception, **kw):
        super().__init__(*a, **kw)
        self.fail_with = fail_with

    def download_file(self, item_token, dest_rel_path, *, workdir, overwrite=True,
                      timeout=None, capability=""):
        raise self.fail_with


def _service(policy, adapter, llm, workdir, run_id):
    return CoreService(policy, workspace_root=os.path.join(workdir, "ws"),
                       run_id=run_id, adapter=adapter, llm=llm, log=_quiet)


def test_truncated_transfer_is_detected_and_blob_removed(policy, file_provider, fake_llm, workdir):
    """服务端声明 100000+ 字节但只落盘一半 -> 必须判失败，不能当成完整文件。"""
    tree = build_fake_tree()
    adapter = DeclaredSizeAdapter(tree, file_provider, page_size_cap=50, declared_delta=99999)
    service = _service(policy, adapter, fake_llm, workdir, "trunc")
    try:
        service.inventory()
        service.acquire(item_types=("online_doc", "online_sheet", "online_slides"))
        rows = [r for r in service.store.list_artifacts() if r.get("acquire_mode") == "export"]
        assert rows, "应该有导出产物记录"
        for row in rows:
            assert row["status"] == ITEM_ACQUIRE_FAILED, row
            assert not row["path"] or not os.path.exists(row["path"]), "截断产物必须被清掉"
        decisions = service.store.latest_decisions("acquire")
        hit = [d for d in decisions.values() if "truncated_transfer" in d["reason_codes"]]
        assert hit, "必须留下 truncated_transfer 的原因码"
        assert "字节" in (hit[0]["evidence"].get("error") or "") or hit[0]["status"] == ITEM_ACQUIRE_FAILED
    finally:
        service.close()


def test_matching_declared_size_is_recorded_as_verified(policy, file_provider, fake_llm, workdir):
    tree = build_fake_tree()
    adapter = DeclaredSizeAdapter(tree, file_provider, page_size_cap=50, declared_delta=0)
    service = _service(policy, adapter, fake_llm, workdir, "verified")
    try:
        service.inventory()
        service.acquire(item_types=("online_doc",))
        report = os.path.join(service.workspace_dir, "acquisition_report.jsonl")
        import json
        rows = [json.loads(l) for l in open(report, encoding="utf-8") if l.strip()]
        exported = [r for r in rows if r.get("acquire_mode") == "export"]
        assert exported
        assert all(r["declared_size_verified"] is True for r in exported)
        assert all(r["status"] == "acquired" for r in exported)
    finally:
        service.close()


class NoDeclaredSizeAdapter(FakeCloudAdapter):
    """模拟"数据源不返回字节数"的情况（例如本地适配器，或未来换的网盘）。"""

    def download_file(self, item_token, dest_rel_path, *, workdir, overwrite=True,
                      timeout=None, capability=""):
        payload = super().download_file(item_token, dest_rel_path, workdir=workdir,
                                        overwrite=overwrite, timeout=timeout)
        payload.pop("size_bytes", None)
        return payload


def test_download_without_declared_size_is_honestly_marked(policy, file_provider, fake_llm, workdir):
    """拿不到服务端声明字节数时，必须如实标注"没做过截断校验"，不能假装校验过。"""
    import json
    adapter = NoDeclaredSizeAdapter(build_fake_tree(), file_provider, page_size_cap=50)
    service = _service(policy, adapter, fake_llm, workdir, "nodecl")
    try:
        service.inventory()
        service.acquire()
        report = os.path.join(service.workspace_dir, "acquisition_report.jsonl")
        rows = [json.loads(l) for l in open(report, encoding="utf-8") if l.strip()]
        downloaded = [r for r in rows if r.get("acquire_mode") == "download"
                      and r["status"] == "acquired"]
        assert downloaded
        assert all(r["declared_size_verified"] is False for r in downloaded)
        assert all("declared_size_unavailable" in r["reason_codes"] for r in downloaded)
    finally:
        service.close()


def test_declared_size_is_verified_when_available(policy, fake_adapter, fake_llm, workdir):
    """飞书 download/export 都会返回 size_bytes，正常路径必须标记为已校验。"""
    import json
    service = _service(policy, fake_adapter, fake_llm, workdir, "decl")
    try:
        service.inventory()
        service.acquire(limit=6)
        report = os.path.join(service.workspace_dir, "acquisition_report.jsonl")
        rows = [json.loads(l) for l in open(report, encoding="utf-8") if l.strip()]
        ok = [r for r in rows if r["status"] == "acquired"]
        assert ok
        assert all(r["declared_size_verified"] is True for r in ok)
        assert all("declared_size_unavailable" not in r["reason_codes"] for r in ok)
    finally:
        service.close()


def test_artifact_paths_survive_a_very_deep_workspace(policy, fake_adapter, fake_llm, workdir):
    """D20 回归：内容寻址文件名 64+ 字符，工作区一深就撞 Windows MAX_PATH。

    注意：超过 260 字符的路径，普通 os.path.isfile 也看不见它 —— 
    文件确实存在，只是必须走 \\\\?\\ 扩展长度 API 才能访问。
    所以断言也要用 winpath，这正好反过来证明产物是真的落盘了。
    """
    from core.document_collection import winpath

    deep = workdir
    for i in range(6):
        deep = os.path.join(deep, f"nested_directory_level_{i}_with_a_fairly_long_name")
    service = CoreService(policy, workspace_root=deep, run_id="deep-run-id-with-suffix",
                          adapter=fake_adapter, llm=fake_llm, log=_quiet)
    try:
        service.inventory()
        result = service.acquire(limit=6)
        acquired = [r for r in service.store.list_artifacts() if r["status"] == "acquired"]
        assert acquired, result.summary
        for row in acquired:
            assert row["path"] and winpath.isfile(row["path"]), row["path"]
            assert winpath.getsize(row["path"]) == row["size_bytes"]
        service.extract(limit=6)
        extracted = [r for r in service.store.list_extractions() if r["status"] == "extracted"]
        assert extracted
        for row in extracted:
            if row.get("text_path"):
                assert winpath.isfile(row["text_path"]), row["text_path"]
        # 报告产物同样要能写出来
        service.deduplicate()
        service.screen()
        service.report()
        for name in ("coverage_report.md", "all_decisions.csv", "screening_report.jsonl",
                     "audit_report.json"):
            if name == "audit_report.json":
                service.audit()
            assert winpath.isfile(os.path.join(service.workspace_dir, name)), name
    finally:
        service.close()


def test_post_store_checksum_mismatch_is_detected(policy, fake_adapter, fake_llm, workdir,
                                                  monkeypatch):
    """落盘后重算哈希不一致（磁盘/杀软/占用导致）必须判失败并清掉坏文件。"""
    calls = {"n": 0}
    real = acq.sha256_file

    def flaky(path, chunk_size=1 << 20):
        calls["n"] += 1
        digest = real(path, chunk_size)
        # 第二次调用（落盘后复核）返回不同的值，模拟文件在移动过程中被改写
        return ("0" * 64) if calls["n"] % 2 == 0 else digest

    monkeypatch.setattr(acq, "sha256_file", flaky)
    service = _service(policy, fake_adapter, fake_llm, workdir, "checksum")
    try:
        service.inventory()
        result = service.acquire(limit=4)
        assert result.summary["status_counts"].get("acquire_failed", 0) >= 1
        decisions = service.store.latest_decisions("acquire")
        hit = [d for d in decisions.values() if "checksum_mismatch_after_store" in d["reason_codes"]]
        assert hit, [d["reason_codes"] for d in decisions.values()]
    finally:
        service.close()


@pytest.mark.parametrize("exc", [
    OSError(28, "No space left on device"),
    PermissionError(13, "Permission denied"),
    OSError(2, "No such file or directory"),
])
def test_disk_and_permission_errors_do_not_crash_the_batch(policy, file_provider, fake_llm,
                                                           workdir, exc):
    tree = build_fake_tree()
    adapter = DiskFullAdapter(tree, file_provider, page_size_cap=50, fail_with=exc)
    service = _service(policy, adapter, fake_llm, workdir, f"disk-{type(exc).__name__}")
    try:
        service.inventory()
        result = service.acquire()          # 不能抛异常
        counts = result.summary["status_counts"]
        assert counts.get("acquire_failed", 0) >= 1
        assert result.status in ("partial", "failed", "needs_review")
        # 每条都要有可读的错误信息，不能只剩一个空状态
        failed = [r for r in service.store.list_artifacts() if r["status"] == ITEM_ACQUIRE_FAILED]
        assert failed and all(r["error"] for r in failed)
    finally:
        service.close()


def test_no_partial_files_after_disk_full(policy, file_provider, fake_llm, workdir):
    tree = build_fake_tree()
    adapter = DiskFullAdapter(tree, file_provider, page_size_cap=50,
                              fail_with=OSError(28, "No space left on device"))
    service = _service(policy, adapter, fake_llm, workdir, "nopart")
    try:
        service.inventory()
        service.acquire()
        leftovers = []
        for root, _dirs, files in os.walk(service.workspace_dir):
            for name in files:
                if name.endswith(".part"):
                    leftovers.append(os.path.join(root, name))
        assert leftovers == []
    finally:
        service.close()
