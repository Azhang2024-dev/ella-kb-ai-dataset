"""配置加载与校验。

策略文件（config/collection_policy.json）是业务规则的唯一事实来源：
大小上限、语种配额、题型/题量、排除项、安全约束都写在 JSON 里，
代码只做加载、校验和取值，不硬编码业务数字。

这样当需求（例如题量从 5 改成 4、语种增加一个）变化时，
只改 JSON，不改代码，也不需要重新测试筛选逻辑本身。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import CONTRACT_VERSION, RunConfig

DEFAULT_POLICY_PATH = os.path.join("config", "collection_policy.json")

# run_id 会直接拼进工作区路径，因此必须是文件系统安全的：
# 只允许字母数字开头 + [A-Za-z0-9._-]，并且拒绝 "." / ".." 这类纯点号名，
# 否则 workspace_root/".." 会逃出工作区。
_RUN_ID_RE = re.compile(r"^(?!\.+)[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class PolicyError(ValueError):
    """策略文件缺失、格式非法或业务约束自相矛盾时抛出。"""


@dataclass(frozen=True)
class CollectionPolicy:
    """对策略 JSON 的只读封装。

    刻意保留 raw（原始 dict），因为报告需要把生效规则原样落盘，
    便于事后复核"当时到底按什么标准筛的"。
    """

    path: str
    raw: dict[str, Any]

    # -- 基础 -------------------------------------------------------------
    @property
    def schema_version(self) -> str:
        return str(self.raw.get("schema_version", CONTRACT_VERSION))

    @property
    def sources(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(s) for s in self.raw.get("sources", ()))

    def section(self, name: str) -> dict[str, Any]:
        val = self.raw.get(name)
        return dict(val) if isinstance(val, Mapping) else {}

    # -- 格式 -------------------------------------------------------------
    @property
    def formats(self) -> dict[str, Any]:
        return self.section("formats")

    @property
    def allowed_extensions(self) -> tuple[str, ...]:
        return tuple(str(e).lower().lstrip(".") for e in self.formats.get("allowed_extensions", ()))

    @property
    def conversion_required_extensions(self) -> tuple[str, ...]:
        return tuple(
            str(e).lower().lstrip(".")
            for e in self.formats.get("conversion_required_extensions", ())
        )

    @property
    def excluded_extensions(self) -> tuple[str, ...]:
        return tuple(str(e).lower().lstrip(".") for e in self.formats.get("excluded_extensions", ()))

    @property
    def online_type_export(self) -> dict[str, str]:
        return {str(k).lower(): str(v).lower() for k, v in self.formats.get("online_type_export", {}).items()}

    @property
    def online_types_pending_size(self) -> tuple[str, ...]:
        return tuple(str(t).lower() for t in self.formats.get("online_types_pending_size", ()))

    # -- 大小 -------------------------------------------------------------
    @property
    def size_limits_mb(self) -> dict[str, int]:
        return {str(k).lower().lstrip("."): int(v) for k, v in self.raw.get("size_limits_mb", {}).items()}

    def size_limit_mb(self, extension: str | None, item_type: str | None = None) -> int | None:
        """返回某扩展名的 MB 上限；未配置返回 None（调用方决定如何处理）。"""
        if not extension:
            return None
        return self.size_limits_mb.get(str(extension).lower().lstrip("."))

    @property
    def online_size_policy(self) -> str:
        return str(self.raw.get("online_size_policy", "pending_size_verification"))

    # -- 语言 / 学科 ------------------------------------------------------
    @property
    def languages(self) -> dict[str, Any]:
        return self.section("languages")

    @property
    def target_languages(self) -> tuple[str, ...]:
        langs = list(self.languages.get("p0", ()))
        langs += [x for x in self.languages.get("p1_extra", ()) if x not in langs]
        return tuple(str(x).lower() for x in langs)

    @property
    def min_documents_per_language(self) -> int:
        return int(self.languages.get("min_documents_per_language", 0))

    @property
    def min_language_confidence(self) -> float:
        """语种放行的最低置信度。低于此值一律 needs_review，不放行也不淘汰。"""
        return float(self.languages.get("min_confidence_for_accept", 0.70))

    def min_language_confidence_for(self, language: str | None) -> float:
        """某语种实际生效的置信度门槛。

        非拉丁脚本语种（zh/ru/ar）靠字形占比识别，证据比拉丁语系的虚词命中更硬，
        策略里对它们单独给了更低门槛；未配置的语种回落到全局 min_confidence_for_accept。
        调用方显式覆盖门槛时不应走这里（覆盖是调用方的明确意图）。
        """
        per_language = self.languages.get("min_confidence_by_language") or {}
        if isinstance(per_language, Mapping) and language:
            value = per_language.get(str(language).lower())
            if value is not None:
                return float(value)
        return self.min_language_confidence

    @property
    def prompt_text_chars(self) -> int:
        """喂给模型的正文窗口上限（字符）。单一事实来源，query 与出题共用。"""
        return int(self.content_rules.get("prompt_text_chars", 50000))

    @property
    def subjects(self) -> dict[str, Any]:
        return self.section("subjects")

    @property
    def target_subjects_for_hint(self) -> tuple[str, ...]:
        """关键词兜底允许给出的学科 key（P0 + P1），避免吐出未登记学科。"""
        subjects = self.subjects
        out: list[str] = []
        for key in ("p0", "p1", "p1_extra"):
            for value in subjects.get(key, ()) or ():
                text = str(value).lower()
                if text not in out:
                    out.append(text)
        return tuple(out)

    @property
    def subject_gate_enabled(self) -> bool:
        """学科是否参与放行判定（P1 范围门禁）。

        需求 3.1 把"学科属于 P0 或 P1"列为与语种、教育属性并列的**必须满足**条件，
        但历史实现只把 subject 当展示用配比维度，导致心理学/社会学/艺术学这类
        范围外文档也能 accepted。默认开启；策略可显式关闭（旧批次对照用）。
        """
        gate = self.subjects.get("gate")
        if isinstance(gate, Mapping):
            return bool(gate.get("enabled", True))
        return True

    @property
    def subject_unknown_action(self) -> str:
        """模型判不出学科（unknown）时的落点：needs_review（默认）或 rejected。

        默认 needs_review：学科判不出不等于范围外，不能当越界直接淘汰（宁交人工不误杀）。
        """
        gate = self.subjects.get("gate")
        if isinstance(gate, Mapping):
            return str(gate.get("unknown_action", "needs_review")).lower()
        return "needs_review"

    # -- 内容规则 ---------------------------------------------------------
    @property
    def content_rules(self) -> dict[str, Any]:
        return self.section("content_rules")

    @property
    def full_text_review_threshold_chars(self) -> int:
        return int(self.content_rules.get("full_text_review_threshold_chars", 50000))

    @property
    def min_chars_for_query_generation(self) -> int:
        return int(self.content_rules.get("min_chars_for_query_generation", 0))

    # -- Query ------------------------------------------------------------
    @property
    def query_plan(self) -> dict[str, Any]:
        return self.section("query_plan")

    @property
    def query_types(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(t) for t in self.query_plan.get("query_types", ()))

    @property
    def queries_per_document(self) -> int:
        declared = int(self.query_plan.get("queries_per_document", 0))
        derived = sum(int(t.get("count", 0)) for t in self.query_types)
        # 两者不一致时以题型明细为准，并在校验阶段报错，避免静默偏差。
        return derived or declared

    @property
    def forbidden_reference_kinds(self) -> tuple[str, ...]:
        return tuple(str(x).lower() for x in self.query_plan.get("forbidden_reference_kinds", ()))

    # -- 去重 -------------------------------------------------------------
    @property
    def deduplication(self) -> dict[str, Any]:
        return self.section("deduplication")

    # -- LLM --------------------------------------------------------------
    @property
    def llm(self) -> dict[str, Any]:
        return self.section("llm")

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm.get("enabled", False))

    # -- 出题 --------------------------------------------------------------
    @property
    def question_generation(self) -> dict[str, Any]:
        return self.section("question_generation")

    # -- 运行默认值 / 安全 ------------------------------------------------
    @property
    def run_defaults(self) -> dict[str, Any]:
        return self.section("run_defaults")

    @property
    def safety(self) -> dict[str, Any]:
        return self.section("safety")

    @property
    def source_readonly(self) -> bool:
        return bool(self.safety.get("source_readonly", True))

    @property
    def allowlisted_lark_commands(self) -> tuple[str, ...]:
        return tuple(str(c) for c in self.safety.get("allowlisted_lark_commands", ()))

    # -- 派生 -------------------------------------------------------------
    def build_run_config(self, run_id: str, **overrides: Any) -> RunConfig:
        """把策略投影成一次运行的 RunConfig。

        overrides 允许 CLI 覆盖少数字段（dry_run / llm_enabled / workspace_dir），
        但不允许覆盖大小上限和题型表这类业务硬约束。
        """
        if not _RUN_ID_RE.match(run_id or ""):
            raise PolicyError(
                f"非法 run_id: {run_id!r}（必须以字母或数字开头，只允许 A-Za-z0-9._-，"
                f"长度 1-128，且不能是纯点号，因为它会被拼进工作区路径）"
            )

        forbidden_overrides = {"max_size_mb", "query_plan", "sources", "target_languages"}
        bad = forbidden_overrides.intersection(overrides)
        if bad:
            raise PolicyError(f"以下字段不允许在运行时覆盖：{sorted(bad)}")

        qp = self.query_plan
        defaults = self.run_defaults
        cfg = RunConfig(
            run_id=run_id,
            source_folders=tuple(s.get("token", "") for s in self.sources),
            allowed_extensions=self.allowed_extensions,
            max_size_mb=self.size_limits_mb,
            target_languages=self.target_languages,
            queries_per_document=self.queries_per_document,
            dry_run=bool(overrides.get("dry_run", defaults.get("dry_run", False))),
            approval_mode=str(overrides.get("approval_mode", defaults.get("approval_mode", "read_only"))),
            llm_enabled=bool(overrides.get("llm_enabled", self.llm_enabled)),
            schema_version=self.schema_version,
            sources=self.sources,
            question_count_min=int(qp.get("question_count_min", 2)),
            question_count_max=int(qp.get("question_count_max", 11)),
            preferred_question_counts=tuple(int(x) for x in qp.get("preferred_question_counts", (2, 3, 5, 7, 9))),
            min_documents_per_language=self.min_documents_per_language,
            full_text_review_threshold_chars=self.full_text_review_threshold_chars,
            online_types_pending_size=self.online_types_pending_size,
            workspace_dir=str(overrides.get("workspace_dir", defaults.get("workspace_dir", "output/document_collection"))),
            llm_config_path=str(overrides.get("llm_config_path", self.llm.get("config_path", ""))),
        )
        return cfg

    def to_dict(self) -> dict[str, Any]:
        return dict(self.raw)


def _require(cond: bool, errors: list[str], message: str) -> None:
    if not cond:
        errors.append(message)


def validate_policy(policy: CollectionPolicy) -> list[str]:
    """返回错误列表；空列表表示策略可用。

    校验重点是"业务约束自相矛盾"，例如：
    - 题型明细加起来不等于声明的每文档 query 数；
    - 某题型题量超出 [question_count_min, question_count_max]；
    - 题量在不同题型间重复（需求明确要求尽量不重复）；
    - allowed_extensions 里没有配大小上限；
    - 安全段没有声明只读。
    """
    errors: list[str] = []
    raw = policy.raw

    _require(bool(raw.get("sources")), errors, "sources 不能为空")
    for idx, src in enumerate(policy.sources):
        _require(bool(src.get("name")), errors, f"sources[{idx}] 缺少 name（源文件夹名称必须写清楚）")
        _require(bool(src.get("token")), errors, f"sources[{idx}] 缺少 token")
        _require(src.get("readonly") is True, errors, f"sources[{idx}] 必须声明 readonly=true")

    qp = policy.query_plan
    declared = int(qp.get("queries_per_document", 0))
    derived = sum(int(t.get("count", 0)) for t in policy.query_types)
    _require(
        declared == derived,
        errors,
        f"query_plan.queries_per_document({declared}) 与题型明细合计({derived}) 不一致",
    )

    single = sum(int(t.get("count", 0)) for t in policy.query_types if not t.get("compatibility_only"))
    mixed = sum(int(t.get("count", 0)) for t in policy.query_types if t.get("compatibility_only"))
    _require(
        single == int(qp.get("single_type_query_count", single)),
        errors,
        "单题型 query 条数与 query_plan.single_type_query_count 不一致",
    )
    _require(
        mixed == int(qp.get("mixed_type_query_count", mixed)),
        errors,
        "混合题型 query 条数与 query_plan.mixed_type_query_count 不一致",
    )

    qmin = int(qp.get("question_count_min", 2))
    qmax = int(qp.get("question_count_max", 11))
    counts: list[int] = []
    for t in policy.query_types:
        qc = int(t.get("question_count", 0))
        counts.append(qc)
        _require(
            qmin <= qc <= qmax,
            errors,
            f"题型 {t.get('type_key')} 题量 {qc} 超出允许区间 [{qmin}, {qmax}]（需求要求小于 12）",
        )
    if qp.get("question_counts_should_not_repeat", True):
        dup = {c for c in counts if counts.count(c) > 1}
        _require(not dup, errors, f"不同题型题量重复：{sorted(dup)}（需求要求尽量不重复）")

    _require(
        bool(qp.get("whole_document_only", False)),
        errors,
        "query_plan.whole_document_only 必须为 true（query 面向整篇文档）",
    )
    _require(
        bool(policy.forbidden_reference_kinds),
        errors,
        "query_plan.forbidden_reference_kinds 不能为空",
    )

    limits = policy.size_limits_mb
    for ext in policy.allowed_extensions:
        _require(ext in limits, errors, f"allowed_extensions 中的 {ext} 缺少 size_limits_mb 配置")
    for ext in policy.conversion_required_extensions:
        _require(ext in limits, errors, f"conversion_required_extensions 中的 {ext} 缺少 size_limits_mb 配置")
    overlap = set(policy.allowed_extensions) & set(policy.conversion_required_extensions)
    _require(not overlap, errors, f"格式同时出现在 allowed 和 conversion_required：{sorted(overlap)}")

    _require(bool(policy.target_languages), errors, "languages.p0 不能为空")
    _require(
        policy.min_documents_per_language > 0,
        errors,
        "languages.min_documents_per_language 必须大于 0",
    )

    safety = policy.safety
    _require(safety.get("source_readonly") is True, errors, "safety.source_readonly 必须为 true")
    _require(bool(safety.get("allowlisted_lark_commands")), errors, "safety.allowlisted_lark_commands 不能为空")
    _require(bool(safety.get("never_log")), errors, "safety.never_log 不能为空")

    cr = policy.content_rules
    _require(
        int(cr.get("full_text_review_threshold_chars", 0)) > 0,
        errors,
        "content_rules.full_text_review_threshold_chars 必须大于 0",
    )
    _require(
        int(cr.get("prompt_text_chars", 0)) > 0,
        errors,
        "content_rules.prompt_text_chars 必须大于 0（query/出题共用同一输入窗口）",
    )
    _require(
        int(cr.get("prompt_text_chars", 0)) >= int(cr.get("min_chars_for_query_generation", 0)),
        errors,
        "content_rules.prompt_text_chars 不能小于 min_chars_for_query_generation",
    )

    per_language = policy.languages.get("min_confidence_by_language") or {}
    if isinstance(per_language, Mapping):
        p0 = set(policy.target_languages)
        unknown = {str(k).lower() for k in per_language} - p0
        _require(
            not unknown,
            errors,
            f"languages.min_confidence_by_language 含非目标语种：{sorted(unknown)}",
        )
        for key, value in per_language.items():
            _require(
                0.0 < float(value) <= policy.min_language_confidence,
                errors,
                f"languages.min_confidence_by_language[{key}]={value} 必须在 (0, "
                f"min_confidence_for_accept] 之间（只能更严或更松，不能定义成无法触发的值）",
            )

    return errors


def load_policy(path: str | os.PathLike[str] | None = None) -> CollectionPolicy:
    """加载并校验策略；校验失败抛 PolicyError（带全部错误明细）。"""
    resolved = os.fspath(path) if path else DEFAULT_POLICY_PATH
    if not os.path.isabs(resolved):
        resolved = os.path.abspath(resolved)
    if not os.path.isfile(resolved):
        raise PolicyError(f"策略文件不存在：{resolved}")
    try:
        with open(resolved, "r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"策略文件不是合法 JSON：{resolved}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise PolicyError(f"策略文件顶层必须是对象：{resolved}")

    policy = CollectionPolicy(path=resolved, raw=raw)
    errors = validate_policy(policy)
    if errors:
        raise PolicyError("策略校验失败：\n  - " + "\n  - ".join(errors))
    return policy


def describe_policy(policy: CollectionPolicy) -> dict[str, Any]:
    """给 validate-config / 报告用的摘要，只暴露生效规则，不暴露密钥。"""
    return {
        "policy_path": policy.path,
        "schema_version": policy.schema_version,
        "sources": [
            {"name": s.get("name"), "token": s.get("token"), "role": s.get("role")}
            for s in policy.sources
        ],
        "allowed_extensions": list(policy.allowed_extensions),
        "conversion_required_extensions": list(policy.conversion_required_extensions),
        "size_limits_mb": policy.size_limits_mb,
        "target_languages": list(policy.target_languages),
        "min_documents_per_language": policy.min_documents_per_language,
        "min_language_confidence": policy.min_language_confidence,
        "queries_per_document": policy.queries_per_document,
        "query_types": [
            {
                "type_key": t.get("type_key"),
                "count": t.get("count"),
                "question_count": t.get("question_count"),
                "compatibility_only": bool(t.get("compatibility_only")),
            }
            for t in policy.query_types
        ],
        "llm_enabled": policy.llm_enabled,
        "source_readonly": policy.source_readonly,
        "requirement_revisions": {
            k: {"token": v.get("token"), "revision_id": v.get("revision_id")}
            for k, v in policy.section("requirement_revisions").items()
            if isinstance(v, Mapping)
        },
    }
