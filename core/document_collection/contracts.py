"""统一契约层：输入、输出、状态与证据结构。

设计目的
--------
CLI、Skill 以及未来的 MCP Server / Plugin 都只依赖本模块定义的数据结构，
不直接依赖任何具体实现。这样当数据源（飞书云盘 -> 其他网盘）、业务规则
（大小/语言/题量）或宿主平台发生变化时，改动集中在适配器和配置，而不是契约。

约束
----
1. 所有 dataclass 都是 frozen，避免中途被意外改写；
2. 所有对外结构都能无损转成 JSON（to_dict / from_dict）；
3. 每个条目决策都必须带 status + reason_codes + 溯源信息（source_path / url）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

CONTRACT_VERSION = "0.1"

# ---------------------------------------------------------------------------
# 运行级状态（RunResult.status）
# ---------------------------------------------------------------------------
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

RUN_STATUSES: tuple[str, ...] = (
    STATUS_COMPLETED,
    STATUS_PARTIAL,
    STATUS_NEEDS_REVIEW,
    STATUS_FAILED,
    STATUS_CANCELLED,
)

# ---------------------------------------------------------------------------
# 条目级状态（单个文档在流水线中的位置）
# ---------------------------------------------------------------------------
ITEM_DISCOVERED = "discovered"
ITEM_ACQUIRED = "acquired"
ITEM_ACQUIRE_FAILED = "acquire_failed"
ITEM_PENDING_EXPORT = "pending_export"
ITEM_EXTRACTED = "extracted"
ITEM_EXTRACTION_FAILED = "extraction_failed"
ITEM_CONVERSION_REQUIRED = "conversion_required"
ITEM_ACCEPTED = "accepted"
ITEM_REJECTED = "rejected"
ITEM_NEEDS_REVIEW = "needs_review"

# 计划 Task 5 要求的五个对外输出状态
ITEM_OUTPUT_STATUSES: tuple[str, ...] = (
    ITEM_ACCEPTED,
    ITEM_REJECTED,
    ITEM_NEEDS_REVIEW,
    ITEM_PENDING_EXPORT,
    ITEM_CONVERSION_REQUIRED,
)

# ---------------------------------------------------------------------------
# 能力名称 <-> CLI 命令 <-> 未来 MCP Tool
# 这张表是 Skill / MCP / Plugin 的共同事实来源，禁止各处重复定义。
# ---------------------------------------------------------------------------
CAPABILITIES: tuple[dict[str, str], ...] = (
    {
        "name": "collection.inventory",
        "cli_command": "inventory",
        "future_mcp_tool": "collection_inventory",
    },
    {
        "name": "collection.acquire",
        "cli_command": "acquire",
        "future_mcp_tool": "collection_acquire",
    },
    {
        "name": "collection.extract",
        "cli_command": "extract",
        "future_mcp_tool": "collection_extract",
    },
    {
        "name": "collection.screen",
        "cli_command": "screen",
        "future_mcp_tool": "collection_screen",
    },
    {
        "name": "collection.deduplicate",
        "cli_command": "deduplicate",
        "future_mcp_tool": "collection_deduplicate",
    },
    {
        "name": "collection.generate_queries",
        "cli_command": "query",
        "future_mcp_tool": "collection_generate_queries",
    },
    {
        # 参考题目生成（题干/选项/答案/解析）：消费 query 阶段的 validated 文档。
        # 与 generate_queries 同属"出题"能力域，但产物与语义不同（query 是"要什么题"，
        # questions 是"题目本身"），因此单独成一个能力，契约对齐 query。
        "name": "collection.generate_questions",
        "cli_command": "questions",
        "future_mcp_tool": "collection_generate_questions",
    },
    {
        "name": "collection.report",
        "cli_command": "report",
        "future_mcp_tool": "collection_report",
    },
    {
        "name": "collection.resume",
        "cli_command": "resume",
        "future_mcp_tool": "collection_resume",
    },
    {
        # 本地优先入口：直接登记用户已下载好的文档，跳过云盘 inventory/acquire。
        # 不进入 fixed_pipeline_order（那条是云盘链路的固定顺序），
        # 而是与 inventory+acquire 等价的另一条入口。
        "name": "collection.ingest",
        "cli_command": "ingest",
        "future_mcp_tool": "collection_ingest",
    },
    {
        # 交付能力：从 accepted 池按学科/语种选目标集，出真实文件名 + 交付表。
        # 取代历史的外部一次性脚本 build_selection.py（那套用目录名当语种、
        # 文件名当学科，与 Core 口径脱节）。
        "name": "collection.deliver",
        "cli_command": "deliver",
        "future_mcp_tool": "collection_deliver",
    },
)

CAPABILITY_NAMES: tuple[str, ...] = tuple(c["name"] for c in CAPABILITIES)
CLI_COMMANDS: tuple[str, ...] = tuple(c["cli_command"] for c in CAPABILITIES)

# ---------------------------------------------------------------------------
# 副作用声明（manifest 使用）
# ---------------------------------------------------------------------------
SIDE_EFFECT_READ_SOURCE = "read_source"
SIDE_EFFECT_WRITE_LOCAL = "write_local"
SIDE_EFFECT_CALL_LLM = "call_llm"
SIDE_EFFECT_NEEDS_HUMAN = "needs_human_review"
SIDE_EFFECT_RETRYABLE = "retryable"

# 全局硬约束：任何能力都不得写回云盘源。
FORBIDDEN_SIDE_EFFECTS: tuple[str, ...] = (
    "modify_source",
    "delete_source",
    "move_source",
    "rename_source",
    "upload_to_source",
)

# ---------------------------------------------------------------------------
# 常用 reason code（可解释性要求：每个决定都要有 reason_codes）
# ---------------------------------------------------------------------------
RC_OK = "ok"
RC_ACQUIRED = "acquired"
RC_SIZE_UNKNOWN_REMOTE = "size_unknown_remote"
RC_SIZE_OVER_LIMIT = "size_over_limit"
RC_SIZE_WITHIN_LIMIT = "size_within_limit"
RC_EXT_UNSUPPORTED = "extension_unsupported"
RC_EXT_SUPPORTED = "extension_supported"
RC_MIME_UNSUPPORTED = "mime_unsupported"
RC_MIME_GRAPHICS_DOMINATED = "graphics_dominated"
RC_ACQUIRE_FAILED = "acquire_failed"
RC_PENDING_EXPORT = "pending_export"
RC_CONVERSION_REQUIRED = "conversion_required"
RC_EXTRACT_OK = "extract_ok"
RC_EXTRACT_FAILED = "extract_failed"
RC_EXTRACT_EMPTY = "extract_empty_text"
RC_LANG_UNSUPPORTED = "language_not_in_scope"
RC_LANG_SUPPORTED = "language_in_scope"
RC_DUP_EXACT = "exact_duplicate"
RC_DUP_NORMALIZED = "normalized_duplicate"
RC_DUP_UNIQUE = "unique"
RC_NEAR_DUP_SUSPECT = "near_duplicate_suspect"
RC_EDU_LEVEL = "education_level"
RC_SUBJECT = "subject"
RC_QUERYABILITY = "queryability"
RC_LLM_UNAVAILABLE = "llm_unavailable"
RC_LLM_BAD_OUTPUT = "llm_output_invalid"
# 推理模型的 max_tokens 预算被推理过程吃光（finish_reason=length 且正文为空）。
# 与"输出格式不对"是两码事：前者要调大预算，后者要修提示词，不能混为一谈。
RC_LLM_BUDGET_EXHAUSTED = "llm_budget_exhausted"
RC_QUERY_COUNT = "query_count_mismatch"
RC_QUERY_QUESTION_COUNT = "question_count_out_of_range"
RC_QUERY_QUESTION_DUP = "question_count_repeated"
RC_QUERY_FRAGMENT_REF = "query_references_fragment"
RC_QUERY_LANG = "query_language_mismatch"
RC_QUERY_DUP = "query_duplicate"
RC_QUERY_NO_EVIDENCE = "query_missing_evidence"
# query 句式同质化：同一文档内多条 query 用同一个开场套路（需求要求"句式类型不能过于重复"）。
# 与"正文重复"(RC_QUERY_DUP) 是两码事——正文可以不重样，但都是"请根据本文档…"。
RC_QUERY_STYLE_REPEATED = "query_style_repeated"
RC_CONTENT_AUDIT_REJECTED = "content_audit_rejected"
RC_SOURCE_READONLY = "source_is_read_only"
# 学科范围门禁：模型判定的学科不在策略声明的 P0∪P1 集合内（如心理学/社会学/艺术学）。
# 需求 3.1 要求"学科属于 P0 或 P1 范围"是硬条件，不是仅用于展示的配比维度。
RC_SUBJECT_OUT_OF_SCOPE = "subject_out_of_scope"


# reason_codes 是一条完整的判定轨迹，里面同时包含"通过了哪些闸门"和"最终因为什么被拒"。
# 报告如果把它们混在一起统计，就会出现「淘汰原因：extension_supported 37 次」这种
# 自相矛盾的表格（第一次真实 Pilot 的覆盖报告就是这样）。所以显式分成三类。
POSITIVE_REASON_CODES: frozenset[str] = frozenset({
    RC_OK, RC_ACQUIRED, RC_EXT_SUPPORTED, RC_SIZE_WITHIN_LIMIT, RC_LANG_SUPPORTED,
    RC_EXTRACT_OK, RC_DUP_UNIQUE,
})

# 信息性标记：既不是通过也不是拒绝，只是提醒后续要走别的流程
INFORMATIONAL_REASON_CODES: frozenset[str] = frozenset({
    "requires_full_text_review", "long_document", "content_already_stored",
    "resumed_existing_artifact", RC_SIZE_UNKNOWN_REMOTE, "mime_verdict",
})


def deciding_reason(reason_codes: Sequence[str]) -> str | None:
    """从判定轨迹里取出真正决定结论的那一条（最后一条非正向、非信息性原因）。

    报告、CSV 和 explain 都用它，避免把"通过了大小检查"当成淘汰原因展示。
    """
    for code in reversed(list(reason_codes or ())):
        text = str(code)
        head = text.split("=", 1)[0]
        if head in POSITIVE_REASON_CODES or head in INFORMATIONAL_REASON_CODES:
            continue
        if head.startswith("mime_verdict"):
            continue
        return text
    return None


def is_explanatory_reason(code: str) -> bool:
    """该原因码是否应该出现在"淘汰/复核原因分布"统计里。"""
    head = str(code).split("=", 1)[0]
    if head in POSITIVE_REASON_CODES or head in INFORMATIONAL_REASON_CODES:
        return False
    return not head.startswith("mime_verdict")


@dataclass(frozen=True)
class RunConfig:
    """一次运行的完整配置。

    字段含义与两周计划 6.1 一致；额外字段用于承载业务硬约束
    （语言配额、题量约束、字符阈值），避免这些规则散落在代码里。
    """

    run_id: str
    source_folders: tuple[str, ...]
    allowed_extensions: tuple[str, ...]
    max_size_mb: dict[str, int]
    target_languages: tuple[str, ...]
    queries_per_document: int
    dry_run: bool
    approval_mode: str
    llm_enabled: bool
    # --- 扩展（业务硬约束）---
    schema_version: str = CONTRACT_VERSION
    sources: tuple[dict[str, Any], ...] = ()
    question_count_min: int = 2
    question_count_max: int = 11
    preferred_question_counts: tuple[int, ...] = (2, 3, 5, 7, 9)
    min_documents_per_language: int = 20
    full_text_review_threshold_chars: int = 50000
    online_types_pending_size: tuple[str, ...] = ("sheet", "slides", "docx", "bitable", "wiki")
    workspace_dir: str = ""
    llm_config_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunConfig":
        kwargs: dict[str, Any] = {}
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        for key, value in data.items():
            if key not in known:
                continue
            if key in ("source_folders", "allowed_extensions", "target_languages",
                       "preferred_question_counts", "online_types_pending_size"):
                kwargs[key] = tuple(value or ())
            elif key == "sources":
                kwargs[key] = tuple(dict(s) for s in (value or ()))
            elif key == "max_size_mb":
                kwargs[key] = {str(k).lower(): int(v) for k, v in (value or {}).items()}
            else:
                kwargs[key] = value
        return cls(**kwargs)


@dataclass(frozen=True)
class SourceItem:
    """云盘/本地盘点到的一条资源记录。

    remote_size 允许为 None：飞书 drive files list 与 metas batch_query
    都不返回文件大小（已在 Task 0 实测确认），此时大小只能在下载后测量。
    """

    source_name: str
    source_folder_path: str
    item_token: str
    name: str
    item_type: str
    extension: str | None = None
    mime_type: str | None = None
    remote_size: int | None = None
    remote_modified_time: str | None = None
    url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourceItem":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs.setdefault("metadata", {})
        return cls(**kwargs)

    @property
    def source_path(self) -> str:
        """人类可读的溯源路径：<源文件夹名>/<相对路径>。"""
        rel = self.metadata.get("relative_path") or self.name
        return f"{self.source_name}/{rel}"


@dataclass(frozen=True)
class RunResult:
    """所有能力统一的返回结构（计划 6.3）。"""

    status: str
    run_id: str
    summary: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[dict[str, Any], ...] = ()
    review_items: tuple[dict[str, Any], ...] = ()
    errors: tuple[dict[str, Any], ...] = ()
    next_action: str | None = None
    capability: str | None = None
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.status not in RUN_STATUSES:
            raise ValueError(f"非法 RunResult.status: {self.status!r}，允许值 {RUN_STATUSES}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RunResult":
        kwargs: dict[str, Any] = {}
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        for key, value in data.items():
            if key not in known:
                continue
            if key in ("artifacts", "review_items", "errors"):
                kwargs[key] = tuple(dict(v) for v in (value or ()))
            else:
                kwargs[key] = value
        return cls(**kwargs)

    def to_json(self, indent: int | None = 2) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)


_SAFE_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")


def safe_item_id(token: str) -> str:
    """把资源 token 归一成可以安全当作文件名/主键的 item_id。

    为什么需要：本地目录适配器用绝对路径当 token，直接拿去做文件名会
    超出 Windows MAX_PATH（260），实测会抛 FileNotFoundError。
    飞书 token 本身是 27 位字母数字，走快路径保持原样，方便人工对照。
    """
    text = str(token or "")
    if _SAFE_ID_RE.fullmatch(text):
        return text
    return "id-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def artifact_record(path: str, kind: str, **extra: Any) -> dict[str, Any]:
    """统一 artifact 记录格式，便于报告与 MCP 透传。"""
    rec: dict[str, Any] = {"path": path, "kind": kind}
    rec.update(extra)
    return rec


def error_record(stage: str, message: str, **extra: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {"stage": stage, "message": message}
    rec.update(extra)
    return rec


def summarize(items: Sequence[Mapping[str, Any]], key: str = "status") -> dict[str, int]:
    """按某个字段做计数，报告与 CLI 摘要共用。"""
    out: dict[str, int] = {}
    for it in items:
        val = str(it.get(key, "unknown"))
        out[val] = out.get(val, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))
