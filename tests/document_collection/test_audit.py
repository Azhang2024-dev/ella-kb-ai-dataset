"""验收门禁自检测试（计划第 9 节的 5 个检查点）。

这些用例的关键不是"audit 能跑"，而是**audit 真的能抓到违规**：
每个检查点都要有一个"故意做坏"的用例，否则门禁等于没有。
"""

from __future__ import annotations

import json
import os

import pytest

from core.document_collection.audit import (
    DETERMINISTIC_STAGES,
    AuditReport,
    audit_run,
    format_audit_report,
    write_audit_report,
)
from core.document_collection.service import CoreService


def _quiet(_m: str) -> None:
    return None


@pytest.fixture
def clean_run(policy, fake_adapter, fake_llm, workdir):
    service = CoreService(policy, workspace_root=os.path.join(workdir, "ws"),
                          run_id="audit-clean", adapter=fake_adapter, llm=fake_llm, log=_quiet)
    service.inventory()
    service.acquire()
    service.extract()
    service.deduplicate()
    service.screen()
    yield service
    service.close()


def _checks(report, checkpoint_prefix):
    return [c for c in report.checks if c.checkpoint.startswith(checkpoint_prefix)]


def test_clean_run_passes_every_checkpoint(clean_run):
    report = audit_run(policy=clean_run.policy, store=clean_run.store,
                       workspace_dir=clean_run.workspace_dir, run_id=clean_run.run_id)
    failed = [(c.checkpoint, c.name, c.detail) for c in report.failed]
    assert report.passed, failed
    for prefix in ("1-", "2-", "3-", "4-", "5-"):
        assert _checks(report, prefix), f"检查点 {prefix} 没有任何检查项"


def test_audit_report_is_written_and_readable(clean_run):
    report = audit_run(policy=clean_run.policy, store=clean_run.store,
                       workspace_dir=clean_run.workspace_dir, run_id=clean_run.run_id)
    json_path = os.path.join(clean_run.workspace_dir, "audit_report.json")
    write_audit_report(json_path, report)
    payload = json.load(open(json_path, encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["total_checks"] == len(report.checks)
    text = format_audit_report(report)
    assert "检查点 1-能运行" in text and "检查点 3-没有误放行" in text
    assert "PASS" in text


# ---------------------------------------------------------------------------
# 每个检查点都要能被"做坏"抓到
# ---------------------------------------------------------------------------
def test_audit_catches_oversize_accepted(clean_run):
    accepted = [k for k, v in clean_run.store.latest_decisions("screen").items()
                if v["status"] == "accepted"]
    assert accepted
    victim = accepted[0]
    clean_run.store.upsert_artifact(victim, size_bytes=200 * 1024 * 1024)
    report = audit_run(policy=clean_run.policy, store=clean_run.store,
                       workspace_dir=clean_run.workspace_dir, run_id=clean_run.run_id)
    assert not report.passed
    hit = [c for c in report.failed if "超大小" in c.name]
    assert hit and hit[0].evidence["violations"][0]["item_id"] == victim


def test_audit_catches_unsupported_extension_accepted(clean_run):
    accepted = [k for k, v in clean_run.store.latest_decisions("screen").items()
                if v["status"] == "accepted"]
    victim = accepted[0]
    item = clean_run.store.get_item(victim)
    item["extension"] = "jpg"
    clean_run.store.upsert_item(item)
    report = audit_run(policy=clean_run.policy, store=clean_run.store,
                       workspace_dir=clean_run.workspace_dir, run_id=clean_run.run_id)
    assert any("不支持格式" in c.name for c in report.failed)


def test_audit_catches_hard_rejection_overridden_by_screen(clean_run):
    accepted = [k for k, v in clean_run.store.latest_decisions("screen").items()
                if v["status"] == "accepted"]
    victim = accepted[0]
    clean_run.store.add_decision(victim, "acquire", "rejected", ["size_over_limit"], {})
    report = audit_run(policy=clean_run.policy, store=clean_run.store,
                       workspace_dir=clean_run.workspace_dir, run_id=clean_run.run_id)
    assert any("翻案" in c.name for c in report.failed)


def test_audit_catches_duplicate_accepted_in_one_group(clean_run):
    clean_run.store.set_dedup("deadbeef", "sha256", "DUP_A", True, "test")
    clean_run.store.set_dedup("deadbeef", "sha256", "DUP_B", False, "test")
    clean_run.store.add_decision("DUP_A", "screen", "accepted", ["ok"], {})
    clean_run.store.add_decision("DUP_B", "screen", "accepted", ["ok"], {})
    report = audit_run(policy=clean_run.policy, store=clean_run.store,
                       workspace_dir=clean_run.workspace_dir, run_id=clean_run.run_id)
    assert any("同一去重组" in c.name for c in report.failed)


def test_audit_catches_missing_explanation(clean_run):
    decisions = clean_run.store.latest_decisions("screen")
    victim = next(iter(decisions))
    clean_run.store.add_decision(victim, "screen", "rejected", [], {})
    report = audit_run(policy=clean_run.policy, store=clean_run.store,
                       workspace_dir=clean_run.workspace_dir, run_id=clean_run.run_id)
    failed_names = " ".join(c.name for c in report.failed)
    assert "reason_codes" in failed_names or "决定原因" in failed_names


def test_audit_catches_broken_manifest(clean_run, workdir):
    broken_dir = os.path.join(workdir, "broken_skill")
    os.makedirs(broken_dir, exist_ok=True)
    manifest = json.load(open(os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "skills", "document_collection", "capability-manifest.json"), encoding="utf-8"))
    manifest["capabilities"] = manifest["capabilities"][:-1]     # 少一个能力
    with open(os.path.join(broken_dir, "capability-manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False)
    with open(os.path.join(broken_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nname: x\ndescription: y\n---\n# x\n")
    report = audit_run(policy=clean_run.policy, store=clean_run.store,
                       workspace_dir=clean_run.workspace_dir, run_id=clean_run.run_id,
                       skill_dir=broken_dir)
    assert any(c.checkpoint.startswith("1-") and not c.passed for c in report.checks)


def test_audit_catches_readonly_policy_weakened(clean_run):
    raw = json.loads(json.dumps(clean_run.policy.raw, ensure_ascii=False))
    raw["safety"]["allowlisted_lark_commands"].append("drive +delete")
    from core.document_collection.config import CollectionPolicy
    weak = CollectionPolicy(path=clean_run.policy.path, raw=raw)
    report = audit_run(policy=weak, store=clean_run.store,
                       workspace_dir=clean_run.workspace_dir, run_id=clean_run.run_id)
    assert any("只读" in c.name and not c.passed for c in report.checks)


# ---------------------------------------------------------------------------
# 检查点 5：复跑对比
# ---------------------------------------------------------------------------
def _run_pipeline(policy, fake_adapter, fake_llm, workdir, run_id, semantic=False):
    service = CoreService(policy, workspace_root=os.path.join(workdir, "cmp"),
                          run_id=run_id, adapter=fake_adapter, llm=fake_llm, log=_quiet)
    service.inventory()
    service.acquire()
    service.extract()
    service.deduplicate()
    service.screen(semantic=semantic)
    path = service.workspace_dir
    service.close()
    return path


def test_identical_reruns_pass_the_reproducibility_checkpoint(policy, fake_adapter,
                                                              fake_llm, workdir):
    first = _run_pipeline(policy, fake_adapter, fake_llm, workdir, "cmp-1")
    second = _run_pipeline(policy, fake_adapter, fake_llm, workdir, "cmp-2")
    from core.document_collection.state import StateStore
    store = StateStore(os.path.join(second, "state.sqlite3"))
    try:
        report = audit_run(policy=policy, store=store, workspace_dir=second,
                           run_id="cmp-2", compare_run_dir=first)
    finally:
        store.close()
    compare_checks = [c for c in report.checks if "一致" in c.name or "可比性" in c.name]
    assert compare_checks, "必须有复跑对比检查项"
    assert all(c.passed for c in compare_checks), [(c.name, c.detail) for c in compare_checks]


def test_semantic_runs_do_not_fail_the_reproducibility_checkpoint(policy, fake_adapter,
                                                                  fake_llm, workdir):
    """启用模型时筛选决策不保证逐字一致，检查点必须降级为"可比性"而不是判失败。"""
    first = _run_pipeline(policy, fake_adapter, fake_llm, workdir, "sem-1", semantic=True)
    second = _run_pipeline(policy, fake_adapter, fake_llm, workdir, "sem-2", semantic=True)
    from core.document_collection.state import StateStore
    store = StateStore(os.path.join(second, "state.sqlite3"))
    try:
        report = audit_run(policy=policy, store=store, workspace_dir=second,
                           run_id="sem-2", compare_run_dir=first)
    finally:
        store.close()
    info = [c for c in report.checks if "可比性" in c.name]
    assert info and info[0].passed
    assert "语义复核=True" in info[0].detail
    # 确定性阶段仍然必须一致
    det = [c for c in report.checks if "确定性阶段" in c.name]
    assert det and det[0].passed, det[0].detail if det else "missing"


def test_compare_detects_a_real_regression(policy, fake_adapter, fake_llm, workdir):
    first = _run_pipeline(policy, fake_adapter, fake_llm, workdir, "reg-1")
    second = _run_pipeline(policy, fake_adapter, fake_llm, workdir, "reg-2")
    from core.document_collection.state import StateStore
    store = StateStore(os.path.join(second, "state.sqlite3"))
    try:
        # 人为篡改第二次的采集哈希，模拟"产物变了"
        rows = store.list_artifacts(statuses=("acquired",))
        assert rows
        store.upsert_artifact(rows[0]["item_id"], sha256="0" * 64)
        report = audit_run(policy=policy, store=store, workspace_dir=second,
                           run_id="reg-2", compare_run_dir=first)
        assert any("SHA-256" in c.name and not c.passed for c in report.checks), \
            [(c.name, c.detail) for c in report.checks if not c.passed]
    finally:
        store.close()


def test_compare_with_missing_run_is_reported(clean_run, workdir):
    report = audit_run(policy=clean_run.policy, store=clean_run.store,
                       workspace_dir=clean_run.workspace_dir, run_id=clean_run.run_id,
                       compare_run_dir=os.path.join(workdir, "does-not-exist"))
    assert any("对比运行存在" in c.name and not c.passed for c in report.checks)


def test_semantic_flag_is_read_from_the_capability_key(clean_run):
    """回归：stage_state_json 的键是 collection.screen，不是 screen。"""
    from core.document_collection.audit import _semantic_enabled

    clean_run.store.touch_run(clean_run.run_id,
                              {"collection.screen": {"semantic_review_enabled": True}})
    assert _semantic_enabled(clean_run.store, clean_run.run_id) is True
    clean_run.store.touch_run(clean_run.run_id,
                              {"collection.screen": {"semantic_review_enabled": False}})
    assert _semantic_enabled(clean_run.store, clean_run.run_id) is False
    clean_run.store.touch_run(clean_run.run_id, {})
    assert _semantic_enabled(clean_run.store, clean_run.run_id) is False


def test_deterministic_stages_are_the_non_llm_ones():
    assert DETERMINISTIC_STAGES == ("acquire", "extract", "deduplicate")
    assert "screen" not in DETERMINISTIC_STAGES
    assert "query" not in DETERMINISTIC_STAGES


def test_exported_artifacts_are_disclosed_not_failed(policy, fake_adapter, fake_llm, workdir):
    """实测：drive +export 每次由服务端重新渲染，字节不稳定。

    audit 必须把这件事**披露出来**而不是判失败，否则检查点 5 永远红，
    真正的确定性回归反而被淹没。
    """
    from core.document_collection.state import StateStore

    first = _run_pipeline(policy, fake_adapter, fake_llm, workdir, "exp-1")
    second = _run_pipeline(policy, fake_adapter, fake_llm, workdir, "exp-2")
    store = StateStore(os.path.join(second, "state.sqlite3"))
    try:
        # 人为让一个导出产物的哈希不同，模拟服务端重新渲染
        rows = [r for r in store.list_artifacts() if r.get("acquire_mode") == "export"]
        assert rows, "夹具里必须有在线导出产物"
        store.upsert_artifact(rows[0]["item_id"], sha256="f" * 64)
        report = audit_run(policy=policy, store=store, workspace_dir=second,
                           run_id="exp-2", compare_run_dir=first)
        disclosed = [c for c in report.checks if "字节不稳定" in c.name]
        assert disclosed and disclosed[0].passed
        assert "1 个 SHA-256 不同" in disclosed[0].detail
        assert disclosed[0].evidence["differing_item_ids"] == [rows[0]["item_id"]]
        # 下载类产物没被动过，必须仍然一致
        assert any("下载类产物" in c.name and c.passed for c in report.checks)
    finally:
        store.close()


def test_touch_run_merges_stage_state_instead_of_overwriting(workdir):
    """D18 回归：每条 CLI 命令都是独立进程，只带自己那一个阶段的摘要。

    覆盖式写入会让 cost_and_runtime.json / resume / audit 只看到最后一步。
    """
    from core.document_collection.state import StateStore

    store = StateStore(os.path.join(workdir, "merge.sqlite3"))
    try:
        store.start_run("r1", workdir)
        store.touch_run("r1", {"collection.inventory": {"unique_items": 22}})
        store.touch_run("r1", {"collection.acquire": {"total_selected": 20}})
        store.touch_run("r1", {"collection.report": {"accepted_total": 6}})
        stages = json.loads(store.get_run("r1")["stage_state_json"])
        assert set(stages) == {"collection.inventory", "collection.acquire",
                               "collection.report"}, stages
        assert stages["collection.inventory"]["unique_items"] == 22
        # 同一阶段再写一次应当更新而不是新增
        store.touch_run("r1", {"collection.acquire": {"total_selected": 21}})
        stages = json.loads(store.get_run("r1")["stage_state_json"])
        assert stages["collection.acquire"]["total_selected"] == 21
        assert len(stages) == 3
    finally:
        store.close()


def test_audit_after_report_still_sees_the_screen_stage(clean_run):
    """端到端验证 D18：先 report 再 audit，audit 仍要能读到 screen 的语义复核标记。"""
    from core.document_collection.audit import _semantic_enabled

    clean_run.report()
    stages = json.loads(clean_run.store.get_run(clean_run.run_id)["stage_state_json"])
    assert "collection.screen" in stages, f"report 之后 screen 摘要不能丢：{list(stages)}"
    assert "collection.report" in stages
    assert _semantic_enabled(clean_run.store, clean_run.run_id) is False   # 本用例未开语义


def test_audit_report_dataclass_summary():
    report = AuditReport(run_id="x", generated_at="now")
    assert report.passed is True
    assert report.failed == []
    assert report.to_dict()["total_checks"] == 0
