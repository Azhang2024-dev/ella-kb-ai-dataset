"""Task 5（前半）+ Task 6：硬筛选与语义复核。

硬规则顺序（计划 Task 5，与需求第二/三章一一对应）：

    采集成功
      -> 文件类型支持
      -> 文件大小合格
      -> 内容抽取成功
      -> 语言基本符合
      -> 非图形主导
      -> 非精确重复

三条不可让步的原则：
1. **硬失败不能被 LLM 覆盖**：大小超限、格式不支持、图形主导、精确重复，
   一旦命中就直接 rejected，语义复核根本不会被调用；
2. **没有证据的判断不能自动接受**：LLM 必须给出 evidence，否则转 needs_review；
3. **超过 5 万字不淘汰**：只打 requires_full_text_review 标记（需求 2.2 / 默认口径 1）。
"""

from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .config import CollectionPolicy
from .contracts import (
    deciding_reason,
    ITEM_ACCEPTED,
    ITEM_ACQUIRE_FAILED,
    ITEM_CONVERSION_REQUIRED,
    ITEM_NEEDS_REVIEW,
    ITEM_PENDING_EXPORT,
    ITEM_REJECTED,
    RC_ACQUIRE_FAILED,
    RC_CONVERSION_REQUIRED,
    RC_DUP_EXACT,
    RC_DUP_NORMALIZED,
    RC_EDU_LEVEL,
    RC_EXTRACT_EMPTY,
    RC_EXT_SUPPORTED,
    RC_EXT_UNSUPPORTED,
    RC_LANG_SUPPORTED,
    RC_LANG_UNSUPPORTED,
    RC_LLM_BAD_OUTPUT,
    RC_LLM_BUDGET_EXHAUSTED,
    RC_LLM_UNAVAILABLE,
    RC_MIME_GRAPHICS_DOMINATED,
    RC_OK,
    RC_PENDING_EXPORT,
    RC_QUERYABILITY,
    RC_SIZE_OVER_LIMIT,
    RC_SIZE_WITHIN_LIMIT,
    RC_SUBJECT,
    RC_SUBJECT_OUT_OF_SCOPE,
)
from .state import StateStore
from .parallel import map_ordered
from .metadata import metadata_hints
from . import winpath
from .textstats import is_graphics_dominated, is_text_too_sparse, truncate_for_prompt

LogFn = Callable[[str], None]


def _noop(msg: str) -> None:  # pragma: no cover
    pass


def _processed_item_ids(store: StateStore) -> set[str]:
    """真正进入过采集/抽取阶段的条目集合。

    判据用"有没有本地产物或抽取记录"，而不是 status：
    第一次真实 Pilot 时 screen 跑过全量 4892 条，把从未采集的条目状态
    也改写成了 needs_review，用 status 判断会失效。证据才是可靠依据。
    """
    ids = {str(row["item_id"]) for row in store.list_artifacts()}
    ids |= {str(row["item_id"]) for row in store.list_extractions()}
    return ids


@dataclass
class ScreeningOptions:
    limit: int | None = None
    include_statuses: tuple[str, ...] = ()
    include_unprocessed: bool = False
    run_semantic: bool = False
    concurrency: int = 1
    # None = 用策略里的 languages.min_confidence_for_accept（默认 0.70）
    language_confidence_threshold: float | None = None
    semantic_confidence_threshold: float = 0.55
    accepted_education_levels: tuple[str, ...] = ("university",)
    review_education_levels: tuple[str, ...] = ("vocational", "unknown")
    rejected_education_levels: tuple[str, ...] = ("school", "general")
    llm_text_sample_chars: int = 12000
    # 学科范围门禁：subject 必须落在 allowed_subjects 内。默认由 run_screen 用
    # 策略的 P0∪P1 集合填充；显式置 False 可关闭（旧批次对照用）。
    subject_gate: bool = True
    allowed_subjects: tuple[str, ...] = ()
    subject_unknown_action: str = "needs_review"


def _size_limit(policy: CollectionPolicy, ext: str | None) -> int | None:
    return policy.size_limit_mb(ext)


def hard_screen(*, item: Mapping[str, Any], artifact: Mapping[str, Any] | None,
                extraction: Mapping[str, Any] | None,
                dedup_decision: Mapping[str, Any] | None,
                acquire_decision: Mapping[str, Any] | None = None,
                policy: CollectionPolicy, opts: ScreeningOptions) -> dict[str, Any]:
    """只做确定性判断，返回 {status, reason_codes, evidence, hard_failed}。"""
    reasons: list[str] = []
    evidence: dict[str, Any] = {}
    # 确定性元数据兜底（P7/D3/D4）：不依赖模型，从文件名+标题关键词推学科/教育属性。
    # 只作提示；模型判定（semantic_review）会覆盖它。这里零文件 IO，条目生成也不额外读盘。
    hint_name = f"{item.get('name') or ''} {(item.get('metadata') or {}).get('relative_path') or ''}"
    hints = metadata_hints(hint_name, None,
                           allowed_subjects=policy.target_subjects_for_hint)
    evidence.update(hints)
    ext = (item.get("extension") or "").lower()
    item_status = str(item.get("status") or "")
    size_bytes = (artifact or {}).get("size_bytes")
    artifact_status = str((artifact or {}).get("status") or "")

    # 0) 采集阶段已经硬拒绝的，直接沿用，不允许在本阶段被重新放行。
    #    典型场景：超限文件的本地 blob 已被删除，artifact.path 为空，
    #    如果只看 path 会误判成 "artifact_missing" -> needs_review，等于放软了硬门槛。
    if acquire_decision and str(acquire_decision.get("status")) == ITEM_REJECTED:
        codes = [str(c) for c in (acquire_decision.get("reason_codes") or [])]
        evidence.update({"acquire_evidence": dict(acquire_decision.get("evidence") or {})})
        return _hard(ITEM_REJECTED, reasons + (codes or [RC_ACQUIRE_FAILED]),
                     evidence, hard_failed=True)

    # 1) 采集成功
    if artifact_status in (ITEM_PENDING_EXPORT,):
        return _hard(ITEM_PENDING_EXPORT, reasons + [RC_PENDING_EXPORT], evidence, hard_failed=False)
    if artifact_status in (ITEM_CONVERSION_REQUIRED,):
        return _hard(ITEM_CONVERSION_REQUIRED, reasons + [RC_CONVERSION_REQUIRED], evidence, hard_failed=False)
    if artifact_status in (ITEM_ACQUIRE_FAILED,) or item_status == ITEM_ACQUIRE_FAILED:
        return _hard(ITEM_REJECTED, reasons + [RC_ACQUIRE_FAILED],
                     {"error": (artifact or {}).get("error")}, hard_failed=True)
    if not artifact or not (artifact or {}).get("path"):
        return _hard(ITEM_NEEDS_REVIEW, reasons + ["artifact_missing"], evidence, hard_failed=False)

    # 2) 文件类型支持
    if ext in policy.conversion_required_extensions:
        return _hard(ITEM_CONVERSION_REQUIRED, reasons + [RC_CONVERSION_REQUIRED], evidence, hard_failed=False)
    if ext not in policy.allowed_extensions:
        return _hard(ITEM_REJECTED, reasons + [RC_EXT_UNSUPPORTED], {"extension": ext}, hard_failed=True)
    reasons.append(RC_EXT_SUPPORTED)

    # 3) 文件大小合格
    limit_mb = _size_limit(policy, ext)
    if limit_mb is None:
        return _hard(ITEM_NEEDS_REVIEW, reasons + ["size_limit_not_configured"], evidence, hard_failed=False)
    if size_bytes is None:
        return _hard(ITEM_NEEDS_REVIEW, reasons + ["size_unknown"], evidence, hard_failed=False)
    if int(size_bytes) > limit_mb * 1024 * 1024:
        evidence["size_bytes"] = int(size_bytes)
        evidence["limit_mb"] = limit_mb
        return _hard(ITEM_REJECTED, reasons + [RC_SIZE_OVER_LIMIT], evidence, hard_failed=True)
    reasons.append(RC_SIZE_WITHIN_LIMIT)
    evidence["size_bytes"] = int(size_bytes)
    evidence["limit_mb"] = limit_mb

    # 4) 内容抽取成功
    ex_status = str((extraction or {}).get("status") or "")
    if ex_status in ("failed",):
        return _hard(ITEM_NEEDS_REVIEW, reasons + ["extract_failed"],
                     {"error": (extraction or {}).get("error")}, hard_failed=False)

    char_count = int((extraction or {}).get("char_count") or 0)
    page_count = (extraction or {}).get("page_count")
    image_area_ratio = (extraction or {}).get("image_area_ratio")
    language = str((extraction or {}).get("language") or "unknown")
    confidence = float((extraction or {}).get("language_confidence") or 0.0)
    evidence.update({
        "char_count": char_count,
        "page_count": page_count,
        "image_area_ratio": image_area_ratio,
        "language": language,
        "language_confidence": confidence,
        "extractor": (extraction or {}).get("extractor"),
        "mime_verdict": (extraction or {}).get("mime_verdict"),
    })

    # 4b) 正文为空 -> 先判图形主导，再看是否只是解析问题。
    #     顺序很重要：空正文时语言必然是 unknown，如果先跑语言规则，
    #     真正的淘汰原因（图形主导）就会被 "language_ambiguous" 掩盖成待复核。
    content_rules = policy.content_rules
    if char_count <= 0:
        graphics, graphics_reasons = is_graphics_dominated(
            char_count=0,
            page_count=int(page_count) if page_count else None,
            image_area_ratio=float(image_area_ratio) if image_area_ratio is not None else None,
            min_chars=int(content_rules.get("min_chars_for_query_generation", 800)),
        )
        evidence["graphics_reasons"] = graphics_reasons
        if graphics:
            return _hard(ITEM_REJECTED, reasons + [RC_MIME_GRAPHICS_DOMINATED, RC_EXTRACT_EMPTY],
                         evidence, hard_failed=True)
        return _hard(ITEM_NEEDS_REVIEW, reasons + [RC_EXTRACT_EMPTY], evidence, hard_failed=False)

    # 5) 语言基本符合
    targets = set(policy.target_languages)
    # 门槛按语种区分：zh/ru/ar 等非拉丁脚本用策略里的更低门槛，
    # 其余语种用全局 min_confidence_for_accept。调用方显式覆盖时以覆盖值为准。
    if opts.language_confidence_threshold is not None:
        threshold = float(opts.language_confidence_threshold)
        threshold_source = "caller_override"
    else:
        threshold = policy.min_language_confidence_for(language)
        threshold_source = ("per_language" if threshold != policy.min_language_confidence
                            else "policy_default")
    evidence["language_confidence_threshold"] = threshold
    evidence["language_threshold_source"] = threshold_source
    if language in targets and confidence >= threshold:
        reasons.append(RC_LANG_SUPPORTED)
    elif language == "unknown" or confidence < threshold:
        # 置信度不足时既不放行也不淘汰：识别错了会污染语种配额，
        # 直接淘汰又可能丢掉真正的 P0 语种样本，交人工最稳。
        reasons.append("language_ambiguous")
        evidence["language_ambiguous_reason"] = (
            "unknown" if language == "unknown" else f"confidence<{threshold}"
        )
        return _hard(ITEM_NEEDS_REVIEW, reasons, evidence, hard_failed=False)
    else:
        return _hard(ITEM_REJECTED, reasons + [RC_LANG_UNSUPPORTED], evidence, hard_failed=True)

    # 6) 非图形主导
    min_chars = int(content_rules.get("min_chars_for_query_generation", 800))
    graphics, graphics_reasons = is_graphics_dominated(
        char_count=char_count,
        page_count=int(page_count) if page_count else None,
        image_area_ratio=float(image_area_ratio) if image_area_ratio is not None else None,
        min_chars=min_chars,
    )
    if graphics:
        evidence["graphics_reasons"] = graphics_reasons
        return _hard(ITEM_REJECTED, reasons + [RC_MIME_GRAPHICS_DOMINATED], evidence, hard_failed=True)

    # 6b) 文字密度不足但没有图形证据 -> 复核，不当作图形主导淘汰
    sparse, sparse_reasons = is_text_too_sparse(
        char_count=char_count,
        page_count=int(page_count) if page_count else None,
        min_chars=min_chars,
    )
    if sparse:
        evidence["sparse_reasons"] = sparse_reasons
        return _hard(ITEM_NEEDS_REVIEW, reasons + ["text_too_sparse"], evidence, hard_failed=False)

    # 7) 非精确重复
    if dedup_decision and dedup_decision.get("status") == ITEM_REJECTED:
        codes = list(dedup_decision.get("reason_codes") or [])
        if RC_DUP_EXACT in codes or RC_DUP_NORMALIZED in codes:
            evidence["duplicate_of"] = (dedup_decision.get("evidence") or {}).get("representative")
            return _hard(ITEM_REJECTED, reasons + codes, evidence, hard_failed=True)

    # 通过硬规则。
    # 字数不足的分支已经在 6b 处理过，这里不再重复判断（避免死代码与双重口径）。
    if char_count > policy.full_text_review_threshold_chars:
        reasons.append("requires_full_text_review")
        evidence["is_long_document"] = True
    reasons.append(RC_OK)
    return _hard(ITEM_ACCEPTED, reasons, evidence, hard_failed=False)


def _hard(status: str, reasons: Sequence[str], evidence: Mapping[str, Any],
          hard_failed: bool) -> dict[str, Any]:
    return {"status": status, "reason_codes": list(reasons),
            "evidence": dict(evidence), "hard_failed": hard_failed}


def semantic_review(*, decision: Mapping[str, Any], llm: Any,
                    document_ref: Mapping[str, Any], policy: CollectionPolicy,
                    opts: ScreeningOptions) -> dict[str, Any]:
    """只处理通过硬规则的条目。返回更新后的决定。"""
    if decision["status"] != ITEM_ACCEPTED:
        return dict(decision)

    reasons = list(decision["reason_codes"])
    evidence = dict(decision["evidence"])
    try:
        verdict = llm.classify_document(document_ref, {
            "subjects": policy.subjects,
            "exclusions": policy.section("exclusions").get("document_kinds", []),
            "target_languages": list(policy.target_languages),
        })
    except Exception as exc:  # noqa: BLE001 - LLM 失败必须可恢复，不能让整批崩
        name = type(exc).__name__
        if name == "LLMUnavailable":
            code = RC_LLM_UNAVAILABLE
        elif name == "LLMBudgetExhausted":
            code = RC_LLM_BUDGET_EXHAUSTED
        else:
            code = RC_LLM_BAD_OUTPUT
        reasons.append(code)
        evidence["llm_error"] = f"{type(exc).__name__}: {exc}"[:400]
        return {"status": ITEM_NEEDS_REVIEW, "reason_codes": reasons,
                "evidence": evidence, "hard_failed": False, "llm": None}

    education = str(verdict.get("education_level") or "unknown").lower()
    subject = str(verdict.get("subject") or "unknown").lower()
    queryability = str(verdict.get("queryability") or "low").lower()
    exclude_kind = verdict.get("exclude_kind")
    llm_evidence = verdict.get("evidence") or []
    confidence = float(verdict.get("confidence") or 0.0)
    needs_human = bool(verdict.get("needs_human_review"))

    # 学科参与放行判定（P1 范围门禁，需求 3.1"学科属于 P0 或 P1"是硬条件）。
    # 模型给 unknown 时先回落到关键词兜底（P7）：兜底只在本模块内补 subject，
    # 用于让"模型判不出但文件名/正文有明显学科词"的条目不至于卡在 unknown。
    # 教育属性**参与放行判定**，绝不能拿关键词兜底去改判——那等于让"没有证据的判断"
    # 自动通过。兜底值只写进 evidence 的 *_hint，供报告显示。
    subject_source = "llm"
    if subject in ("unknown", ""):
        hint = str(evidence.get("subject_hint") or "unknown")
        if hint != "unknown":
            subject, subject_source = hint, "keyword_hint"

    evidence.update({
        "education_level": education, "subject": subject,
        "subject_source": subject_source, "education_level_source": "llm",
        "queryability": queryability, "exclude_kind": exclude_kind,
        "llm_confidence": confidence, "llm_evidence": llm_evidence[:6],
    })
    reasons.append(f"{RC_EDU_LEVEL}={education}")
    reasons.append(f"{RC_SUBJECT}={subject}")
    reasons.append(f"{RC_QUERYABILITY}={queryability}")

    if exclude_kind and str(exclude_kind).lower() not in ("null", "none", ""):
        reasons.append(f"excluded_kind={exclude_kind}")
        return {"status": ITEM_REJECTED, "reason_codes": reasons,
                "evidence": evidence, "hard_failed": False, "llm": verdict}
    if education in opts.rejected_education_levels:
        return {"status": ITEM_REJECTED, "reason_codes": reasons,
                "evidence": evidence, "hard_failed": False, "llm": verdict}
    if education in opts.review_education_levels:
        return {"status": ITEM_NEEDS_REVIEW, "reason_codes": reasons,
                "evidence": evidence, "hard_failed": False, "llm": verdict}

    # 学科范围门禁：不在 P0∪P1 集合 → 淘汰；unknown → 按策略落 needs_review。
    # 放在教育属性之后、queryability 之前：教育属性不合格的更根本，先判它。
    # allowed_subjects 为空时门禁自动不生效（无法拿空集当白名单，否则全部误杀）——
    # run_screen 会从策略填充它；直接构造 ScreeningOptions 的单测不填即视为关闭。
    if opts.subject_gate and opts.allowed_subjects and subject not in opts.allowed_subjects:
        if subject == "unknown":
            action = opts.subject_unknown_action
            reasons.append(RC_SUBJECT_OUT_OF_SCOPE)
            status = ITEM_REJECTED if action == "reject" else ITEM_NEEDS_REVIEW
            return {"status": status, "reason_codes": reasons,
                    "evidence": evidence, "hard_failed": False, "llm": verdict}
        reasons.append(RC_SUBJECT_OUT_OF_SCOPE)
        evidence["subject_out_of_scope"] = subject
        return {"status": ITEM_REJECTED, "reason_codes": reasons,
                "evidence": evidence, "hard_failed": False, "llm": verdict}

    if queryability == "low":
        return {"status": ITEM_REJECTED, "reason_codes": reasons,
                "evidence": evidence, "hard_failed": False, "llm": verdict}
    if queryability == "medium":
        return {"status": ITEM_NEEDS_REVIEW, "reason_codes": reasons,
                "evidence": evidence, "hard_failed": False, "llm": verdict}
    if not llm_evidence:
        # 计划 Task 6：没有证据的模型判断不能自动接受
        reasons.append("llm_no_evidence")
        return {"status": ITEM_NEEDS_REVIEW, "reason_codes": reasons,
                "evidence": evidence, "hard_failed": False, "llm": verdict}
    if confidence < opts.semantic_confidence_threshold or needs_human:
        reasons.append("llm_low_confidence" if confidence < opts.semantic_confidence_threshold
                       else "llm_requests_human_review")
        return {"status": ITEM_NEEDS_REVIEW, "reason_codes": reasons,
                "evidence": evidence, "hard_failed": False, "llm": verdict}

    return {"status": ITEM_ACCEPTED, "reason_codes": reasons,
            "evidence": evidence, "hard_failed": False, "llm": verdict}


def _load_text(item_id: str, workspace_dir: str,
               extraction: Mapping[str, Any] | None = None) -> str:
    """读回抽取阶段落盘的正文。

    优先用库里记录的 text_path（抽取时的真实路径），
    回退到约定路径 extracted/<item_id>.txt。
    """
    candidates: list[str] = []
    recorded = (extraction or {}).get("text_path")
    if recorded:
        candidates.append(str(recorded))
    candidates.append(os.path.join(workspace_dir, "extracted", f"{item_id}.txt"))
    for path in candidates:
        if path and winpath.isfile(path):
            try:
                with winpath.open_read(path, encoding="utf-8-sig") as fh:
                    return fh.read()
            except OSError:
                continue
    return ""


def run_screen(*, policy: CollectionPolicy, store: StateStore, workspace_dir: str,
               run_id: str, options: ScreeningOptions | None = None,
               llm: Any | None = None, log: LogFn = _noop) -> dict[str, Any]:
    """执行筛选，写 screening_report.jsonl / human_review_queue.csv，落库。"""
    opts = options or ScreeningOptions()
    # 学科门禁的允许集合与落点由策略统一决定，避免调用方漏填导致门禁形同虚设。
    # 调用方显式传了 allowed_subjects 则尊重之（定向白名单场景）。
    if not opts.allowed_subjects:
        opts.allowed_subjects = policy.target_subjects_for_hint
    if opts.subject_gate and not policy.subject_gate_enabled:
        opts.subject_gate = False
    if opts.subject_unknown_action == "needs_review":
        opts.subject_unknown_action = policy.subject_unknown_action
    # 门槛由策略决定，不在代码里写死。None 表示"按语种取策略门槛"
    # （zh/ru/ar 有单独更低的 min_confidence_by_language），由 hard_screen 解析；
    # CLI 传入具体值时是调用方的明确覆盖，优先级最高。
    winpath.makedirs(workspace_dir)
    all_items = store.list_items(statuses=list(opts.include_statuses) or None)
    if opts.include_unprocessed:
        items = all_items
        skipped = 0
    else:
        processed = _processed_item_ids(store)
        items = [i for i in all_items if str(i["item_id"]) in processed]
        skipped = len(all_items) - len(items)
    if skipped:
        log(f"[screen] 跳过 {skipped} 条尚未采集的条目（没有本地产物也没有抽取记录）；"
            f"需要一并处理时加 --include-unprocessed")
    if opts.limit:
        items = items[: int(opts.limit)]

    dedup_decisions = store.latest_decisions("deduplicate")
    acquire_decisions = store.latest_decisions("acquire")
    run_semantic = bool(opts.run_semantic and llm is not None and getattr(llm, "available", False))
    if opts.run_semantic and not run_semantic:
        log("[screen] 语义复核未启用（llm 不可用或未配置），只做硬规则筛选")

    log(f"[screen] 待筛选 {len(items)} 条，语义复核={run_semantic}，"
        f"并发 {max(1, opts.concurrency) if run_semantic else 1}")
    report_path = os.path.join(workspace_dir, "screening_report.jsonl")
    counts: dict[str, int] = {}
    reason_counter: dict[str, int] = {}
    lang_accepted: dict[str, int] = {}
    subject_accepted: dict[str, int] = {}
    review_rows: list[dict[str, Any]] = []
    accepted_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []
    started = time.time()
    done = {"n": 0}

    # 主线程取好每个条目的只读快照（worker 不碰 store），
    # 语义复核是逐条独立模型调用，放线程池并发；落库仍回主线程按序执行。
    snapshots = [
        {
            "item": item,
            "artifact": store.get_artifact(str(item["item_id"])),
            "extraction": store.get_extraction(str(item["item_id"])),
        }
        for item in items
    ]

    def _work(index: int, snap: Mapping[str, Any]) -> dict[str, Any]:
        item = snap["item"]
        artifact = snap["artifact"]
        extraction = snap["extraction"]
        decision = hard_screen(item=item, artifact=artifact, extraction=extraction,
                               dedup_decision=dedup_decisions.get(str(item["item_id"])),
                               acquire_decision=acquire_decisions.get(str(item["item_id"])),
                               policy=policy, opts=opts)
        llm_verdict = None
        if run_semantic and decision["status"] == ITEM_ACCEPTED:
            text = _load_text(str(item["item_id"]), workspace_dir, extraction)
            document_ref = {
                "name": item.get("name"),
                "source_name": item.get("source_name"),
                "relative_path": (item.get("metadata") or {}).get("relative_path"),
                "extension": item.get("extension"),
                "language": (extraction or {}).get("language"),
                "char_count": (extraction or {}).get("char_count"),
                "page_count": (extraction or {}).get("page_count"),
                "text": truncate_for_prompt(text, opts.llm_text_sample_chars),
            }
            decision = semantic_review(decision=decision, llm=llm,
                                       document_ref=document_ref,
                                       policy=policy, opts=opts)
            llm_verdict = decision.pop("llm", None)
        return {"decision": decision, "llm_verdict": llm_verdict}

    with winpath.open_write(report_path, "a", encoding="utf-8") as fh:
        def _persist(index: int, value: Any) -> None:
            item = items[index]
            artifact = snapshots[index]["artifact"]
            extraction = snapshots[index]["extraction"]
            item_id = str(item["item_id"])
            if isinstance(value, BaseException):
                decision = {"status": ITEM_NEEDS_REVIEW,
                            "reason_codes": ["screen_exception"],
                            "evidence": {"error": f"{type(value).__name__}: {value}"[:400]}}
                llm_verdict = None
            else:
                decision = value["decision"]
                llm_verdict = value.get("llm_verdict")

            status = decision["status"]
            counts[status] = counts.get(status, 0) + 1
            for code in decision["reason_codes"]:
                key = code.split("=", 1)[0]
                reason_counter[key] = reason_counter.get(key, 0) + 1

            store.add_decision(item_id, "screen", status, decision["reason_codes"],
                               decision["evidence"])
            store.set_item_stage(item_id, status, "screen")

            row = {
                "item_id": item_id,
                "name": item.get("name"),
                "source_name": item.get("source_name"),
                "source_path": f"{item.get('source_name')}/"
                               f"{(item.get('metadata') or {}).get('relative_path') or item.get('name')}",
                "url": item.get("url"),
                "item_type": item.get("item_type"),
                "extension": item.get("extension"),
                "status": status,
                "deciding_reason": deciding_reason(decision["reason_codes"]),
                "reason_codes": decision["reason_codes"],
                "size_bytes": decision["evidence"].get("size_bytes"),
                "char_count": decision["evidence"].get("char_count"),
                "language": decision["evidence"].get("language"),
                "education_level": (decision["evidence"].get("education_level")
                                    or decision["evidence"].get("education_level_hint")),
                "subject": (decision["evidence"].get("subject")
                            or decision["evidence"].get("subject_hint")),
                "artifact_path": (artifact or {}).get("path"),
                "error": (extraction or {}).get("error") or (artifact or {}).get("error"),
                "run_id": run_id,
                "screened_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            if status == ITEM_ACCEPTED:
                accepted_rows.append(row)
                lang = str(decision["evidence"].get("language") or "unknown")
                lang_accepted[lang] = lang_accepted.get(lang, 0) + 1
                # 学科：模型判定优先，缺省回落到关键词兜底提示，避免清单学科全空
                subj = str(decision["evidence"].get("subject")
                           or decision["evidence"].get("subject_hint") or "unknown")
                subject_accepted[subj] = subject_accepted.get(subj, 0) + 1
            elif status == ITEM_REJECTED:
                rejected_rows.append(row)
            else:
                review_rows.append(row)
            fh.write(json.dumps({**row, "evidence": decision["evidence"],
                                 "llm_verdict": llm_verdict}, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            done["n"] += 1
            if done["n"] % 50 == 0 or done["n"] == len(items):
                store.commit()
                log(f"[screen] {done['n']}/{len(items)} 状态分布 {counts}")

        map_ordered(_work, snapshots,
                    concurrency=(opts.concurrency if run_semantic else 1),
                    on_result=_persist)

    store.commit()
    _write_csv(os.path.join(workspace_dir, "accepted_documents.csv"), accepted_rows)
    _write_csv(os.path.join(workspace_dir, "rejected_documents.csv"), rejected_rows)
    _write_csv(os.path.join(workspace_dir, "human_review_queue.csv"), review_rows)

    summary = {
        "total_screened": len(items),
        "skipped_unprocessed": skipped,
        "total_items_in_store": len(all_items),
        "status_counts": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "reason_counts": dict(sorted(reason_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        "accepted_by_language": dict(sorted(lang_accepted.items(), key=lambda kv: (-kv[1], kv[0]))),
        "accepted_by_subject": dict(sorted(subject_accepted.items(), key=lambda kv: (-kv[1], kv[0]))),
        "semantic_review_enabled": run_semantic,
        "language_confidence_threshold": (opts.language_confidence_threshold
                                          if opts.language_confidence_threshold is not None
                                          else policy.min_language_confidence),
        "language_confidence_thresholds_by_language": {
            lang: policy.min_language_confidence_for(lang)
            for lang in policy.target_languages
        },
        "elapsed_sec": round(time.time() - started, 2),
        "artifacts": [report_path,
                      os.path.join(workspace_dir, "accepted_documents.csv"),
                      os.path.join(workspace_dir, "rejected_documents.csv"),
                      os.path.join(workspace_dir, "human_review_queue.csv")],
    }
    store.touch_run(run_id, {"collection.screen": summary})
    log(f"[screen] 完成：{summary['status_counts']}")
    return summary


def _write_csv(path: str, rows: Sequence[Mapping[str, Any]]) -> None:
    fieldnames = ["item_id", "name", "source_name", "source_path", "url", "item_type",
                  "extension", "status", "deciding_reason", "reason_codes",
                  "size_bytes", "char_count",
                  "language", "education_level", "subject", "artifact_path", "error",
                  "run_id", "screened_at"]
    winpath.makedirs(os.path.dirname(os.path.abspath(path)))
    with winpath.open_write(path, encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = dict(row)
            if isinstance(out.get("reason_codes"), (list, tuple)):
                out["reason_codes"] = ";".join(str(c) for c in out["reason_codes"])
            writer.writerow(out)
