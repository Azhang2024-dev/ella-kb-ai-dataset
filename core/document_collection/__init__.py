"""文档采集与筛选工具包。

对外只暴露三个东西：
- CoreService：八个能力的门面；
- load_policy / CollectionPolicy：业务规则入口；
- 契约常量：状态名、能力名。

任何宿主（CLI / Skill / 未来的 MCP / Plugin）都从这里进，
不要直接 import 子模块的内部函数。
"""

from __future__ import annotations

__version__ = "0.1.0"

from .config import CollectionPolicy, PolicyError, describe_policy, load_policy, validate_policy
from .contracts import (
    CAPABILITIES,
    CAPABILITY_NAMES,
    CLI_COMMANDS,
    CONTRACT_VERSION,
    FORBIDDEN_SIDE_EFFECTS,
    RUN_STATUSES,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_NEEDS_REVIEW,
    STATUS_PARTIAL,
    RunConfig,
    RunResult,
    SourceItem,
)
from .service import CoreService, build_service, make_run_id
from .metadata import infer_education_level, infer_subject, metadata_hints

__all__ = [
    "CAPABILITIES",
    "CAPABILITY_NAMES",
    "CLI_COMMANDS",
    "CONTRACT_VERSION",
    "CollectionPolicy",
    "CoreService",
    "FORBIDDEN_SIDE_EFFECTS",
    "PolicyError",
    "RUN_STATUSES",
    "RunConfig",
    "RunResult",
    "SourceItem",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_NEEDS_REVIEW",
    "STATUS_PARTIAL",
    "__version__",
    "build_service",
    "describe_policy",
    "infer_education_level",
    "infer_subject",
    "load_policy",
    "make_run_id",
    "metadata_hints",
    "validate_policy",
]
