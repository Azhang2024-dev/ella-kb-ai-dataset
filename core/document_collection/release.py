"""人工放行（human release）：把 needs_review 的条目改判为 accepted。

为什么需要这个能力：

    screen 的设计原则之一是"证据不足就交人工，不误杀也不误放"
    （教育属性 unknown / 可出题性 medium 一律落 needs_review）。
    但流水线此前只**产出** human_review_queue.csv，没有**回填人工结论**的入口——
    人工看完了却无法把结论落库，只能改状态库 SQLite，那正是"绕过审计"的做法。

本能力把人工结论做成**一次可审计的决定**，而不是一次 UPDATE：

  1. 只允许 `needs_review → accepted`，其余一律拒收（不覆盖任何已有结论）；
  2. 逐条复核硬约束（采集期硬拒绝不翻案 / 语种在范围内 / 学科在 P0∪P1 /
     与已 accepted 文档不重复），任一不过就拒收并给出原因；
  3. 决策沿用 `add_decision` **追加**写入，历史决定一条不删；
  4. 原 needs_review 决策的 evidence 原样继承，再追加放行元信息，
     保证 deliver 的 `_subject_of()` 仍能取到 subject。

这样 audit 的"没有误放行""结果可解释"两个检查点都能继续成立。
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .config import CollectionPolicy
from .contracts import (
    ITEM_ACCEPTED,
    ITEM_NEEDS_REVIEW,
    ITEM_REJECTED,
    RC_HUMAN_RELEASED,
    RC_OK,
)
from .state import StateStore
from . import winpath

LogFn = Callable[[str], None]

_EXT_RE = re.compile(r"\.(pdf|md|docx|pptx|txt|csv|xlsx)$", re.I)


def _noop(msg: str) -> None:  # pragma: no cover
    pass


def _stem(name: str) -> str:
    return _EXT_RE.sub("", os.path.basename(name or "")).lower()


def _text_fingerprint(text: str) -> str:
    """正文指纹：归一化空白后取 sha256。用于识别"换名重复入库"的同一份文档。"""
    return hashlib.sha256(re.sub(r"\s+", "", text or "").encode("utf-8")).hexdigest()


@dataclass
class ReleaseOptions:
    """放行参数。item_ids 为空时不做任何事（这是个定向动作，绝不批量自动放行）。"""

    item_ids: Sequence[str] = ()
    reason: str = ""
    released_by: str = "human"
    dry_run: bool = False
    # 允许的语种/学科缺省取策略；显式传入用于定向场景
    allowed_languages: Sequence[str] = ()
    allowed_subjects: Sequence[str] = ()


def _load_text(item_id: str, workspace_dir: str, extraction: Mapping[str, Any] | None) -> str:
    """优先读 extraction.text_path，其次回落到 <workspace>/extracted/<item_id>.txt。"""
    candidates = [(extraction or {}).get("text_path"),
                  os.path.join(workspace_dir, "extracted", f"{item_id}.txt")]
    for path in candidates:
        if path and winpath.isfile(path):
            try:
                with winpath.open_read(path, encoding="utf-8-sig") as fh:
                    return fh.read()
            except OSError:
                continue
    return ""


def run_release(*, policy: CollectionPolicy, store: StateStore, workspace_dir: str,
                run_id: str, options: ReleaseOptions | None = None,
                log: LogFn = _noop) -> dict[str, Any]:
    """把人工确认过的 needs_review 条目放行为 accepted。逐条校验，不通过就拒收。"""
    opts = options or ReleaseOptions()
    languages = {str(x).lower() for x in (opts.allowed_languages or policy.target_languages)}
    subjects = {str(x).lower() for x in (opts.allowed_subjects
                                         or policy.target_subjects_for_hint)}
    if not opts.item_ids:
        log("[release] 未指定 item_id（这是个定向动作，不批量放行）")
        return {"total_requested": 0, "released_total": 0, "rejected_total": 0,
                "skipped_total": 0, "dry_run": bool(opts.dry_run),
                "released": [], "rejected": [], "skipped": []}

    screen_decisions = store.latest_decisions("screen")
    acquire_decisions = store.latest_decisions("acquire")
    items = {str(i["item_id"]): i for i in store.list_items()}

    # 已 accepted 文档的唯一性基线：按 (语种, stem) 与 (语种, 正文指纹) 记集合，
    # 防止"同一份文档的 md/pdf 副本"或"换名重复入库"被当作新文档放进来。
    used_stem: set[tuple[str, str]] = set()
    used_tfp: set[tuple[str, str]] = set()
    for item_id, dec in screen_decisions.items():
        if str(dec.get("status")) != ITEM_ACCEPTED:
            continue
        item = items.get(item_id) or {}
        extraction = store.get_extraction(item_id)
        lang = str((extraction or {}).get("language") or "unknown").lower()
        used_stem.add((lang, _stem(str(item.get("name") or ""))))
        text = _load_text(item_id, workspace_dir, extraction)
        if text:
            used_tfp.add((lang, _text_fingerprint(text)))

    released: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for item_id in opts.item_ids:
        item = items.get(item_id)
        if not item:
            rejected.append({"item_id": item_id, "reason": "no_item"})
            continue
        dec = screen_decisions.get(item_id)
        status = str((dec or {}).get("status") or "none")
        if status != ITEM_NEEDS_REVIEW:
            # 已是 accepted / rejected / 无 screen 结论：不动，避免覆盖历史结论。
            skipped.append({"item_id": item_id, "reason": f"status_is_{status}"})
            continue

        extraction = store.get_extraction(item_id)
        artifact = store.get_artifact(item_id) or {}
        name = str(item.get("name") or "")
        lang = str((extraction or {}).get("language") or "unknown").lower()
        evidence = dict((dec or {}).get("evidence") or {})
        subject = str(evidence.get("subject") or evidence.get("subject_hint")
                      or "unknown").lower()

        problems: list[str] = []
        # 1) 采集阶段的硬拒绝不得被筛选阶段翻案（audit 检查点 3）
        acq = acquire_decisions.get(item_id) or {}
        if str(acq.get("status")) == ITEM_REJECTED:
            problems.append("acquire_stage_already_rejected")
        # 2) 语种须在范围内
        if lang not in languages:
            problems.append(f"language_not_in_scope:{lang}")
        # 3) 学科须在 P0∪P1 内（放行不能把范围外学科塞进交付）
        if subject not in subjects:
            problems.append(f"subject_out_of_scope:{subject}")
        # 4) 必须有本地产物，否则交付拿不到正文
        if not artifact.get("path") or not winpath.exists(str(artifact.get("path") or "")):
            problems.append("no_local_artifact")
        # 5) 唯一性：与本 run 已 accepted 的文档不得同源或同文
        key_stem = (lang, _stem(name))
        if key_stem in used_stem:
            problems.append("duplicate_stem_with_accepted")
        text = _load_text(item_id, workspace_dir, extraction)
        tfp = _text_fingerprint(text) if text else ""
        if tfp and (lang, tfp) in used_tfp:
            problems.append("duplicate_content_with_accepted")

        if problems:
            rejected.append({"item_id": item_id, "name": name, "reason_codes": problems})
            continue

        # 继承原 evidence，追加放行元信息（不删任何既有键）
        new_evidence = dict(evidence)
        new_evidence.update({
            "human_released": True,
            "released_by": str(opts.released_by or "human"),
            "released_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "previous_status": ITEM_NEEDS_REVIEW,
            "previous_reason_codes": list((dec or {}).get("reason_codes") or []),
            "release_note": str(opts.reason or ""),
            "run_id": run_id,
        })
        reason_codes = [RC_HUMAN_RELEASED, RC_OK]
        if opts.reason:
            reason_codes.append(f"release_note={opts.reason}")

        if not opts.dry_run:
            store.add_decision(item_id, "screen", ITEM_ACCEPTED, reason_codes, new_evidence)
            store.set_item_stage(item_id, ITEM_ACCEPTED, "screen")
            # 放行后立刻占位，避免同批次里重复放行同一份文档
            used_stem.add(key_stem)
            if tfp:
                used_tfp.add((lang, tfp))

        released.append({
            "item_id": item_id, "name": name, "language": lang, "subject": subject,
            "reason_codes": reason_codes,
        })

    if not opts.dry_run:
        store.commit()

    summary = {
        "total_requested": len(opts.item_ids),
        "released_total": len(released),
        "rejected_total": len(rejected),
        "skipped_total": len(skipped),
        "dry_run": bool(opts.dry_run),
        "released": released,
        "rejected": rejected,
        "skipped": skipped,
    }
    log(f"[release] 请求 {summary['total_requested']} 条："
        f"放行 {summary['released_total']} / 拒收 {summary['rejected_total']} / "
        f"跳过 {summary['skipped_total']}"
        + ("（dry-run，未落库）" if opts.dry_run else ""))
    return summary