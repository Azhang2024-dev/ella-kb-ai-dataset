"""验收门禁自检（计划第 9 节的 5 个检查点，机械化执行）。

为什么要做成命令而不是人工看报告：
「硬失败零误放行」「可以复跑」这两条必须每次交付前都能重新证明一遍，
人工看 CSV 既慢又容易漏。这里把 5 个检查点写成可重复执行的断言，
输出 PASS/FAIL 明细，任何一条 FAIL 都返回非零退出码。

检查点 3 里"源文件未被修改"无法从本地状态反证（我们没有写权限，也没有改动记录），
所以它检查的是**结构性保证**：策略声明只读 + 白名单只含只读命令 +
适配器协议里不存在写方法。这三条一起构成"不可能发生"，比事后比对更可靠。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from .config import CollectionPolicy
from .contracts import ITEM_ACCEPTED, ITEM_REJECTED, deciding_reason
from .state import StateStore
from . import winpath

MB = 1024 * 1024


@dataclass
class AuditCheck:
    checkpoint: str
    name: str
    passed: bool
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"checkpoint": self.checkpoint, "name": self.name,
                "passed": self.passed, "detail": self.detail, "evidence": self.evidence}


@dataclass
class AuditReport:
    run_id: str
    generated_at: str
    checks: list[AuditCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed(self) -> list[AuditCheck]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "generated_at": self.generated_at,
            "passed": self.passed,
            "total_checks": len(self.checks),
            "failed_checks": len(self.failed),
            "checks": [c.to_dict() for c in self.checks],
        }


def _adapter_protocol_has_no_write_methods() -> tuple[bool, list[str]]:
    from .adapters.base import SourceAdapter

    names = [n for n in dir(SourceAdapter) if not n.startswith("_") and n != "name"]
    bad = [n for n in names if any(w in n.lower() for w in
           ("delete", "move", "rename", "update", "upload", "create", "patch", "write"))]
    return (not bad), names


def audit_run(*, policy: CollectionPolicy, store: StateStore, workspace_dir: str,
              run_id: str, compare_run_dir: str | None = None,
              skill_dir: str | None = None) -> AuditReport:
    report = AuditReport(run_id=run_id, generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"))

    # ---------------- 检查点 1：能运行 ----------------
    manifest_ok, manifest_detail = _check_manifest(skill_dir)
    report.checks.append(AuditCheck("1-能运行", "capability manifest 与契约一致",
                                    manifest_ok, manifest_detail))
    no_write, methods = _adapter_protocol_has_no_write_methods()
    report.checks.append(AuditCheck(
        "1-能运行", "适配器协议不含任何写方法", no_write,
        f"协议方法：{methods}", {"methods": methods}))

    # ---------------- 检查点 2：盘点完整 ----------------
    items = store.list_items()
    manifest_path = os.path.join(workspace_dir, "run_manifest.json")
    inv_manifest: dict[str, Any] = {}
    if os.path.isfile(manifest_path):
        try:
            inv_manifest = json.load(open(manifest_path, encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            inv_manifest = {}
    inv_report = (inv_manifest.get("report") or {})
    inv_errors = inv_report.get("errors") or []
    paging_gaps = [e for e in inv_errors if "next_page_token" in str(e.get("message", ""))]
    report.checks.append(AuditCheck(
        "2-盘点完整", "没有 has_more=true 却缺 next_page_token 的漏项风险",
        not paging_gaps, f"漏项风险 {len(paging_gaps)} 处",
        {"count": len(paging_gaps), "samples": paging_gaps[:5]}))

    sources_seen = {str(i.get("source_name")) for i in items}
    expected_sources = {str(s["name"]) for s in policy.sources}
    # 只有在跑过云盘 inventory 的情况下才要求"两个源都已盘点"。
    # 本地优先链路（ingest --dir）压根没有云盘盘点这一步，源名是用户给的本地文件夹名，
    # 硬套这条断言会把推荐的本地入口误判成不合格（实测踩过）。
    cloud_inventory = os.path.isfile(os.path.join(workspace_dir, "source_inventory.jsonl"))
    if cloud_inventory:
        report.checks.append(AuditCheck(
            "2-盘点完整", "两个源文件夹都已盘点且用真实名称记录",
            expected_sources <= sources_seen,
            f"期望 {sorted(expected_sources)}，实际 {sorted(sources_seen)}"))
    else:
        report.checks.append(AuditCheck(
            "2-盘点完整", "本地 ingest 链路：条目均带来源名（无云盘盘点，跳过两源断言）",
            not {s for s in sources_seen if s in ("None", "", "unknown")},
            f"来源名集合：{sorted(sources_seen)}"))

    missing_name = [i["item_id"] for i in items if not i.get("source_name")]
    report.checks.append(AuditCheck(
        "2-盘点完整", "每条记录都有源文件夹名称", not missing_name,
        f"缺失 {len(missing_name)} 条", {"samples": missing_name[:5]}))

    # ---------------- 检查点 3：没有误放行 ----------------
    accepted_ids: list[str] = []
    decisions = store.latest_decisions("screen")
    for item_id, decision in decisions.items():
        if decision.get("status") == ITEM_ACCEPTED:
            accepted_ids.append(item_id)

    oversize: list[dict[str, Any]] = []
    bad_ext: list[dict[str, Any]] = []
    hard_override: list[dict[str, Any]] = []
    items_by_id = {str(i["item_id"]): i for i in items}
    acquire_decisions = store.latest_decisions("acquire")
    for item_id in accepted_ids:
        item = items_by_id.get(item_id, {})
        artifact = store.get_artifact(item_id) or {}
        ext = str(item.get("extension") or "").lower()
        size = artifact.get("size_bytes")
        limit_mb = policy.size_limit_mb(ext)
        if limit_mb is None:
            bad_ext.append({"item_id": item_id, "reason": "size_limit_not_configured",
                            "extension": ext})
        elif size is not None and int(size) > limit_mb * MB:
            oversize.append({"item_id": item_id, "size_bytes": int(size),
                             "limit_mb": limit_mb, "extension": ext})
        if ext and ext not in policy.allowed_extensions:
            bad_ext.append({"item_id": item_id, "extension": ext,
                            "reason": "extension_not_allowed"})
        acq = acquire_decisions.get(item_id)
        if acq and acq.get("status") == ITEM_REJECTED:
            hard_override.append({"item_id": item_id,
                                  "acquire_reason_codes": acq.get("reason_codes")})

    report.checks.append(AuditCheck(
        "3-没有误放行", "没有超大小文件被接受", not oversize,
        f"违规 {len(oversize)} 条", {"violations": oversize[:10]}))
    report.checks.append(AuditCheck(
        "3-没有误放行", "没有不支持格式被接受", not bad_ext,
        f"违规 {len(bad_ext)} 条", {"violations": bad_ext[:10]}))
    report.checks.append(AuditCheck(
        "3-没有误放行", "采集阶段的硬拒绝没有被筛选阶段翻案", not hard_override,
        f"违规 {len(hard_override)} 条", {"violations": hard_override[:10]}))

    # 精确重复不能重复计入 accepted
    dup_accepted: list[dict[str, Any]] = []
    groups = store.dedup_groups()
    for key, members in groups.items():
        accepted_in_group = [m["item_id"] for m in members if m["item_id"] in set(accepted_ids)]
        if len(accepted_in_group) > 1:
            dup_accepted.append({"group": key, "accepted": accepted_in_group})
    report.checks.append(AuditCheck(
        "3-没有误放行", "同一去重组里没有多条被接受", not dup_accepted,
        f"违规 {len(dup_accepted)} 组", {"violations": dup_accepted[:10]}))

    sha_accepted = [(i, (store.get_artifact(i) or {}).get("sha256")) for i in accepted_ids]
    sha_values = [s for _i, s in sha_accepted if s]
    dup_sha = len(sha_values) - len(set(sha_values))
    report.checks.append(AuditCheck(
        "3-没有误放行", "被接受的文档没有相同 SHA-256", dup_sha == 0,
        f"重复 {dup_sha} 条", {"accepted": len(sha_values), "unique": len(set(sha_values))}))

    readonly_ok = (policy.source_readonly
                   and all(s.get("readonly") is True for s in policy.sources)
                   and bool(policy.allowlisted_lark_commands)
                   and not any(w in " ".join(policy.allowlisted_lark_commands)
                               for w in ("delete", "move", "update-title", "upload",
                                         "create-folder", "create-shortcut")))
    report.checks.append(AuditCheck(
        "3-没有误放行", "云盘只读得到结构性保证（策略声明 + 只读白名单 + 协议无写方法）",
        readonly_ok and no_write,
        f"白名单：{list(policy.allowlisted_lark_commands)}"))

    # ---------------- 检查点 4：结果可解释 ----------------
    unexplainable: list[dict[str, Any]] = []
    for item_id, decision in decisions.items():
        item = items_by_id.get(item_id, {})
        artifact = store.get_artifact(item_id) or {}
        meta = item.get("metadata") or {}
        source_path = f"{item.get('source_name')}/{meta.get('relative_path') or item.get('name')}"
        codes = decision.get("reason_codes") or []
        problems = []
        if not decision.get("status"):
            problems.append("no_status")
        if not codes:
            problems.append("no_reason_codes")
        if not source_path or source_path.startswith("None/"):
            problems.append("no_source_path")
        if decision.get("status") == ITEM_ACCEPTED and not artifact.get("path"):
            problems.append("accepted_without_artifact_path")
        if problems:
            unexplainable.append({"item_id": item_id, "problems": problems})
    report.checks.append(AuditCheck(
        "4-结果可解释", "每条筛选结论都有 status / reason_codes / source_path / artifact_path",
        not unexplainable, f"不合格 {len(unexplainable)} 条",
        {"samples": unexplainable[:10], "screened": len(decisions)}))

    no_deciding = [i for i, d in decisions.items()
                   if d.get("status") in (ITEM_ACCEPTED, ITEM_REJECTED)
                   and deciding_reason(d.get("reason_codes") or []) is None
                   and d.get("status") == ITEM_REJECTED]
    report.checks.append(AuditCheck(
        "4-结果可解释", "每条淘汰都能给出单一决定原因", not no_deciding,
        f"缺失 {len(no_deciding)} 条", {"samples": no_deciding[:10]}))

    # ---------------- 检查点 5：可以复跑 ----------------
    if compare_run_dir:
        report.checks.extend(_compare_runs(store, workspace_dir, compare_run_dir))
    else:
        report.checks.append(AuditCheck(
            "5-可以复跑", "与另一次运行对比（未提供 --compare-with，跳过）", True,
            "离线一致性由 pytest test_pipeline_is_replayable_with_identical_decisions 覆盖"))

    return report


def _check_manifest(skill_dir: str | None) -> tuple[bool, str]:
    from . import contracts as C

    if not skill_dir:
        skill_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "skills", "document_collection")
    path = os.path.join(skill_dir, "capability-manifest.json")
    if not os.path.isfile(path):
        return False, f"找不到 manifest：{path}"
    try:
        manifest = json.load(open(path, encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return False, f"manifest 不是合法 JSON：{exc}"
    names = [c.get("name") for c in manifest.get("capabilities", [])]
    if names != list(C.CAPABILITY_NAMES):
        return False, f"能力名或顺序与契约不一致：{names}"
    for entry, contract in zip(manifest["capabilities"], C.CAPABILITIES):
        if entry.get("cli_command") != contract["cli_command"]:
            return False, f"{entry.get('name')} 的 cli_command 不一致"
        if entry.get("future_mcp_tool") != contract["future_mcp_tool"]:
            return False, f"{entry.get('name')} 的 future_mcp_tool 不一致"
        if not entry.get("side_effects"):
            return False, f"{entry.get('name')} 没有声明副作用"
    skill_md = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(skill_md):
        return False, f"找不到 SKILL.md：{skill_md}"
    text = open(skill_md, encoding="utf-8").read()
    if not text.startswith("---\n"):
        return False, "SKILL.md 缺少 YAML frontmatter"
    missing = [n for n in C.CAPABILITY_NAMES if n not in text]
    if missing:
        return False, f"SKILL.md 缺少能力名：{missing}"
    return True, f"manifest {len(names)} 个能力与契约逐项一致，SKILL.md 齐备"


# 这些阶段的结论完全由确定性规则得出，两次运行必须逐条一致。
DETERMINISTIC_STAGES = ("acquire", "extract", "deduplicate")


def _semantic_enabled(store: StateStore, run_id: str) -> bool:
    """这次运行的筛选阶段是否启用了语义复核。

    注意 stage_state_json 的键是**能力全名**（collection.screen），
    不是短阶段名（screen）—— 第一版实现看错了键，导致启用模型的运行
    被当成未启用，从而错误地强制要求筛选决策逐字一致。
    """
    row = store.get_run(run_id) or {}
    try:
        stages = json.loads(row.get("stage_state_json") or "{}")
    except json.JSONDecodeError:
        return False
    for key in ("collection.screen", "screen"):
        block = stages.get(key)
        if isinstance(block, dict) and "semantic_review_enabled" in block:
            return bool(block["semantic_review_enabled"])
    return False


def _inventory_counts(workspace_dir: str) -> dict[str, Any] | None:
    path = os.path.join(workspace_dir, "run_manifest.json")
    if not os.path.isfile(path):
        return None
    try:
        manifest = json.load(open(path, encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return None
    counts = ((manifest.get("report") or {}).get("counts")) or {}
    return {k: counts.get(k) for k in ("raw_occurrences", "unique_items",
                                       "duplicate_occurrences",
                                       "cross_source_duplicate_tokens")}


def _compare_runs(store: StateStore, workspace_dir: str,
                  compare_dir: str) -> list[AuditCheck]:
    """与另一次运行对比，验证「可以复跑」。

    必须区分两类阶段：
    - `acquire / extract / deduplicate` 完全由确定性规则得出，两次运行必须逐条一致；
    - `screen` 在启用语义复核时依赖模型输出，**不保证逐字一致**。
      把模型结果也纳入"必须一致"会让这条检查点永远失败，
      从而掩盖真正的确定性回归；所以只在两次都没启用模型时才强制比对。
    """
    other_db = os.path.join(compare_dir, "state.sqlite3")
    if not os.path.isfile(other_db):
        return [AuditCheck("5-可以复跑", f"对比运行存在（{compare_dir}）", False,
                           "找不到对比运行的 state.sqlite3")]
    other = StateStore(other_db)
    try:
        other_run_ids = [r["run_id"] for r in other.list_runs()]
        other_run_id = other_run_ids[0] if other_run_ids else ""

        def stage_decisions(target: StateStore, stages: Sequence[str]) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for stage in stages:
                out[stage] = {
                    item_id: (value["status"], sorted(value["reason_codes"]))
                    for item_id, value in target.latest_decisions(stage).items()
                }
            return out

        def split_hashes(target: StateStore) -> tuple[dict[str, str], dict[str, str]]:
            """把产物按获取方式分开：download 必须逐字节可复现，export 不一定。"""
            downloaded: dict[str, str] = {}
            exported: dict[str, str] = {}
            for row in target.list_artifacts():
                if not row.get("sha256"):
                    continue
                bucket = exported if str(row.get("acquire_mode")) == "export" else downloaded
                bucket[str(row["item_id"])] = str(row["sha256"])
            return downloaded, exported

        my_downloaded, my_exported = split_hashes(store)
        their_downloaded, their_exported = split_hashes(other)
        my_counts = store.count_items()
        their_counts = other.count_items()
        my_det = stage_decisions(store, DETERMINISTIC_STAGES)
        their_det = stage_decisions(other, DETERMINISTIC_STAGES)

        export_diff = sorted(k for k in my_exported
                             if their_exported.get(k) != my_exported[k])
        checks = [
            AuditCheck("5-可以复跑", "下载类产物的 SHA-256 逐字节一致",
                       my_downloaded == their_downloaded,
                       f"本次 {len(my_downloaded)} 个 / 对比 {len(their_downloaded)} 个"),
            # 实测：drive +export 每次都由服务端重新渲染，字节数会差几个 byte，
            # 因此在线资源的导出产物**不具备字节级可复现性**。
            # 这一条只做披露不做判失败，否则检查点 5 会永远红，
            # 反而掩盖真正的确定性回归；正文级去重用的是规范化文本指纹，不受影响。
            AuditCheck("5-可以复跑",
                       "导出类产物字节不稳定属已知事实（服务端每次重新渲染，仅披露）",
                       True,
                       f"本次 {len(my_exported)} 个 / 对比 {len(their_exported)} 个，"
                       f"其中 {len(export_diff)} 个 SHA-256 不同",
                       {"differing_item_ids": export_diff[:10]}),
            AuditCheck("5-可以复跑", "两次运行的条目状态计数一致",
                       my_counts == their_counts,
                       f"本次 {my_counts} / 对比 {their_counts}"),
            AuditCheck("5-可以复跑",
                       "确定性阶段决策逐条一致（acquire / extract / deduplicate）",
                       my_det == their_det,
                       f"本次 {sum(len(v) for v in my_det.values())} 条 / "
                       f"对比 {sum(len(v) for v in their_det.values())} 条"),
        ]

        my_inv = _inventory_counts(workspace_dir)
        their_inv = _inventory_counts(compare_dir)
        if my_inv and their_inv:
            checks.append(AuditCheck("5-可以复跑", "两次运行的盘点计数一致",
                                     my_inv == their_inv,
                                     f"本次 {my_inv} / 对比 {their_inv}"))

        my_semantic = _semantic_enabled(store, store_run_id(store))
        their_semantic = _semantic_enabled(other, other_run_id)
        if my_semantic or their_semantic:
            my_screen = stage_decisions(store, ("screen",))["screen"]
            their_screen = stage_decisions(other, ("screen",))["screen"]
            same = sum(1 for k in my_screen if my_screen.get(k) == their_screen.get(k))
            checks.append(AuditCheck(
                "5-可以复跑",
                "筛选决策可比性（启用了语义复核，模型输出不保证逐字一致，故不强制相等）",
                True,
                f"本次语义复核={my_semantic}，对比={their_semantic}；"
                f"筛选决策 {len(my_screen)} 条中 {same} 条完全相同"))
        else:
            my_screen = stage_decisions(store, ("screen",))
            their_screen = stage_decisions(other, ("screen",))
            checks.append(AuditCheck("5-可以复跑", "筛选决策一致（两次都未启用模型）",
                                     my_screen == their_screen,
                                     f"本次 {len(my_screen['screen'])} 条 / "
                                     f"对比 {len(their_screen['screen'])} 条"))
        return checks
    finally:
        other.close()


def store_run_id(store: StateStore) -> str:
    runs = store.list_runs()
    return runs[0]["run_id"] if runs else ""


def write_audit_report(path: str, report: AuditReport) -> None:
    winpath.makedirs(os.path.dirname(os.path.abspath(path)))
    with winpath.open_write(path, encoding="utf-8") as fh:
        json.dump(report.to_dict(), fh, ensure_ascii=False, indent=2, default=str)


def format_audit_report(report: AuditReport) -> str:
    lines = [f"# 验收门禁自检 run_id={report.run_id}",
             f"生成时间：{report.generated_at}",
             f"结论：{'PASS' if report.passed else 'FAIL'}"
             f"（{len(report.checks) - len(report.failed)}/{len(report.checks)} 通过）", ""]
    current = None
    for check in report.checks:
        if check.checkpoint != current:
            current = check.checkpoint
            lines.append(f"## 检查点 {current}")
        mark = "✅" if check.passed else "❌"
        lines.append(f"- {mark} {check.name}")
        if check.detail:
            lines.append(f"      {check.detail}")
        if not check.passed and check.evidence:
            lines.append(f"      evidence: {json.dumps(check.evidence, ensure_ascii=False, default=str)[:600]}")
    lines.append("")
    return "\n".join(lines)
