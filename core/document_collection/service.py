"""CoreService：八个能力的统一门面。

CLI、未来的 MCP Server 和 Plugin 都只调这一层，保证：
- 同一个 run_id 下所有阶段共享同一个 SQLite 状态；
- 每个能力都返回统一的 RunResult（status / summary / artifacts / review_items / errors / next_action）；
- next_action 是稳定契约，未来 Agent Loop 可以直接照着走，不需要读代码。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import llm as llm_module
from .acquisition import AcquireOptions, run_acquire
from .config import CollectionPolicy, load_policy
from .contracts import (
    ITEM_ACQUIRED,
    RUN_STATUSES,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_NEEDS_REVIEW,
    STATUS_PARTIAL,
    RunResult,
    artifact_record,
    error_record,
)
from .deduplication import DedupOptions, run_deduplicate
from .extraction import ExtractionOptions, run_extract
from .ingest import IngestOptions, run_ingest
from .inventory import InventoryOptions, run_inventory
from .query_generation import QueryOptions, run_query_generation
from .question_generation import QuestionOptions, run_question_generation
from .delivery import DeliverOptions, run_deliver
from .release import ReleaseOptions, run_release
from .material_type import MaterialTypeOptions, run_material_type
from .reporting import run_report
from .screening import ScreeningOptions, run_screen
from .state import StateStore
from . import winpath

LogFn = Callable[[str], None]


def _default_log(msg: str) -> None:
    print(msg, flush=True)


def make_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}"


class CoreService:
    """文档采集与筛选的核心服务。"""

    def __init__(self, policy: CollectionPolicy, *, workspace_root: str | None = None,
                 run_id: str | None = None, adapter: Any | None = None,
                 llm: Any | None = None, log: LogFn = _default_log,
                 replay_dir: str | None = None, record_dir: str | None = None,
                 concurrency: int = 1):
        self.policy = policy
        self.run_id = run_id or make_run_id()
        self.concurrency = max(1, int(concurrency))
        self.workspace_root = os.path.abspath(workspace_root or policy.run_defaults.get(
            "workspace_dir", os.path.join("output", "document_collection")))
        self.workspace_dir = os.path.join(self.workspace_root, self.run_id)
        winpath.makedirs(self.workspace_dir)
        self.log = log
        self._adapter_override = adapter
        self._llm_override = llm
        self.replay_dir = replay_dir
        self.record_dir = record_dir
        self.store = StateStore(os.path.join(self.workspace_dir, "state.sqlite3"))
        self.stage_summaries: dict[str, Any] = {}
        self.store.start_run(self.run_id, self.workspace_dir,
                             config={"policy_path": policy.path,
                                     "workspace_root": self.workspace_root},
                             policy_digest=str(hash(json.dumps(policy.raw, sort_keys=True,
                                                               ensure_ascii=False))))

    # -- 资源 -------------------------------------------------------------
    def close(self) -> None:
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "CoreService":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def db_path(self) -> str:
        return self.store.db_path

    def build_adapter(self) -> Any:
        """按策略构造数据源适配器；支持录制/回放。"""
        if self._adapter_override is not None:
            return self._adapter_override
        access = self.policy.section("source_access")
        kind = str(access.get("adapter", "lark_cli")).lower()
        if kind == "local_files":
            from .adapters.local_files import LocalFilesAdapter
            return LocalFilesAdapter(access.get("root"))
        from .adapters.lark_cli import LarkCliAdapter, subprocess_runner
        runner: Any = subprocess_runner
        if self.replay_dir:
            from .adapters.recorded import ReplayRunner
            runner = ReplayRunner(self.replay_dir)
            self.log(f"[adapter] 使用回放录像：{self.replay_dir}")
        elif self.record_dir:
            from .adapters.recorded import RecordingRunner
            runner = RecordingRunner(subprocess_runner, self.record_dir)
            self.log(f"[adapter] 录制真实响应到：{self.record_dir}")
        return LarkCliAdapter(
            access.get("lark_cli_path") or None,
            timeout=int(access.get("request_timeout_sec", 180)),
            max_retries=int(access.get("max_retries", 3)),
            backoff=tuple(access.get("retry_backoff_sec", (2, 5, 10))),
            allowlist=tuple(tuple(str(c).split()) for c in self.policy.allowlisted_lark_commands),
            runner=runner,
            inter_call_delay=float(access.get("inter_call_delay_sec", 0.2)),
            identity=str(access.get("identity", "user")),
            workdir=self.workspace_dir,
        )

    def build_llm(self, force_enabled: bool | None = None) -> Any:
        """构造模型适配器。

        force_enabled=True 时忽略策略里的 llm.enabled（CLI 的 --semantic / --llm 走这条路），
        否则会出现"命令行明确要求启用、却被策略静默关掉"的矛盾行为。
        force_enabled=False 时强制不用模型。None 表示跟随策略。
        """
        if self._llm_override is not None:
            return self._llm_override
        if force_enabled is False:
            return llm_module.OfflineLLMAdapter("cli_disabled_llm")
        llm_policy = dict(self.policy.llm)
        if force_enabled is True:
            llm_policy["enabled"] = True
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        def on_call(record: Mapping[str, Any]) -> None:
            self.store.log_llm_call(
                item_id=record.get("item_id"), scene=str(record.get("scene") or ""),
                model=str(record.get("model") or ""), ok=bool(record.get("ok")),
                prompt_chars=int(record.get("prompt_chars") or 0),
                output_chars=int(record.get("output_chars") or 0),
                latency_ms=int(record.get("latency_ms") or 0),
                error=record.get("error"),
            )

        return llm_module.build_adapter(llm_policy, repo_root=repo_root, on_call=on_call)

    # -- 结果封装 ---------------------------------------------------------
    def _result(self, capability: str, status: str, summary: Mapping[str, Any], *,
                artifacts: Sequence[Mapping[str, Any]] = (),
                review_items: Sequence[Mapping[str, Any]] = (),
                errors: Sequence[Mapping[str, Any]] = (),
                next_action: str | None = None) -> RunResult:
        if status not in RUN_STATUSES:
            raise ValueError(f"非法状态 {status}")
        self.stage_summaries[capability] = dict(summary)
        self.store.touch_run(self.run_id, self.stage_summaries)
        self._dump_result(capability, status, summary, artifacts, errors, next_action)
        return RunResult(
            status=status, run_id=self.run_id, summary=dict(summary),
            artifacts=tuple(dict(a) for a in artifacts),
            review_items=tuple(dict(r) for r in review_items),
            errors=tuple(dict(e) for e in errors),
            next_action=next_action, capability=capability,
        )

    def _dump_result(self, capability: str, status: str, summary: Mapping[str, Any],
                     artifacts: Sequence[Mapping[str, Any]],
                     errors: Sequence[Mapping[str, Any]], next_action: str | None) -> None:
        path = os.path.join(self.workspace_dir, f"result_{capability.split('.')[-1]}.json")
        payload = {
            "capability": capability, "status": status, "run_id": self.run_id,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "summary": dict(summary), "artifacts": [dict(a) for a in artifacts],
            "errors": [dict(e) for e in errors], "next_action": next_action,
        }
        with winpath.open_write(path, encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)

    # -- 能力 0：ingest（本地优先入口）-----------------------------------
    def ingest(self, *, root: str, extensions: Sequence[str] = (),
               limit: int | None = None, copy: bool = False,
               recursive: bool = True, source_name: str | None = None,
               include_hidden: bool = False) -> RunResult:
        """直接登记用户已下载好的本地文档，跳过云盘 inventory/acquire。

        这是本地优先链路的第一步，产出与 inventory+acquire 等价：
        条目状态落到 acquired，后续 extract/deduplicate/screen 无需改动即可接上。
        默认原地引用源文件（只读，不复制、不移动、不改名）。
        """
        capability = "collection.ingest"
        opts = IngestOptions(limit=limit, extensions=tuple(extensions), copy=copy,
                             recursive=recursive, source_name=source_name,
                             include_hidden=include_hidden)
        try:
            summary = run_ingest(policy=self.policy, store=self.store,
                                 workspace_dir=self.workspace_dir, run_id=self.run_id,
                                 root=root, options=opts, log=self.log)
        except Exception as exc:  # noqa: BLE001
            return self._result(capability, STATUS_FAILED, {"message": str(exc)[:400]},
                                errors=[error_record("ingest", f"{type(exc).__name__}: {exc}")],
                                next_action="检查 --dir 路径是否正确")
        counts = summary.get("status_counts", {})
        status = STATUS_COMPLETED
        if counts.get("ingest_failed") or counts.get("needs_review"):
            status = STATUS_PARTIAL
        artifacts = (artifact_record(summary["report_path"], "ingest_report"),)
        next_action = "collection.extract" if counts.get(ITEM_ACQUIRED) else "collection.report"
        return self._result(capability, status, summary, artifacts=artifacts,
                            next_action=next_action)

    # -- 能力 1：inventory ------------------------------------------------
    def inventory(self, *, page_size: int | None = None, max_depth: int | None = None,
                  per_source_limit: int | None = None,
                  include_subfolders: Sequence[str] = (),
                  exclude_subfolders: Sequence[str] = (),
                  max_folders: int | None = None,
                  source_names: Sequence[str] = ()) -> RunResult:
        capability = "collection.inventory"
        access = self.policy.section("source_access")
        opts = InventoryOptions(
            page_size=int(page_size or access.get("page_size", 200)),
            max_depth=int(max_depth or access.get("max_depth", 10)),
            per_source_limit=per_source_limit,
            include_subfolders=tuple(include_subfolders),
            exclude_subfolders=tuple(exclude_subfolders),
            max_folders=max_folders,
        )
        sources = [s for s in self.policy.sources
                   if not source_names or s["name"] in set(source_names)]
        if not sources:
            return self._result(capability, STATUS_FAILED, {"message": "没有匹配的采集源"},
                                errors=[error_record("inventory", f"source_names={list(source_names)} 无匹配")],
                                next_action="修正 --source 参数或策略 sources")
        adapter = self.build_adapter()
        try:
            report = run_inventory(adapter=adapter, sources=sources, policy=self.policy,
                                   store=self.store, workspace_dir=self.workspace_dir,
                                   run_id=self.run_id, options=opts, log=self.log)
        except Exception as exc:  # noqa: BLE001
            return self._result(capability, STATUS_FAILED, {"message": str(exc)[:400]},
                                errors=[error_record("inventory", f"{type(exc).__name__}: {exc}")],
                                next_action="collection.acquire")
        summary = {
            "sources": report.sources,
            "raw_occurrences": report.raw_occurrences,
            "unique_items": report.unique_items,
            "duplicate_occurrences": report.duplicate_occurrences,
            "cross_source_duplicate_tokens": report.cross_source_duplicate_tokens,
            "type_counts": report.type_counts,
            "error_count": len(report.errors),
        }
        status = STATUS_COMPLETED if not report.errors else STATUS_PARTIAL
        artifacts = tuple(artifact_record(a["path"], a["kind"],
                                          **{k: v for k, v in a.items() if k not in ("path", "kind")})
                          for a in report.artifacts)
        # inventory 的错误字典自带 stage / message 键，直接 **展开会和
        # error_record(stage, message, **extra) 的位置参数撞车（TypeError）。
        errors = tuple(error_record("inventory", e.get("message", ""), **{
            k: v for k, v in e.items() if k not in ("message", "stage")})
            for e in report.errors)
        return self._result(capability, status, summary, artifacts=artifacts, errors=errors,
                            next_action="collection.acquire")

    # -- 能力 2：acquire --------------------------------------------------
    def acquire(self, *, limit: int | None = None, extensions: Sequence[str] = (),
                item_types: Sequence[str] = (), source_names: Sequence[str] = (),
                top_folders: Sequence[str] = (), languages: Sequence[str] = (),
                dry_run: bool = False, include_statuses: Sequence[str] = (),
                sample: int | None = None, sample_strata: Sequence[str] = (),
                sample_seed: int | None = None) -> RunResult:
        capability = "collection.acquire"
        opts = AcquireOptions(
            limit=limit, extensions=tuple(extensions), item_types=tuple(item_types),
            source_names=tuple(source_names),
            top_folders=tuple(top_folders), languages=tuple(languages),
            dry_run=dry_run,
            include_statuses=tuple(include_statuses) or AcquireOptions.include_statuses,
            sample=sample,
            sample_strata=tuple(sample_strata) or AcquireOptions.sample_strata,
            sample_seed=AcquireOptions.sample_seed if sample_seed is None else int(sample_seed),
        )
        adapter = self.build_adapter()
        summary = run_acquire(adapter=adapter, policy=self.policy, store=self.store,
                              workspace_dir=self.workspace_dir, run_id=self.run_id,
                              options=opts, log=self.log)
        counts = summary.get("status_counts", {})
        status = STATUS_COMPLETED
        if counts.get("acquire_failed") or counts.get("pending_export"):
            status = STATUS_PARTIAL
        if dry_run:
            status = STATUS_NEEDS_REVIEW
        artifacts = (artifact_record(summary["report_path"], "acquisition_report"),)
        return self._result(capability, status, summary, artifacts=artifacts,
                            next_action="collection.extract")

    # -- 能力 3：extract --------------------------------------------------
    def extract(self, *, limit: int | None = None, extensions: Sequence[str] = (),
                include_statuses: Sequence[str] = (), item_ids: Sequence[str] = (),
                concurrency: int | None = None) -> RunResult:
        capability = "collection.extract"
        opts = ExtractionOptions(
            limit=limit, extensions=tuple(extensions),
            include_statuses=tuple(include_statuses) or ExtractionOptions.include_statuses,
            item_ids=tuple(item_ids),
            concurrency=self.concurrency if concurrency is None else int(concurrency),
        )
        summary = run_extract(policy=self.policy, store=self.store,
                              workspace_dir=self.workspace_dir, run_id=self.run_id,
                              options=opts, log=self.log)
        counts = summary.get("status_counts", {})
        status = STATUS_COMPLETED if not counts.get("extraction_failed") else STATUS_PARTIAL
        artifacts = (artifact_record(summary["report_path"], "extraction_report"),)
        return self._result(capability, status, summary, artifacts=artifacts,
                            next_action="collection.deduplicate")

    # -- 能力 4：deduplicate ---------------------------------------------
    def deduplicate(self, *, limit: int | None = None, tolerance: float | None = None,
                    include_statuses: Sequence[str] = (),
                    reject_duplicates: bool = True) -> RunResult:
        capability = "collection.deduplicate"
        opts = DedupOptions(limit=limit,
                            include_statuses=tuple(include_statuses),
                            reject_exact_duplicates=bool(reject_duplicates),
                            near_duplicate_char_tolerance=float(
                                tolerance if tolerance is not None else 0.02))
        summary = run_deduplicate(policy=self.policy, store=self.store,
                                  workspace_dir=self.workspace_dir, run_id=self.run_id,
                                  options=opts, log=self.log)
        artifacts = tuple(artifact_record(p, "dedup_artifact") for p in summary.get("artifacts", []))
        return self._result(capability, STATUS_COMPLETED, summary, artifacts=artifacts,
                            next_action="collection.screen")

    # -- 能力 5：screen ---------------------------------------------------
    def screen(self, *, limit: int | None = None, semantic: bool | None = None,
               include_statuses: Sequence[str] = (),
               include_unprocessed: bool = False,
               concurrency: int | None = None) -> RunResult:
        capability = "collection.screen"
        run_semantic = self.policy.llm_enabled if semantic is None else bool(semantic)
        # semantic=None 跟随策略；True/False 是调用方的明确意图，必须穿透策略开关
        llm = self.build_llm(force_enabled=semantic) if run_semantic else None
        if run_semantic and (llm is None or not getattr(llm, "available", False)):
            self.log("[screen] 需要语义复核但模型不可用，降级为仅硬规则；"
                     "语义项会全部转 needs_review，不会自动放行")
            run_semantic = False
        opts = ScreeningOptions(limit=limit, run_semantic=run_semantic,
                                include_statuses=tuple(include_statuses),
                                include_unprocessed=include_unprocessed,
                                concurrency=self.concurrency if concurrency is None else int(concurrency))
        summary = run_screen(policy=self.policy, store=self.store,
                             workspace_dir=self.workspace_dir, run_id=self.run_id,
                             options=opts, llm=llm, log=self.log)
        counts = summary.get("status_counts", {})
        status = STATUS_COMPLETED
        if counts.get("needs_review"):
            status = STATUS_NEEDS_REVIEW
        review_items = tuple(
            {"item_id": r["item_id"], "name": r.get("name"), "source_path": r.get("source_path"),
             "reason_codes": r.get("reason_codes")}
            for r in self._review_rows(limit=50)
        )
        artifacts = tuple(artifact_record(p, "screen_artifact") for p in summary.get("artifacts", []))
        next_action = "collection.generate_queries" if counts.get("accepted") else "collection.report"
        return self._result(capability, status, summary, artifacts=artifacts,
                            review_items=review_items, next_action=next_action)

    def _review_rows(self, limit: int = 50) -> list[dict[str, Any]]:
        path = os.path.join(self.workspace_dir, "human_review_queue.csv")
        if not winpath.isfile(path):
            return []
        import csv as _csv
        with winpath.open_read(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(_csv.DictReader(fh))
        out: list[dict[str, Any]] = []
        for row in rows[:limit]:
            codes = str(row.get("reason_codes") or "").split(";")
            out.append({"item_id": row.get("item_id"), "name": row.get("name"),
                        "source_path": row.get("source_path"),
                        "reason_codes": [c for c in codes if c]})
        return out

    # -- 能力 6：generate_queries ----------------------------------------
    def generate_queries(self, *, limit: int | None = None,
                         include_statuses: Sequence[str] = ("accepted",),
                         item_ids: Sequence[str] = (),
                         enable_llm: bool | None = None) -> RunResult:
        capability = "collection.generate_queries"
        llm = self.build_llm(force_enabled=True if enable_llm else None)
        if not getattr(llm, "available", False):
            return self._result(
                capability, STATUS_NEEDS_REVIEW,
                {"message": "模型不可用，未生成任何 query（不伪造结果）",
                 "total_documents": 0, "total_queries": 0},
                errors=[error_record("query", "LLMUnavailable: 检查 config/llm_config.json "
                                              "是否存在且含 llm_api_key，或用 --llm 显式启用")],
                next_action="配置可用模型后重跑 collection.generate_queries",
            )
        opts = QueryOptions(limit=limit, include_statuses=tuple(include_statuses),
                            item_ids=tuple(item_ids), concurrency=self.concurrency)
        summary = run_query_generation(policy=self.policy, store=self.store,
                                       workspace_dir=self.workspace_dir, run_id=self.run_id,
                                       llm=llm, options=opts, log=self.log)
        counts = summary.get("status_counts", {})
        status = STATUS_COMPLETED if counts.get("needs_review", 0) == 0 else STATUS_NEEDS_REVIEW
        artifacts = tuple(artifact_record(p, "query_artifact") for p in summary.get("artifacts", []))
        return self._result(capability, status, summary, artifacts=artifacts,
                            next_action="collection.generate_questions")

    # -- 能力 6b：generate_questions（参考题目）--------------------------
    def generate_questions(self, *, limit: int | None = None,
                           item_ids: Sequence[str] = (),
                           enable_llm: bool | None = None) -> RunResult:
        capability = "collection.generate_questions"
        if not self.policy.question_generation.get("enabled", True):
            return self._result(capability, STATUS_NEEDS_REVIEW,
                                {"message": "策略里 question_generation.enabled=false"},
                                next_action="在 policy 中启用 question_generation")
        llm = self.build_llm(force_enabled=True if enable_llm else None)
        if not getattr(llm, "available", False):
            return self._result(
                capability, STATUS_NEEDS_REVIEW,
                {"message": "模型不可用，未生成任何题目（不伪造结果）",
                 "total_documents": 0, "total_questions": 0},
                errors=[error_record("questions", "LLMUnavailable: 检查 config/llm_config.json "
                                                 "是否存在且含 llm_api_key，或用 --llm 显式启用")],
                next_action="配置可用模型后重跑 collection.generate_questions",
            )
        opts = QuestionOptions(limit=limit, item_ids=tuple(item_ids),
                               concurrency=self.concurrency)
        summary = run_question_generation(policy=self.policy, store=self.store,
                                         workspace_dir=self.workspace_dir, run_id=self.run_id,
                                         llm=llm, options=opts, log=self.log)
        counts = summary.get("status_counts", {})
        status = STATUS_COMPLETED if counts.get("needs_review", 0) == 0 else STATUS_NEEDS_REVIEW
        artifacts = tuple(artifact_record(p, "question_artifact")
                          for p in summary.get("artifacts", []))
        return self._result(capability, status, summary, artifacts=artifacts,
                            next_action="collection.report")

    # -- 能力 6c：deliver（选取 + 真实名落地 + 交付表）--------------------
    def deliver(self, *, per_language: int | None = 30,
                languages: Sequence[str] = (),
                item_ids: Sequence[str] = (),
                require_subject_in_scope: bool = True) -> RunResult:
        """从 accepted 池选出交付集，落真实文件名，出 sheet4 格式交付表。

        与 report 的区别：report 出的是"全池的覆盖/淘汰报告"，deliver 出的是
        "最终交给需求方的那一份"——每语种定量、学科合规、真实文件名可点开、一行一条 query。
        """
        capability = "collection.deliver"
        opts = DeliverOptions(
            per_language=per_language, languages=tuple(languages),
            item_ids=tuple(item_ids),
            require_subject_in_scope=bool(require_subject_in_scope),
        )
        summary = run_deliver(policy=self.policy, store=self.store,
                             workspace_dir=self.workspace_dir, run_id=self.run_id,
                             options=opts, log=self.log)
        counts = summary.get("by_language") or {}
        status = STATUS_COMPLETED if counts else STATUS_NEEDS_REVIEW
        artifacts = tuple(artifact_record(p, "delivery_artifact")
                          for p in summary.get("artifacts", []))
        return self._result(capability, status, summary, artifacts=artifacts,
                            next_action="collection.report")

    # -- 能力：人工放行 ---------------------------------------------------
    def release(self, *, item_ids: Sequence[str] = (), reason: str = "",
                released_by: str = "human", dry_run: bool = False) -> RunResult:
        """把人工复核确认的 needs_review 条目放行为 accepted（追加决策，不覆盖历史）。"""
        capability = "collection.release"
        opts = ReleaseOptions(
            item_ids=tuple(item_ids), reason=reason, released_by=released_by,
            dry_run=bool(dry_run),
        )
        summary = run_release(policy=self.policy, store=self.store,
                              workspace_dir=self.workspace_dir, run_id=self.run_id,
                              options=opts, log=self.log)
        released = int(summary.get("released_total") or 0)
        rejected = int(summary.get("rejected_total") or 0)
        if rejected:
            # 有拒收就不算干净完成：需要人工看看为什么没放进去
            status = STATUS_NEEDS_REVIEW
        else:
            status = STATUS_COMPLETED
        # _emit 的人读分支只打印白名单键，这里把放行计数塞进已识别的键名
        summary["total_documents"] = released
        return self._result(capability, status, summary,
                            next_action="collection.deliver")

    # -- 能力：资料类型标注 -----------------------------------------------
    def material_type(self, *, item_ids: Sequence[str] = (), dry_run: bool = False,
                      use_llm: bool = False) -> RunResult:
        """给交付表「类型」列标注（教材/课堂笔记/试卷/讲义/作业习题），追加落库。"""
        capability = "collection.material_type"
        opts = MaterialTypeOptions(item_ids=tuple(item_ids), dry_run=bool(dry_run),
                                   use_llm=bool(use_llm))
        llm = self.build_llm(force_enabled=True) if use_llm else None
        if use_llm and not getattr(llm, "available", False):
            return self._result(capability, STATUS_NEEDS_REVIEW, {
                "total_requested": 0, "labeled_total": 0, "unresolved_total": 0,
                "dry_run": bool(dry_run), "by_type": {}, "by_genre": {},
            }, errors=[error_record(
                "llm_unavailable",
                "显式要求模型兜底，但适配器不可用：检查 config/llm_config.json "
                "是否存在且含 llm_api_key，或用不带 --llm 的规则模式",
            )], next_action="collection.material_type")
        summary = run_material_type(policy=self.policy, store=self.store,
                                    workspace_dir=self.workspace_dir,
                                    run_id=self.run_id, options=opts,
                                    llm=llm, log=self.log)
        unresolved = int(summary.get("unresolved_total") or 0)
        # 有判不出来的就落 needs_review：标注结果不完整，需要人工看一眼，不粉饰。
        status = STATUS_NEEDS_REVIEW if unresolved else STATUS_COMPLETED
        # _emit 的人读分支只打印白名单键，这里把标注计数塞进已识别的键名
        summary["total_documents"] = int(summary.get("labeled_total") or 0)
        return self._result(capability, status, summary,
                            next_action="collection.deliver")

    # -- 能力 7：report ---------------------------------------------------
    def report(self) -> RunResult:
        capability = "collection.report"
        summary = run_report(policy=self.policy, store=self.store,
                             workspace_dir=self.workspace_dir, run_id=self.run_id)
        # 必须浅拷贝：_result() 会把本 summary 写回 self.stage_summaries["collection.report"]，
        # 直接塞同一个 dict 会形成自引用，json.dumps 报 Circular reference detected。
        summary["stage_summaries"] = {k: dict(v) for k, v in self.stage_summaries.items()}
        facts = summary.get("facts") or {}
        status = STATUS_COMPLETED
        if facts.get("status_counts", {}).get("needs_review"):
            status = STATUS_NEEDS_REVIEW
        artifacts = tuple(artifact_record(p, "report_artifact") for p in summary.get("artifacts", []))
        return self._result(capability, status, summary, artifacts=artifacts, next_action=None)

    # -- 诊断：验收门禁自检（不属于 8 个契约能力，只读） ------------------
    def audit(self, *, compare_run_dir: str | None = None,
              skill_dir: str | None = None) -> RunResult:
        """机械化执行计划第 9 节的 5 个验收检查点。"""
        from .audit import audit_run, format_audit_report, write_audit_report

        capability = "collection.audit"
        report = audit_run(policy=self.policy, store=self.store,
                           workspace_dir=self.workspace_dir, run_id=self.run_id,
                           compare_run_dir=compare_run_dir, skill_dir=skill_dir)
        path = os.path.join(self.workspace_dir, "audit_report.json")
        write_audit_report(path, report)
        md_path = os.path.join(self.workspace_dir, "audit_report.md")
        text = format_audit_report(report)
        with winpath.open_write(md_path, encoding="utf-8") as fh:
            fh.write(text)
        self.log(text)
        summary = {
            "passed": report.passed,
            "total_checks": len(report.checks),
            "failed_checks": len(report.failed),
            "failed": [{"checkpoint": c.checkpoint, "name": c.name, "detail": c.detail}
                       for c in report.failed],
        }
        errors = tuple(error_record(c.checkpoint, c.name, detail=c.detail,
                                    evidence=c.evidence) for c in report.failed)
        status = STATUS_COMPLETED if report.passed else STATUS_FAILED
        artifacts = (artifact_record(path, "audit_report_json"),
                     artifact_record(md_path, "audit_report_md"))
        return self._result(capability, status, summary, artifacts=artifacts,
                            errors=errors, next_action=None)

    # -- 能力 8：resume ---------------------------------------------------
    def resume(self) -> RunResult:
        """报告当前 run 的进度，并给出下一步该跑哪个能力。"""
        capability = "collection.resume"
        counts = self.store.count_items()
        artifacts = self.store.list_artifacts()
        extractions = self.store.list_extractions()
        decisions = self.store.latest_decisions("screen")
        from collections import Counter as _Counter
        query_status = dict(_Counter(str(q.get("status")) for q in self.store.all_queries()))
        query_status["total"] = len(self.store.all_queries())
        question_rows = self.store.all_questions()
        question_status = dict(_Counter(str(q.get("status")) for q in question_rows))
        question_status["total"] = len(question_rows)
        artifact_status = {}
        for row in artifacts:
            artifact_status[str(row.get("status"))] = artifact_status.get(str(row.get("status")), 0) + 1
        extraction_status = {}
        for row in extractions:
            extraction_status[str(row.get("status"))] = extraction_status.get(str(row.get("status")), 0) + 1
        screen_status = {}
        for row in decisions.values():
            screen_status[str(row.get("status"))] = screen_status.get(str(row.get("status")), 0) + 1

        next_action = _decide_next(counts, artifact_status, extraction_status, screen_status,
                                   queries=query_status, questions=question_status)
        summary = {
            "run_id": self.run_id,
            "workspace_dir": self.workspace_dir,
            "item_status_counts": counts,
            "artifact_status_counts": artifact_status,
            "extraction_status_counts": extraction_status,
            "screen_status_counts": screen_status,
            "queries_total": len(self.store.all_queries()),
            "questions_total": len(question_rows),
            "llm_stats": self.store.llm_call_stats(),
            "stage_summaries": json.loads((self.store.get_run(self.run_id) or {}).get(
                "stage_state_json") or "{}"),
        }
        return self._result(capability, STATUS_COMPLETED, summary, next_action=next_action)


    def status(self) -> RunResult:
        """只读诊断：阶段×状态矩阵，给人和编排者一眼看清流水线全貌。

        与 resume 的区别：resume 回答"下一步跑什么"，status 回答"每一步各有多少条、
        处于什么状态"。数据全部来自状态库聚合，不重新计算任何阶段结论。
        """
        capability = "collection.status"
        counts = self.store.count_items()
        artifacts = self.store.list_artifacts()
        extractions = self.store.list_extractions()
        screen_decisions = self.store.latest_decisions("screen")
        queries = self.store.all_queries()
        questions = self.store.all_questions()
        dedup_groups = self.store.dedup_groups()

        def _tally(rows: Iterable[Mapping[str, Any]], key: str) -> dict[str, int]:
            tally: dict[str, int] = {}
            for row in rows:
                value = str(row.get(key))
                tally[value] = tally.get(value, 0) + 1
            return tally

        artifact_status = _tally(artifacts, "status")
        extraction_status = _tally(extractions, "status")
        screen_status = _tally(screen_decisions.values(), "status")
        query_status = _tally(queries, "status")
        question_status = _tally(questions, "status")
        extraction_languages = _tally(
            (row for row in extractions if row.get("language")), "language")

        summary = {
            "run_id": self.run_id,
            "workspace_dir": self.workspace_dir,
            "items": counts,
            "artifacts": artifact_status,
            "extractions": extraction_status,
            "screen": screen_status,
            "queries": {"total": len(queries), **query_status},
            "questions": {"total": len(questions), **question_status},
            "dedup_groups": {"total": len(dedup_groups),
                             "members": sum(len(v) for v in dedup_groups.values())},
            "languages": extraction_languages,
            "llm_stats": self.store.llm_call_stats(),
            "stage_summaries": json.loads((self.store.get_run(self.run_id) or {}).get(
                "stage_state_json") or "{}"),
        }
        next_action = _decide_next(counts, artifact_status, extraction_status, screen_status,
                                   queries=summary["queries"], questions=summary["questions"])
        return self._result(capability, STATUS_COMPLETED, summary, next_action=next_action)


def _decide_next(items: Mapping[str, int], artifacts: Mapping[str, int],
                 extractions: Mapping[str, int], screen: Mapping[str, int],
                 queries: Mapping[str, int] | None = None,
                 questions: Mapping[str, int] | None = None) -> str:
    """确定性的下一步建议，未来 Agent Loop 直接读这个字段。"""
    if not items:
        return "collection.inventory"
    pending_items = items.get("discovered", 0)
    failed_acquire = items.get("acquire_failed", 0)
    pending_export = items.get("pending_export", 0)
    acquired = items.get("acquired", 0)
    if pending_items or failed_acquire:
        return "collection.acquire"
    if acquired:
        return "collection.extract"
    if not extractions:
        return "collection.extract"
    extracted = extractions.get("extracted", 0)
    if extracted and not screen:
        return "collection.deduplicate"
    if screen.get("accepted", 0):
        # 出题分两段：先生成 query，再据 query 生成参考题目
        if queries and (queries.get("total", 0) or queries.get("validated", 0)):
            if not (questions and questions.get("total", 0)):
                return "collection.generate_questions"
            return "collection.report"
        return "collection.generate_queries"
    if pending_export:
        return "collection.acquire（重试 pending_export）"
    return "collection.report"


def build_service(config_path: str | None = None, *, workspace_root: str | None = None,
                  run_id: str | None = None, adapter: Any | None = None,
                  llm: Any | None = None, log: LogFn = _default_log,
                  replay_dir: str | None = None,
                  record_dir: str | None = None,
                  concurrency: int = 1) -> CoreService:
    policy = load_policy(config_path)
    return CoreService(policy, workspace_root=workspace_root, run_id=run_id,
                       adapter=adapter, llm=llm, log=log,
                       replay_dir=replay_dir, record_dir=record_dir,
                       concurrency=concurrency)
