"""命令行入口（argparse，不引入第三方 CLI 框架）。

设计原则
--------
1. 一个能力一个子命令，名字与 capability manifest 一一对应；
2. 所有子命令都能输出统一 RunResult JSON（--json），未来 MCP 直接复用；
3. 退出码可被脚本判定：0 完成 / 2 部分完成 / 3 需人工复核 / 1 失败 / 4 配置错误；
4. CLI 不含业务逻辑，只做参数解析 -> CoreService -> 打印。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Sequence

from . import __version__
from .config import PolicyError, describe_policy, load_policy
from .contracts import STATUS_COMPLETED, STATUS_FAILED, STATUS_NEEDS_REVIEW, STATUS_PARTIAL
from .service import CoreService, build_service, make_run_id
from . import winpath

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PARTIAL = 2
EXIT_NEEDS_REVIEW = 3
EXIT_CONFIG = 4

_STATUS_EXIT = {
    STATUS_COMPLETED: EXIT_OK,
    STATUS_PARTIAL: EXIT_PARTIAL,
    STATUS_NEEDS_REVIEW: EXIT_NEEDS_REVIEW,
    STATUS_FAILED: EXIT_FAILED,
    "cancelled": EXIT_FAILED,
}


def _repo_root() -> str:
    """core/document_collection/cli.py -> 仓库根。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _resolve_run_id(workspace_root: str, run_id: str | None) -> str | None:
    """支持 --run-id @latest：挑最近修改过的那个 run 目录。"""
    if not run_id or run_id != "@latest":
        return run_id
    if not os.path.isdir(workspace_root):
        return None
    candidates = []
    for name in winpath.listdir(workspace_root):
        path = os.path.join(workspace_root, name)
        db = os.path.join(path, "state.sqlite3")
        if winpath.isdir(path) and winpath.isfile(db):
            candidates.append((os.path.getmtime(db), name))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="collection_cli",
        description="Ella 知识库 AI 生题评测集：文档采集与筛选工具（只读采集云盘源）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "本地优先链路（推荐）：\n"
            "  ingest -> extract -> deduplicate -> screen -> query -> questions -> report\n"
            "     collection.ingest            -> ingest\n"
            "云盘链路（可选）：\n"
            "  inventory -> acquire -> extract -> deduplicate -> screen -> query -> questions -> report\n"
            "     collection.inventory         -> inventory\n"
            "     collection.acquire           -> acquire\n"
            "  两条链路在 extract 之后完全共用。\n"
            "  collection.extract          -> extract\n"
            "  collection.deduplicate      -> deduplicate\n"
            "  collection.screen           -> screen\n"
            "  collection.generate_queries -> query\n"
            "  collection.generate_questions -> questions\n"
            "  collection.report           -> report\n"
            "  collection.resume           -> resume\n"
            "\n退出码：0 完成 / 2 部分完成 / 3 需人工复核 / 1 失败（audit 不通过也是 1）"
            " / 4 配置错误\n"
            "只读诊断：resume（下一步）/ status（阶段×状态矩阵）/ explain（单条判定链）"
            " / audit（验收门禁）\n"
            "一致性修复：reconcile（把 query_records.jsonl 回灌状态库）\n"
        ),
    )
    parser.add_argument("--version", action="version",
                        version=f"document_collection {__version__} (contract 0.1)")
    parser.add_argument("--config", default=None,
                        help="策略文件路径（默认 <repo>/config/collection_policy.json）")
    parser.add_argument("--workspace", default=None,
                        help="运行产物根目录（默认取策略 run_defaults.workspace_dir）")
    parser.add_argument("--run-id", default=None,
                        help="运行 ID；@latest 表示复用最近一次运行（续跑）")
    parser.add_argument("--replay-dir", default=None,
                        help="回放录像目录：不发真实请求，用于回归测试")
    parser.add_argument("--record-dir", default=None,
                        help="录制目录：把真实 lark-cli 响应存成录像")
    parser.add_argument("--json", action="store_true", help="以 RunResult JSON 输出")
    parser.add_argument("--quiet", action="store_true", help="只输出结果，不打印过程日志")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="并发 worker 数（抽取/语义复核/生题；默认 1=串行。"
                             "推荐 4-8，本地文件 IO 与模型调用都能受益）")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate-config", help="校验策略文件并打印生效规则")
    p.set_defaults(handler=cmd_validate_config)

    p = sub.add_parser("runs", help="列出工作区里已有的运行")
    p.set_defaults(handler=cmd_runs)

    p = sub.add_parser("ingest",
                       help="本地优先入口：直接登记已下载好的文档（跳过云盘盘点/采集）")
    p.add_argument("--dir", required=True,
                   help="本地文件夹路径（递归遍历其中的文档）")
    p.add_argument("--source-name", default=None,
                   help="来源名（默认取文件夹名），会写进报告与状态库")
    p.add_argument("--extension", action="append", default=[],
                   help="只导入这些扩展名，可重复（默认全部）")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--copy", action="store_true",
                   help="复制到内容寻址的 artifacts/（默认原地引用，不复制不动源文件）")
    p.add_argument("--no-recursive", dest="recursive", action="store_false",
                   help="只导入文件夹第一层，不递归子目录")
    p.add_argument("--include-hidden", action="store_true",
                   help="包含隐藏文件 / Office 临时文件（默认跳过）")
    p.set_defaults(handler=cmd_ingest, recursive=True)

    p = sub.add_parser("inventory", help="递归盘点云盘文件夹（只列举，不下载正文）")
    p.add_argument("--page-size", type=int, default=None)
    p.add_argument("--max-depth", type=int, default=None)
    p.add_argument("--per-source-limit", type=int, default=None,
                   help="每个源最多盘点多少条，用于小批量试跑")
    p.add_argument("--max-folders", type=int, default=None, help="最多下探多少个子文件夹")
    p.add_argument("--source", action="append", default=[],
                   help="只盘点指定源文件夹名，可重复")
    p.add_argument("--include-subfolder", action="append", default=[])
    p.add_argument("--exclude-subfolder", action="append", default=[])
    p.set_defaults(handler=cmd_inventory)

    p = sub.add_parser("acquire", help="只读下载 / 导出资源到本地")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--extension", action="append", default=[])
    p.add_argument("--item-type", action="append", default=[])
    p.add_argument("--source", action="append", default=[])
    p.add_argument("--top-folder", action="append", default=[],
                   help="只采集这些顶层目录，可重复（如 DOC-EN）；下载前按目录名收窄")
    p.add_argument("--language", action="append", default=[],
                   help="只采集这些语种目录（P0 语种代码，如 en/fr/zh），可重复；"
                        "目录别名来自策略 languages.folder_aliases")
    p.add_argument("--status", action="append", default=[],
                   help="只处理这些条目状态，可重复")
    p.add_argument("--sample", type=int, default=None,
                   help="跨层均匀抽样 N 条（Pilot 用），与 --limit 的『取前 N 条』不同")
    p.add_argument("--sample-strata", default="source_name,top_folder,extension",
                   help="分层维度，逗号分隔，可选 source_name/top_folder/extension/item_type")
    p.add_argument("--sample-seed", type=int, default=20260909,
                   help="抽样随机种子；同一种子 + 同一批候选 = 同一份样本")
    p.add_argument("--dry-run", action="store_true", help="只判断能不能采，不真正下载")
    p.add_argument("--keep-oversized", action="store_true",
                   help="保留超限文件的本地副本（默认删除，只留证据）")
    p.set_defaults(handler=cmd_acquire)

    p = sub.add_parser("extract", help="识别真实格式并抽取正文")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--extension", action="append", default=[])
    p.add_argument("--status", action="append", default=[])
    p.add_argument("--no-text-dump", action="store_true", help="不把正文落盘")
    p.set_defaults(handler=cmd_extract)

    p = sub.add_parser("deduplicate", help="跨源统一去重（SHA-256 + 规范化正文）")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--tolerance", type=float, default=None,
                   help="近重复字符数容差，默认 0.02")
    p.add_argument("--keep-duplicates", action="store_true",
                   help="只标记重复，不淘汰（默认淘汰精确重复）")
    p.set_defaults(handler=cmd_deduplicate)

    p = sub.add_parser("screen", help="硬规则筛选 + 可选语义复核")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--semantic", dest="semantic", action="store_true", default=None,
                   help="强制启用语义复核（需要可用模型）")
    p.add_argument("--no-semantic", dest="semantic", action="store_false",
                   help="强制只做硬规则筛选")
    p.add_argument("--status", action="append", default=[])
    p.add_argument("--include-unprocessed", action="store_true",
                   help="把尚未采集的条目也一起筛（默认跳过，否则复核队列会被几千条噪声淹没）")
    p.set_defaults(handler=cmd_screen)

    p = sub.add_parser("query", help="为合格文档生成 10 条 query 并校验")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--status", action="append", default=["accepted"])
    p.add_argument("--item-id", action="append", default=[],
                   help="只给这些 item_id 生成（定向批次；可重复，或配合 --item-id-file）")
    p.add_argument("--item-id-file", default=None,
                   help="从文件读 item_id 白名单（每行一个，# 开头为注释）")
    p.add_argument("--llm", action="store_true",
                   help="即使策略里 llm.enabled=false 也启用模型（本能力必须有模型才能产出）")
    p.set_defaults(handler=cmd_query)

    p = sub.add_parser("questions",
                       help="为已验证 query 的文档生成参考题目（题干/选项/答案/解析）")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--item-id", action="append", default=[],
                   help="只给这些 item_id 生成（定向批次；可重复，或配合 --item-id-file）")
    p.add_argument("--item-id-file", default=None,
                   help="从文件读 item_id 白名单（每行一个，# 开头为注释）")
    p.add_argument("--llm", action="store_true",
                   help="即使策略里 llm.enabled=false 也启用模型（本能力必须有模型才能产出）")
    p.set_defaults(handler=cmd_questions)

    p = sub.add_parser("report", help="生成覆盖与淘汰报告")
    p.set_defaults(handler=cmd_report)

    p = sub.add_parser("deliver",
                       help="交付：选目标集 + 真实文件名落地 + 出交付表（一行一条 query）")
    p.add_argument("--per-language", type=int, default=30,
                   help="每语种入选份数（默认 30；0 或负值=不设上限）")
    p.add_argument("--language", action="append", default=[],
                   help="只处理这些语种（可重复；默认取策略 P0 语种）")
    p.add_argument("--item-id", action="append", default=[],
                   help="定向白名单：只交付这些 item_id（可重复）")
    p.add_argument("--allow-out-of-scope-subject", action="store_true",
                   help="关闭学科二次校验（默认只交付学科属于 P0∪P1 的文档）")
    p.set_defaults(handler=cmd_deliver)

    p = sub.add_parser("resume", help="查看当前运行进度并给出下一步能力")
    p.set_defaults(handler=cmd_resume)

    p = sub.add_parser("reconcile",
                       help="一致性修复：把 query_records.jsonl 回灌状态库 queries 表")
    p.add_argument("--from-file", default=None,
                   help="指定 jsonl 路径（默认 <workspace>/<run>/query_records.jsonl）")
    p.set_defaults(handler=cmd_reconcile)


    p = sub.add_parser("status",
                       help="只读诊断：阶段×状态矩阵，一眼看清每个阶段的数量分布")
    p.set_defaults(handler=cmd_status)

    p = sub.add_parser("audit",
                       help="只读诊断：机械化执行 5 个验收检查点，输出 PASS/FAIL")
    p.add_argument("--compare-with", default=None,
                   help="另一次运行的目录，用于验证「可以复跑」（决策/哈希/计数必须一致）")
    p.set_defaults(handler=cmd_audit)

    p = sub.add_parser("explain",
                       help="只读诊断：打印一个条目的完整判定链（为什么是这个结果）")
    p.add_argument("--item-id", required=True,
                   help="item_id（等于飞书 token；本地适配器下是归一后的 id-<hash>）")
    p.set_defaults(handler=cmd_explain)

    p = sub.add_parser("run-all", help="按固定顺序跑完整条流水线（不含模型编排）")
    p.add_argument("--per-source-limit", type=int, default=None)
    p.add_argument("--limit", type=int, default=None, help="acquire/extract/query 的条数上限")
    p.add_argument("--skip-inventory", action="store_true")
    p.add_argument("--semantic", action="store_true")
    p.set_defaults(handler=cmd_run_all)

    return parser


# ---------------------------------------------------------------------------
# 命令实现：全部只做「取参数 -> 调 CoreService -> 打印」
# ---------------------------------------------------------------------------
def _service(args: argparse.Namespace) -> CoreService:
    config_path = args.config or os.path.join(_repo_root(), "config", "collection_policy.json")
    policy = load_policy(config_path)
    workspace_root = args.workspace or os.path.join(
        _repo_root(), str(policy.run_defaults.get("workspace_dir", "output/document_collection")))
    workspace_root = os.path.abspath(workspace_root)
    run_id = _resolve_run_id(workspace_root, args.run_id)
    if run_id == "@latest" or not run_id:
        run_id = make_run_id()
    log = (lambda _msg: None) if args.quiet else (lambda msg: print(msg, flush=True))
    return build_service(config_path, workspace_root=workspace_root, run_id=run_id,
                         log=log, replay_dir=args.replay_dir, record_dir=args.record_dir,
                         concurrency=getattr(args, "concurrency", 1))


def _emit(result: Any, args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        print(f"[{result.capability}] status={result.status} run_id={result.run_id}")
        summary = result.summary or {}
        for key in ("unique_items", "raw_occurrences", "total_selected", "total_screened",
                    "total_documents", "total_queries", "total_questions",
                    "documents_with_questions", "duplicate_items", "unique",
                    "accepted_total", "status_counts"):
            if key in summary:
                value = summary[key]
                if isinstance(value, dict):
                    value = json.dumps(value, ensure_ascii=False)
                print(f"  {key}: {value}")
        for artifact in result.artifacts:
            print(f"  artifact[{artifact.get('kind')}]: {artifact.get('path')}")
        for error in result.errors[:5]:
            print(f"  error[{error.get('stage')}]: {str(error.get('message'))[:200]}")
        if result.review_items:
            print(f"  review_items: {len(result.review_items)} 条（前 5 条）")
            for item in result.review_items[:5]:
                print(f"    - {item.get('source_path')}: {item.get('reason_codes')}")
        if result.next_action:
            print(f"  next_action: {result.next_action}")
    return _STATUS_EXIT.get(result.status, EXIT_FAILED)


def cmd_validate_config(args: argparse.Namespace, service: CoreService | None = None) -> int:
    config_path = args.config or os.path.join(_repo_root(), "config", "collection_policy.json")
    policy = load_policy(config_path)
    payload = describe_policy(policy)
    payload["validation"] = "ok"
    payload["run_config"] = policy.build_run_config(make_run_id()).to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"策略文件：{policy.path}")
        print(f"校验结果：ok（schema {payload['schema_version']}）")
        print("采集源：")
        for src in payload["sources"]:
            print(f"  - {src['name']}（{src['role']}）token={src['token']}")
        print(f"允许格式：{', '.join(payload['allowed_extensions'])}")
        print(f"需转换格式：{', '.join(payload['conversion_required_extensions'])}")
        print(f"大小上限(MB)：{json.dumps(payload['size_limits_mb'], ensure_ascii=False)}")
        print(f"目标语种：{', '.join(payload['target_languages'])}"
              f"（每种至少 {payload['min_documents_per_language']} 份）")
        print(f"每份文档 query：{payload['queries_per_document']} 条")
        for t in payload["query_types"]:
            flag = "（compatibility_only）" if t["compatibility_only"] else ""
            print(f"  - {t['type_key']}: {t['count']} 条，题量 {t['question_count']} {flag}")
        print(f"模型启用：{payload['llm_enabled']}")
        print(f"云盘只读：{True}")
        print(f"需求 revision：{json.dumps(payload['requirement_revisions'], ensure_ascii=False)}")
    return EXIT_OK


def cmd_runs(args: argparse.Namespace, service: CoreService | None = None) -> int:
    config_path = args.config or os.path.join(_repo_root(), "config", "collection_policy.json")
    policy = load_policy(config_path)
    workspace_root = args.workspace or os.path.join(
        _repo_root(), str(policy.run_defaults.get("workspace_dir", "output/document_collection")))
    workspace_root = os.path.abspath(workspace_root)
    runs: list[dict[str, Any]] = []
    if winpath.isdir(workspace_root):
        for name in sorted(winpath.listdir(workspace_root)):
            db = os.path.join(workspace_root, name, "state.sqlite3")
            if winpath.isfile(db):
                runs.append({"run_id": name, "db": db,
                             "mtime": os.path.getmtime(db),
                             "size_bytes": os.path.getsize(db)})
    runs.sort(key=lambda r: -r["mtime"])
    if args.json:
        print(json.dumps({"runs": runs}, ensure_ascii=False, indent=2))
    else:
        if not runs:
            print(f"工作区 {workspace_root} 下没有运行记录")
        for run in runs:
            import time as _time
            stamp = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(run["mtime"]))
            print(f"  {run['run_id']}  {stamp}  {run['size_bytes']} bytes")
    return EXIT_OK


def cmd_ingest(args: argparse.Namespace, service: CoreService) -> int:
    result = service.ingest(
        root=args.dir, extensions=tuple(args.extension), limit=args.limit,
        copy=args.copy, recursive=args.recursive, source_name=args.source_name,
        include_hidden=args.include_hidden,
    )
    return _emit(result, args)


def cmd_inventory(args: argparse.Namespace, service: CoreService) -> int:
    result = service.inventory(
        page_size=args.page_size, max_depth=args.max_depth,
        per_source_limit=args.per_source_limit, max_folders=args.max_folders,
        include_subfolders=tuple(args.include_subfolder),
        exclude_subfolders=tuple(args.exclude_subfolder),
        source_names=tuple(args.source),
    )
    return _emit(result, args)


def cmd_acquire(args: argparse.Namespace, service: CoreService) -> int:
    strata = tuple(s.strip() for s in str(args.sample_strata or "").split(",") if s.strip())
    result = service.acquire(
        limit=args.limit, extensions=tuple(args.extension),
        item_types=tuple(args.item_type), source_names=tuple(args.source),
        top_folders=tuple(args.top_folder), languages=tuple(args.language),
        dry_run=args.dry_run, include_statuses=tuple(args.status),
        sample=args.sample, sample_strata=strata, sample_seed=args.sample_seed,
    )
    return _emit(result, args)


def cmd_extract(args: argparse.Namespace, service: CoreService) -> int:
    result = service.extract(limit=args.limit, extensions=tuple(args.extension),
                             include_statuses=tuple(args.status))
    return _emit(result, args)


def cmd_deduplicate(args: argparse.Namespace, service: CoreService) -> int:
    result = service.deduplicate(limit=args.limit, tolerance=args.tolerance,
                                 reject_duplicates=not args.keep_duplicates)
    return _emit(result, args)


def cmd_screen(args: argparse.Namespace, service: CoreService) -> int:
    result = service.screen(limit=args.limit, semantic=args.semantic,
                            include_statuses=tuple(args.status),
                            include_unprocessed=args.include_unprocessed)
    return _emit(result, args)


def cmd_query(args: argparse.Namespace, service: CoreService) -> int:
    item_ids = _read_item_id_args(args)
    result = service.generate_queries(limit=args.limit, include_statuses=tuple(args.status),
                                      item_ids=tuple(item_ids),
                                      enable_llm=bool(getattr(args, "llm", False)) or None)
    return _emit(result, args)


def cmd_questions(args: argparse.Namespace, service: CoreService) -> int:
    item_ids = _read_item_id_args(args)
    result = service.generate_questions(limit=args.limit, item_ids=tuple(item_ids),
                                        enable_llm=bool(getattr(args, "llm", False)) or None)
    return _emit(result, args)


def _read_item_id_args(args: argparse.Namespace) -> list[str]:
    """合并 --item-id 与 --item-id-file（每行一个，# 开头为注释）。"""
    item_ids = list(args.item_id or [])
    id_file = getattr(args, "item_id_file", None)
    if id_file:
        with open(id_file, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                token = line.strip()
                if token and not token.startswith("#"):
                    item_ids.append(token)
    return item_ids


def cmd_reconcile(args: argparse.Namespace, service: CoreService) -> int:
    """一致性修复：jsonl 是导出产物，状态库落后时用它回灌（P4 兜底路径）。"""
    path = args.from_file or os.path.join(service.workspace_dir, "query_records.jsonl")
    if not winpath.isfile(path):
        print(f"[reconcile] 找不到 {path}（用一个存在的 run，或 --from-file 指定）",
              file=sys.stderr)
        return EXIT_FAILED
    stats = service.store.reconcile_queries_from_jsonl(path, logger=service.log)
    summary = {"reconciled_from": path, **stats}
    if args.json:
        print(json.dumps({"reconcile": summary}, ensure_ascii=False, indent=2))
    else:
        print(f"[reconcile] 回灌 {stats['records']} 份文档 / {stats['queries']} 条 query "
              f"/ {stats['decisions']} 条决策")
    return EXIT_OK


def cmd_report(args: argparse.Namespace, service: CoreService) -> int:
    result = service.report()
    return _emit(result, args)


def cmd_deliver(args: argparse.Namespace, service: CoreService) -> int:
    per_lang = getattr(args, "per_language", 30)
    result = service.deliver(
        per_language=None if (per_lang is not None and per_lang <= 0) else per_lang,
        languages=tuple(getattr(args, "language", []) or ()),
        item_ids=tuple(getattr(args, "item_id", []) or ()),
        require_subject_in_scope=not bool(getattr(args, "allow_out_of_scope_subject", False)),
    )
    return _emit(result, args)


def cmd_resume(args: argparse.Namespace, service: CoreService) -> int:
    result = service.resume()
    return _emit(result, args)


def cmd_status(args: argparse.Namespace, service: CoreService) -> int:
    """只读诊断：阶段×状态矩阵。service 出结构化 summary，人读渲染在这里。"""
    result = service.status()
    if args.json:
        return _emit(result, args)

    summary = result.summary or {}
    print(f"[status] run_id={result.run_id}")
    print(f"  workspace: {summary.get('workspace_dir')}")
    matrix = (
        ("items（条目状态）", summary.get("items") or {}),
        ("artifacts（本地产物）", summary.get("artifacts") or {}),
        ("extractions（抽取）", summary.get("extractions") or {}),
        ("screen（筛选决定）", summary.get("screen") or {}),
    )
    for title, counts in matrix:
        total = sum(counts.values())
        rendered = "  ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        print(f"  {title}: 合计 {total}" + (f" :: {rendered}" if rendered else " :: （无）"))
    queries = summary.get("queries") or {}
    print(f"  queries: 合计 {queries.get('total', 0)}"
          + "  ".join(f"  {key}={value}" for key, value in sorted(queries.items())
                      if key != "total"))
    questions = summary.get("questions") or {}
    print(f"  questions（参考题目）: 合计 {questions.get('total', 0)}"
          + "  ".join(f"  {key}={value}" for key, value in sorted(questions.items())
                      if key != "total"))
    dedup = summary.get("dedup_groups") or {}
    print(f"  dedup_groups: {dedup.get('total', 0)} 组 / {dedup.get('members', 0)} 条成员")
    languages = summary.get("languages") or {}
    if languages:
        rendered = "  ".join(f"{key}={value}" for key, value in sorted(languages.items()))
        print(f"  languages（抽取正文）: {rendered}")
    llm = summary.get("llm_stats") or {}
    print(f"  llm_calls: {llm.get('calls', 0)} 次（成功 {llm.get('ok_calls', 0)}）")
    print(f"  next_action: {result.next_action}")
    return _STATUS_EXIT.get(result.status, EXIT_FAILED)


def cmd_audit(args: argparse.Namespace, service: CoreService) -> int:
    result = service.audit(compare_run_dir=args.compare_with)
    return _emit(result, args)


def cmd_explain(args: argparse.Namespace, service: CoreService) -> int:
    """只读诊断：一条命令看清某个条目在每个阶段的决定与证据。

    这是 CLI 层的诊断命令，不属于 8 个契约能力，也不写任何数据。
    """
    item_id = args.item_id
    item = service.store.get_item(item_id)
    if item is None:
        # item_id 也可能是原始 token（本地适配器下两者不同），再按 token 找一次
        candidates = [i for i in service.store.list_items() if i.get("item_token") == item_id]
        if len(candidates) == 1:
            item = candidates[0]
            item_id = str(item["item_id"])
        elif candidates:
            print(f"[explain] item_token={item_id} 命中 {len(candidates)} 条，请改用 item_id")
            return EXIT_FAILED
        else:
            print(f"[explain] 找不到条目：{item_id}（run_id={service.run_id}）")
            return EXIT_FAILED

    meta = item.get("metadata") or {}
    payload: dict[str, Any] = {
        "item": {
            "item_id": item_id,
            "item_token": item.get("item_token"),
            "name": item.get("name"),
            "source_name": item.get("source_name"),
            "source_path": f"{item.get('source_name')}/{meta.get('relative_path') or item.get('name')}",
            "all_sources": meta.get("all_sources") or [],
            "url": item.get("url"),
            "item_type": item.get("item_type"),
            "extension": item.get("extension"),
            "status": item.get("status"),
            "stage": item.get("stage"),
        },
        "artifact": service.store.get_artifact(item_id),
        "extraction": service.store.get_extraction(item_id),
        "decisions": service.store.decisions_for(item_id),
        "queries": service.store.queries_for(item_id),
        "dedup_group": [
            {"kind": k.split(":", 1)[0], "group_key": k.split(":", 1)[1], "members": v}
            for k, v in service.store.dedup_groups().items()
            if any(m["item_id"] == item_id for m in v)
        ],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return EXIT_OK

    info = payload["item"]
    print(f"条目：{info['name']}")
    print(f"  item_id      : {info['item_id']}")
    print(f"  来源路径     : {info['source_path']}")
    if len(info["all_sources"]) > 1:
        print(f"  全部来源     : {len(info['all_sources'])} 处")
        for src in info["all_sources"]:
            print(f"                 - {src.get('source_name')}/{src.get('relative_path')}")
    print(f"  链接         : {info['url']}")
    print(f"  类型/扩展名  : {info['item_type']} / {info['extension']}")
    print(f"  当前状态     : {info['status']}（阶段 {info['stage']}）")
    artifact = payload["artifact"] or {}
    if artifact:
        print(f"  本地产物     : {artifact.get('path')}")
        print(f"  大小/SHA-256 : {artifact.get('size_bytes')} / {str(artifact.get('sha256'))[:16]}")
    extraction = payload["extraction"] or {}
    if extraction:
        print(f"  抽取         : {extraction.get('status')} by {extraction.get('extractor')}，"
              f"{extraction.get('char_count')} 字符，语言 {extraction.get('language')}"
              f"（置信度 {extraction.get('language_confidence')}）")
        if extraction.get("error"):
            print(f"  抽取错误     : {extraction.get('error')}")
    print("  判定链：")
    if not payload["decisions"]:
        print("    （无）")
    for decision in payload["decisions"]:
        codes = ",".join(decision.get("reason_codes") or [])
        print(f"    - [{decision.get('stage')}] {decision.get('status')} :: {codes}")
        evidence = decision.get("evidence") or {}
        if evidence:
            print(f"        evidence: {json.dumps(evidence, ensure_ascii=False, default=str)[:300]}")
    if payload["queries"]:
        print(f"  Query：{len(payload['queries'])} 条")
        for query in payload["queries"]:
            flag = " [compatibility_only]" if query.get("compatibility_only") else ""
            print(f"    {query['seq']:>2}. {query['type_key']} 题量={query['question_count']}{flag} "
                  f":: {str((query.get('payload') or {}).get('text'))[:70]}")
    return EXIT_OK


def cmd_run_all(args: argparse.Namespace, service: CoreService) -> int:
    """按固定顺序跑完整条流水线。

    这是"固定流程"，不是 Agent 编排：顺序写死在代码里，
    任何一步 failed 就停下并返回，方便定位是适配器、解析器还是规则的问题。
    """
    exit_code = EXIT_OK
    if not args.skip_inventory:
        result = service.inventory(per_source_limit=args.per_source_limit)
        exit_code = max(exit_code, _emit(result, args))
        if result.status == STATUS_FAILED:
            return exit_code
    for stage in ("acquire", "extract", "deduplicate", "screen"):
        if stage == "acquire":
            result = service.acquire(limit=args.limit)
        elif stage == "extract":
            result = service.extract(limit=args.limit)
        elif stage == "deduplicate":
            result = service.deduplicate()
        else:
            result = service.screen(limit=args.limit,
                                    semantic=True if args.semantic else None)
        # run-all 的 query 阶段同样允许显式启用模型
        exit_code = max(exit_code, _emit(result, args))
        if result.status == STATUS_FAILED:
            return exit_code
    if (service.store.latest_decisions("screen") or {}):
        accepted = [k for k, v in service.store.latest_decisions("screen").items()
                    if v.get("status") == "accepted"]
        if accepted:
            result = service.generate_queries(limit=args.limit,
                                              enable_llm=True if args.semantic else None)
            exit_code = max(exit_code, _emit(result, args))
            # 出题分两段：query 之后据 query 生成参考题目
            result = service.generate_questions(limit=args.limit,
                                               enable_llm=True if args.semantic else None)
            exit_code = max(exit_code, _emit(result, args))
    result = service.report()
    return max(exit_code, _emit(result, args))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command in ("validate-config", "runs"):
            return args.handler(args, None)
        service = _service(args)
    except PolicyError as exc:
        print(f"[config-error] {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception as exc:  # noqa: BLE001
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    try:
        return args.handler(args, service)
    except PolicyError as exc:
        print(f"[config-error] {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("[cancelled] 用户中断", file=sys.stderr)
        return EXIT_FAILED
    finally:
        service.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
