"""Skill 场景与文档一致性测试（计划 Task 8 的验证项）。

分两部分：
A. 文档层：SKILL.md / capability-manifest.json 与代码契约必须一致，
   且 Skill 里不能出现数据处理代码；
B. 行为层：7 个场景必须在真实代码上成立，不能只是写在文档里。
"""

from __future__ import annotations

import json
import os
import re

import pytest

from conftest import FakeCloudAdapter, build_fake_tree

from core.document_collection import contracts as C
from core.document_collection.adapters.base import SafetyViolation, SourceAdapter
from core.document_collection.adapters.lark_cli import LarkCliAdapter
from core.document_collection.config import CollectionPolicy
from core.document_collection.service import CoreService

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "document_collection")
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
MANIFEST = os.path.join(SKILL_DIR, "capability-manifest.json")
SCENARIOS = os.path.join(SKILL_DIR, "examples", "scenarios.md")


def _quiet(_msg: str) -> None:
    return None


# ===========================================================================
# A. 文档层
# ===========================================================================
def test_skill_md_has_valid_frontmatter():
    text = open(SKILL_MD, encoding="utf-8").read()
    assert text.startswith("---\n"), "SKILL.md 必须以 YAML frontmatter 开头"
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "frontmatter 没有正确闭合"
    block = match.group(1)
    assert re.search(r"^name:\s*\S+", block, re.MULTILINE)
    assert re.search(r"^description:\s*\S+", block, re.MULTILINE)
    name = re.search(r"^name:\s*(.+)$", block, re.MULTILINE).group(1).strip()
    assert name == "document_collection"
    description = re.search(r"^description:\s*(.+)$", block, re.MULTILINE).group(1).strip()
    assert len(description) > 60, "description 要写清触发条件与不负责的范围"
    assert "不负责" in description


def test_skill_md_contains_no_data_processing_code():
    """计划 Task 8 验证项：Skill 不包含数据处理代码。"""
    text = open(SKILL_MD, encoding="utf-8").read()
    forbidden = [r"^\s*import\s+\w", r"^\s*def\s+\w+", r"^\s*class\s+\w+",
                 r"sqlite3\.connect", r"\$PY\s+-c"]
    for pattern in forbidden:
        assert not re.search(pattern, text, re.MULTILINE), f"SKILL.md 里出现了代码：{pattern}"
    scenarios = open(SCENARIOS, encoding="utf-8").read()
    for pattern in forbidden:
        assert not re.search(pattern, scenarios, re.MULTILINE), f"场景文档里出现了代码：{pattern}"


def test_skill_md_capability_names_match_the_manifest():
    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    text = open(SKILL_MD, encoding="utf-8").read()
    for cap in manifest["capabilities"]:
        assert cap["name"] in text, f"SKILL.md 缺少能力名 {cap['name']}"
        assert cap["cli_command"] in text, f"SKILL.md 缺少 CLI 命令 {cap['cli_command']}"
    # Skill 里不允许出现 manifest 之外的能力名
    for found in set(re.findall(r"collection\.[a-z_]+", text)):
        assert found in C.CAPABILITY_NAMES, f"SKILL.md 出现了未登记能力 {found}"


def test_skill_md_documents_stop_conditions_and_forbidden_actions():
    text = open(SKILL_MD, encoding="utf-8").read()
    assert "必须停下来的情况" in text
    for signal in ("needs_review", "pending_export", "conversion_required",
                   "mime_extension_mismatch", "acquire_failed"):
        assert signal in text, signal
    assert "禁止动作" in text
    for rule in ("禁止修改云盘源", "禁止放宽大小上限", "禁止让模型覆盖硬失败",
                 "禁止伪造成功", "禁止在 query 文案里写定位"):
        assert rule in text, rule


def test_skill_md_documents_the_fixed_pipeline_order():
    text = open(SKILL_MD, encoding="utf-8").read()
    assert "inventory → acquire → extract → deduplicate → screen → query → questions → deliver → report" in text
    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    assert manifest["fixed_pipeline_order"] == [
        "collection.inventory", "collection.acquire", "collection.extract",
        "collection.deduplicate", "collection.screen", "collection.generate_queries",
        "collection.generate_questions", "collection.deliver", "collection.report",
    ]


def test_manifest_matches_contracts_exactly():
    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    assert manifest["contract_version"] == C.CONTRACT_VERSION
    assert [c["name"] for c in manifest["capabilities"]] == list(C.CAPABILITY_NAMES)
    for entry, contract in zip(manifest["capabilities"], C.CAPABILITIES):
        assert entry["cli_command"] == contract["cli_command"]
        assert entry["future_mcp_tool"] == contract["future_mcp_tool"]
        assert entry["version"] == "0.1"
        assert entry["side_effects"], "每个能力都必须声明副作用"
        assert isinstance(entry["requires_llm"], bool)
        assert isinstance(entry["retryable"], bool)
        assert isinstance(entry["idempotent"], bool)
        assert "inputs" in entry and "outputs" in entry
        bad = set(entry["side_effects"]) & set(C.FORBIDDEN_SIDE_EFFECTS)
        assert not bad, f"{entry['name']} 声明了禁止的副作用 {bad}"
    assert manifest["run_statuses"] == list(C.RUN_STATUSES)
    assert set(manifest["item_statuses"]) == set(C.ITEM_OUTPUT_STATUSES)
    assert set(manifest["result_envelope"]) == {
        "status", "run_id", "summary", "artifacts", "review_items", "errors", "next_action"}
    assert manifest["safety"]["source_readonly"] is True
    assert set(manifest["safety"]["forbidden_side_effects"]) == set(C.FORBIDDEN_SIDE_EFFECTS)


def test_manifest_allowlist_matches_the_real_policy(real_policy):
    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    assert manifest["safety"]["allowlisted_lark_commands"] == list(
        real_policy.allowlisted_lark_commands)


def test_scenarios_document_all_seven_required_cases():
    text = open(SCENARIOS, encoding="utf-8").read()
    for title in ("场景 1：只做 inventory", "场景 2：检查一个候选", "场景 3：拒绝超大小文件",
                  "场景 4：未知 MIME 进入人工复核", "场景 5：采集失败后恢复",
                  "场景 6：为一份合格文档生成 Query", "场景 7：拒绝修改云盘源文件的请求"):
        assert title in text, title


# ===========================================================================
# B. 行为层：7 个场景
# ===========================================================================
@pytest.fixture
def svc(policy, fake_adapter, fake_llm, workdir):
    service = CoreService(policy, workspace_root=os.path.join(workdir, "ws"),
                          run_id="skill-e2e", adapter=fake_adapter, llm=fake_llm, log=_quiet)
    yield service
    service.close()


def test_scenario_1_inventory_only_downloads_nothing(svc):
    result = svc.inventory()
    assert result.status in ("completed", "partial")
    assert result.next_action == "collection.acquire"
    for name in ("source_inventory.jsonl", "source_type_counts.csv",
                 "inventory_summary.md", "run_manifest.json"):
        assert os.path.isfile(os.path.join(svc.workspace_dir, name)), name
    artifacts_dir = os.path.join(svc.workspace_dir, "artifacts")
    assert not os.path.isdir(artifacts_dir) or not os.listdir(artifacts_dir), \
        "inventory 阶段不能下载任何正文"
    assert svc.store.list_artifacts() == []


def test_scenario_2_explain_one_candidate(svc):
    svc.inventory()
    svc.acquire()
    svc.extract()
    svc.deduplicate()
    svc.screen()
    accepted = [k for k, v in svc.store.latest_decisions("screen").items()
                if v["status"] == "accepted"]
    assert accepted
    item_id = accepted[0]
    item = svc.store.get_item(item_id)
    decisions = svc.store.decisions_for(item_id)
    assert decisions, "判定链不能为空"
    # 验收检查点 4：status / reason_codes / source_path / artifact_path
    for decision in decisions:
        assert decision["status"]
        assert isinstance(decision["reason_codes"], list) and decision["reason_codes"], \
            f"{decision['stage']} 阶段的决定缺少 reason_codes"
    assert item["source_name"] in ("知识库_2026", "docQA.llm")
    assert (item.get("metadata") or {}).get("relative_path")
    artifact = svc.store.get_artifact(item_id)
    assert artifact and artifact["path"] and os.path.isfile(artifact["path"])


def test_scenario_3_oversize_is_rejected_and_never_softened(policy, fake_adapter, fake_llm, workdir):
    raw = json.loads(json.dumps(policy.raw, ensure_ascii=False))
    raw["size_limits_mb"]["pdf"] = 1
    tight = CollectionPolicy(path=policy.path, raw=raw)
    service = CoreService(tight, workspace_root=os.path.join(workdir, "ws-tight"),
                          run_id="tight", adapter=fake_adapter, llm=fake_llm, log=_quiet)
    try:
        service.inventory()
        service.acquire()
        service.extract()
        service.deduplicate()
        service.screen(semantic=True)   # 即使模型说"是好文档"也不能翻案
        decision = service.store.latest_decisions("screen")["A_BIG"]
        assert decision["status"] == "rejected"
        assert "size_over_limit" in decision["reason_codes"]
    finally:
        service.close()


def test_scenario_4_unknown_mime_goes_to_human_review(svc):
    svc.inventory()
    svc.acquire()
    svc.extract()
    # A_UNKNOWN 没有扩展名，在采集闸门就被拦下，根本不会进抽取阶段
    acquire_decisions = svc.store.latest_decisions("acquire")
    unknown = acquire_decisions["A_UNKNOWN"]
    assert unknown["status"] == "needs_review"
    assert "extension_missing" in unknown["reason_codes"]
    svc.deduplicate()
    svc.screen()
    screened = svc.store.latest_decisions("screen")
    assert screened["A_UNKNOWN"]["status"] != "accepted"
    review_csv = os.path.join(svc.workspace_dir, "human_review_queue.csv")
    assert os.path.isfile(review_csv)
    text = open(review_csv, encoding="utf-8-sig").read()
    assert "mystery" in text


def test_scenario_5_recover_after_acquire_failure(policy, file_provider, fake_llm, workdir):
    """采集失败后必须能只重跑失败项，且已采到的不重复下载。"""
    tree = build_fake_tree()
    broken = FakeCloudAdapter(tree, file_provider, page_size_cap=50,
                              fail_tokens={"B_RU_PDF": "permanent"})
    workspace = os.path.join(workdir, "ws-resume")
    first = CoreService(policy, workspace_root=workspace, run_id="resume",
                        adapter=broken, llm=fake_llm, log=_quiet)
    try:
        first.inventory()
        summary = first.acquire().summary
        assert summary["status_counts"].get("acquire_failed", 0) >= 1
        assert first.store.get_item("B_RU_PDF")["status"] == "acquire_failed"
        downloaded_first = len(first.store.list_artifacts(statuses=("acquired",)))
    finally:
        first.close()

    healthy = FakeCloudAdapter(tree, file_provider, page_size_cap=50)
    second = CoreService(policy, workspace_root=workspace, run_id="resume",
                         adapter=healthy, llm=fake_llm, log=_quiet)
    try:
        result = second.acquire(include_statuses=("acquire_failed",))
        assert result.summary["total_selected"] == 1, "只应重跑失败的那一条"
        assert second.store.get_item("B_RU_PDF")["status"] == "acquired"
        # 再跑一次全量：已采到的应命中续跑缓存，不重复下载
        calls_before = len(healthy.calls)
        second.acquire()
        new_downloads = [c for c in healthy.calls[calls_before:] if c.startswith("download:")]
        assert new_downloads == [], "续跑不应该重复下载已完成的文件"
        assert len(second.store.list_artifacts(statuses=("acquired",))) >= downloaded_first
    finally:
        second.close()


def test_scenario_6_generate_queries_for_an_accepted_document(svc, fake_llm):
    svc.inventory()
    svc.acquire()
    svc.extract()
    svc.deduplicate()
    svc.screen(semantic=True)
    result = svc.generate_queries(limit=2)
    assert result.summary["status_counts"].get("validated", 0) >= 1
    queries = svc.store.all_queries()
    by_item: dict[str, list] = {}
    for row in queries:
        by_item.setdefault(row["item_id"], []).append(row)
    for rows in by_item.values():
        if len(rows) != 10:
            continue
        counts = sorted({r["question_count"] for r in rows})
        assert max(counts) < 12
        assert len(counts) == len({r["type_key"] for r in rows}), "不同题型题量不应重复"
        assert sum(1 for r in rows if r["compatibility_only"]) == 2
        for row in rows:
            payload = row["payload"]
            assert payload["evidence_locator"], "内部台账必须保留定位"
            for forbidden in ("chapter", "page ", "第", "chapitre", "tabla"):
                assert forbidden not in payload["text"].lower()
        break
    else:
        pytest.fail("没有文档产出完整 10 条 query")
    assert fake_llm.calls.count("query") >= 1
    assert os.path.isfile(os.path.join(svc.workspace_dir, "query_evidence.jsonl"))


def test_scenario_7_source_modification_is_refused_by_code():
    adapter = LarkCliAdapter("lark-cli", runner=lambda *a, **k: None, inter_call_delay=0.0)
    write_attempts = [
        ["drive", "+delete", "--file-token", "X"],
        ["drive", "+move", "--file-token", "X", "--type", "folder", "--token", "Y"],
        ["drive", "+update-title", "--token", "X", "--title", "改名"],
        ["drive", "+upload", "--file", "local.pdf"],
        ["drive", "+create-folder", "--name", "新目录"],
        ["drive", "+create-shortcut", "--file-token", "X", "--folder-token", "Y"],
        ["drive", "+secure-label-update", "--token", "X"],
        ["drive", "+copy", "--file-token", "X", "--folder-token", "Y"],
        ["docs", "+update", "--doc", "X"],
    ]
    for args in write_attempts:
        with pytest.raises(SafetyViolation):
            adapter._invoke(args)


def test_scenario_7_adapter_protocol_has_no_write_methods():
    """从接口层面杜绝写操作：协议里根本没有写方法。"""
    protocol_methods = {name for name in dir(SourceAdapter)
                        if not name.startswith("_") and name != "name"}
    assert protocol_methods == {"list_folder", "inspect", "download_file", "export_online"}
    for name in protocol_methods:
        assert not any(word in name for word in ("delete", "move", "rename", "update",
                                                 "upload", "create", "patch", "write"))
