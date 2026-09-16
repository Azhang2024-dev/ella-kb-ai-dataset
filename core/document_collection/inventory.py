"""Task 2：递归盘点（inventory）。

职责边界：
- 只列举，不下载正文（计划 Task 2 明确要求）；
- 记录准确的源文件夹名称与完整相对路径；
- 处理分页与嵌套目录；
- 区分普通文件 / 在线 Docx / Sheet / Slides / Bitable / 快捷方式 / 未知；
- 两个来源合并后统一去重计数（硬约束 2）。

产物：
    run_manifest.json          运行元信息
    source_inventory_raw.jsonl 去重前逐条记录（每次出现一条）
    source_inventory.jsonl     合并去重后的唯一资源
    source_type_counts.csv     每个源文件夹 x 文件类型 的数量表
    inventory_summary.md       人类可读摘要
"""

from __future__ import annotations

import csv
import json
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import CollectionPolicy
from .contracts import SourceItem
from .state import StateStore
from . import winpath

LogFn = Callable[[str], None]

FOLDER = "folder"
LOCAL_FILE = "local_file"
ONLINE_DOC = "online_doc"
ONLINE_SHEET = "online_sheet"
ONLINE_SLIDES = "online_slides"
SHORTCUT = "shortcut"
UNKNOWN = "unknown"

_ONLINE_DOC_TYPES = {"docx", "doc", "wiki", "docx_v2"}
_ONLINE_SHEET_TYPES = {"sheet", "bitable", "base"}
_ONLINE_SLIDES_TYPES = {"slides", "slide"}
_SHORTCUT_TYPES = {"shortcut", "link", "url"}

# 盘点报表用的展示顺序（与在线文档 1.2 的表格口径一致）
DISPLAY_ORDER = (
    "PDF", "DOC/DOCX", "在线 Docx", "PPT/PPTX", "XLSX/CSV",
    "在线 Sheet", "TXT", "MD", "在线 Slides", "其他/未知",
)


def _noop_log(msg: str) -> None:  # pragma: no cover
    pass


def display_bucket(item_type: str, extension: str | None) -> str:
    """把内部类型映射到需求文档里的展示分桶。"""
    ext = (extension or "").lower().lstrip(".")
    if item_type == FOLDER:
        return "文件夹"
    if item_type == ONLINE_DOC:
        return "在线 Docx"
    if item_type == ONLINE_SHEET:
        return "在线 Sheet"
    if item_type == ONLINE_SLIDES:
        return "在线 Slides"
    if item_type == SHORTCUT:
        return "其他/未知"
    if ext == "pdf":
        return "PDF"
    if ext in ("doc", "docx"):
        return "DOC/DOCX"
    if ext in ("ppt", "pptx"):
        return "PPT/PPTX"
    if ext in ("xlsx", "xls", "csv"):
        return "XLSX/CSV"
    if ext == "txt":
        return "TXT"
    if ext in ("md", "markdown"):
        return "MD"
    return "其他/未知"


def classify_entry(entry: Mapping[str, Any], policy: CollectionPolicy | None = None) -> dict[str, Any]:
    """把适配器返回的原始条目归一化成 item_type / extension / kind。"""
    raw_type = str(entry.get("type") or "").lower()
    name = str(entry.get("name") or "")
    base, dot_ext = os.path.splitext(name)
    ext = dot_ext.lower().lstrip(".") if dot_ext else None

    if raw_type == FOLDER:
        return {"item_type": FOLDER, "extension": None, "online": False}
    if raw_type in _ONLINE_DOC_TYPES:
        target = None
        if policy is not None:
            target = policy.online_type_export.get(raw_type)
        return {"item_type": ONLINE_DOC, "extension": target or "docx",
                "online": True, "online_subtype": raw_type}
    if raw_type in _ONLINE_SHEET_TYPES:
        target = None
        if policy is not None:
            target = policy.online_type_export.get(raw_type)
        return {"item_type": ONLINE_SHEET, "extension": target or "xlsx",
                "online": True, "online_subtype": raw_type}
    if raw_type in _ONLINE_SLIDES_TYPES:
        target = None
        if policy is not None:
            target = policy.online_type_export.get(raw_type)
        return {"item_type": ONLINE_SLIDES, "extension": target or "pptx",
                "online": True, "online_subtype": raw_type}
    if raw_type in _SHORTCUT_TYPES or ext == "url":
        return {"item_type": SHORTCUT, "extension": ext, "online": False}
    if raw_type == "file":
        return {"item_type": LOCAL_FILE, "extension": ext, "online": False}
    return {"item_type": UNKNOWN, "extension": ext, "online": False, "raw_type": raw_type}


@dataclass
class InventoryOptions:
    page_size: int = 200
    max_depth: int = 10
    include_folders: bool = False
    per_source_limit: int | None = None
    include_subfolders: tuple[str, ...] = ()
    exclude_subfolders: tuple[str, ...] = ()
    max_folders: int | None = None


@dataclass
class InventoryReport:
    run_id: str
    started_at: str
    finished_at: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    raw_occurrences: int = 0
    unique_items: int = 0
    duplicate_occurrences: int = 0
    cross_source_duplicate_tokens: int = 0
    type_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    folder_counts: dict[str, int] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "sources": self.sources,
            "counts": {
                "raw_occurrences": self.raw_occurrences,
                "unique_items": self.unique_items,
                "duplicate_occurrences": self.duplicate_occurrences,
                "cross_source_duplicate_tokens": self.cross_source_duplicate_tokens,
            },
            "type_counts": self.type_counts,
            "folder_counts": self.folder_counts,
            "errors": self.errors,
            "artifacts": self.artifacts,
        }


def _matches_subfolder_filter(rel_path: str, opts: InventoryOptions) -> bool:
    if opts.exclude_subfolders:
        top = rel_path.split("/")[0].lower()
        if any(top == e.lower() or top.startswith(e.lower()) for e in opts.exclude_subfolders):
            return False
    if opts.include_subfolders:
        top = rel_path.split("/")[0].lower()
        return any(top == i.lower() or top.startswith(i.lower()) for i in opts.include_subfolders)
    return True


def walk_source(adapter: Any, source: Mapping[str, Any], policy: CollectionPolicy,
                opts: InventoryOptions, *,
                log: LogFn = _noop_log) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """广度优先递归盘点一个源文件夹。

    返回 (occurrences, errors, subfolder_count)。
    occurrence 是"某资源在某源某路径下出现一次"；
    subfolder_count 是**真实遍历到的子文件夹数**（不含源文件夹自身），
    不能用"有文件的目录路径数"近似，否则空文件夹会被漏计。
    """
    source_name = str(source.get("name") or source.get("token"))
    root_token = str(source.get("token") or source.get("path"))
    occurrences: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    queue: list[tuple[str, str, int]] = [(root_token, "", 0)]
    visited_folders: set[str] = {root_token}
    folder_count = 0
    subfolder_count = 0
    page_budget = opts.max_folders

    while queue:
        folder_token, rel_dir, depth = queue.pop(0)
        if depth > opts.max_depth:
            errors.append({"stage": "inventory", "source": source_name,
                           "message": f"超过最大深度 {opts.max_depth}，停止下探：{rel_dir or '<root>'}"})
            continue
        page_token: str | None = None
        page_index = 0
        while True:
            try:
                page = adapter.list_folder(folder_token, page_token=page_token,
                                           page_size=opts.page_size)
            except Exception as exc:  # noqa: BLE001 - 盘点必须容错，单页失败不拖垮整体
                errors.append({
                    "stage": "inventory", "source": source_name,
                    "folder_token": folder_token, "relative_path": rel_dir,
                    "page_index": page_index,
                    "message": f"{type(exc).__name__}: {exc}",
                    "retryable": bool(getattr(exc, "retryable", False)),
                })
                log(f"  [WARN] {source_name} 列举失败 {rel_dir or '<root>'} p{page_index}: {exc}")
                break
            page_index += 1
            for entry in page.entries:
                cls = classify_entry(entry, policy)
                name = str(entry.get("name") or "")
                rel_path = f"{rel_dir}/{name}" if rel_dir else name
                token = str(entry.get("token") or "")
                if cls["item_type"] == FOLDER:
                    folder_count += 1
                    subfolder_count += 1
                    if token and token not in visited_folders:
                        visited_folders.add(token)
                        if page_budget is None or folder_count <= page_budget:
                            queue.append((token, rel_path, depth + 1))
                    if opts.include_folders:
                        occurrences.append(_occurrence(source, entry, cls, rel_path, rel_dir))
                    continue
                if not _matches_subfolder_filter(rel_path, opts):
                    continue
                occurrences.append(_occurrence(source, entry, cls, rel_path, rel_dir))
                if opts.per_source_limit and len(occurrences) >= opts.per_source_limit:
                    log(f"  [INFO] {source_name} 达到 per_source_limit={opts.per_source_limit}，停止盘点")
                    return occurrences, errors, subfolder_count
            if page.has_more and page.next_page_token:
                page_token = page.next_page_token
                continue
            if page.has_more and not page.next_page_token:
                errors.append({"stage": "inventory", "source": source_name,
                               "folder_token": folder_token, "relative_path": rel_dir,
                               "message": "has_more=true 但缺少 next_page_token，可能有漏项"})
            break
        log(f"  [DIR] {source_name}/{rel_dir or '<root>'} 累计 {len(occurrences)} 条")

    return occurrences, errors, subfolder_count


def _occurrence(source: Mapping[str, Any], entry: Mapping[str, Any],
                cls: Mapping[str, Any], rel_path: str, rel_dir: str) -> dict[str, Any]:
    size = entry.get("size")
    return {
        "source_name": str(source.get("name")),
        "source_role": str(source.get("role") or ""),
        "source_folder_token": str(source.get("token") or ""),
        "source_folder_path": rel_dir or "/",
        "relative_path": rel_path,
        "item_token": str(entry.get("token") or ""),
        "name": str(entry.get("name") or ""),
        "raw_type": str(entry.get("type") or ""),
        "item_type": cls["item_type"],
        "extension": cls.get("extension"),
        "online": bool(cls.get("online")),
        "online_subtype": cls.get("online_subtype"),
        "remote_size": int(size) if isinstance(size, int) else None,
        "remote_modified_time": entry.get("modified_time"),
        "url": entry.get("url"),
        "owner_id": entry.get("owner_id"),
        "display_bucket": display_bucket(cls["item_type"], cls.get("extension")),
    }


def to_source_item(occ: Mapping[str, Any]) -> SourceItem:
    return SourceItem(
        source_name=occ["source_name"],
        source_folder_path=occ["source_folder_path"],
        item_token=occ["item_token"],
        name=occ["name"],
        item_type=occ["item_type"],
        extension=occ.get("extension"),
        mime_type=None,
        remote_size=occ.get("remote_size"),
        remote_modified_time=occ.get("remote_modified_time"),
        url=occ.get("url"),
        metadata={
            "relative_path": occ["relative_path"],
            "raw_type": occ.get("raw_type"),
            "online": occ.get("online", False),
            "online_subtype": occ.get("online_subtype"),
            "display_bucket": occ.get("display_bucket"),
            "source_role": occ.get("source_role"),
            "source_folder_token": occ.get("source_folder_token"),
            "owner_id": occ.get("owner_id"),
        },
    )


def merge_and_count(occurrences: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """跨源统一去重计数。

    去重键 = item_token（同一资源在两个文件夹都挂快捷方式时 token 相同）。
    同名不同 token 不在这里合并：需求 5 明确要求"先比对正文再计数"，
    正文级去重属于 deduplication 阶段，盘点阶段只做 token 级合并。
    """
    by_token: dict[str, dict[str, Any]] = {}
    per_source_raw: Counter = Counter()
    per_source_unique: Counter = Counter()
    bucket_counts: dict[str, Counter] = defaultdict(Counter)
    ext_counts: dict[str, Counter] = defaultdict(Counter)
    folder_paths: dict[str, set[str]] = defaultdict(set)

    for occ in occurrences:
        src = occ["source_name"]
        token = occ["item_token"]
        per_source_raw[src] += 1
        bucket_counts[src][occ.get("display_bucket") or UNKNOWN] += 1
        if occ.get("extension"):
            ext_counts[src][str(occ["extension"]).lower()] += 1
        folder_paths[src].add(occ.get("source_folder_path") or "/")
        if token not in by_token:
            by_token[token] = {
                **occ,
                "all_sources": [{"source_name": occ["source_name"],
                                 "relative_path": occ["relative_path"],
                                 "url": occ.get("url")}],
                "duplicate_of_token": False,
            }
            per_source_unique[src] += 1
        else:
            by_token[token]["all_sources"].append(
                {"source_name": occ["source_name"],
                 "relative_path": occ["relative_path"],
                 "url": occ.get("url")}
            )

    merged_buckets: Counter = Counter()
    for token, rec in by_token.items():
        merged_buckets[rec.get("display_bucket") or UNKNOWN] += 1

    cross_source = sum(
        1 for rec in by_token.values()
        if len({s["source_name"] for s in rec["all_sources"]}) > 1
    )
    return {
        "unique_items": len(by_token),
        "raw_occurrences": sum(per_source_raw.values()),
        "duplicate_occurrences": sum(per_source_raw.values()) - len(by_token),
        "cross_source_duplicate_tokens": cross_source,
        "per_source_raw": dict(per_source_raw),
        "per_source_unique": dict(per_source_unique),
        "per_source_buckets": {k: dict(v) for k, v in bucket_counts.items()},
        "per_source_extensions": {k: dict(v) for k, v in ext_counts.items()},
        "per_source_folder_count": {k: len(v) for k, v in folder_paths.items()},
        "merged_buckets": dict(merged_buckets),
        "unique_records": by_token,
    }


def write_type_counts_csv(path: str, sources: Sequence[Mapping[str, Any]],
                          stats: Mapping[str, Any]) -> None:
    """输出「每个源文件夹 x 文件类型」的数量表（列名用真实文件夹名）。"""
    names = [str(s["name"]) for s in sources]
    per_source = stats["per_source_buckets"]
    buckets = list(DISPLAY_ORDER)
    extra = sorted({b for n in names for b in per_source.get(n, {})} - set(buckets) - {"文件夹"})
    merged = stats["merged_buckets"]
    rows = []
    for bucket in buckets + extra:
        row = {"文件类型": bucket}
        for n in names:
            row[n] = int(per_source.get(n, {}).get(bucket, 0))
        row["合计（去重前）"] = sum(row[n] for n in names)
        row["两源合并去重后"] = int(merged.get(bucket, 0))
        rows.append(row)
    total_row = {"文件类型": "非文件夹资源合计"}
    for n in names:
        total_row[n] = int(stats["per_source_raw"].get(n, 0))
    total_row["合计（去重前）"] = sum(total_row[n] for n in names)
    total_row["两源合并去重后"] = int(stats["unique_items"])
    rows.append(total_row)

    fieldnames = ["文件类型", *names, "合计（去重前）", "两源合并去重后"]
    winpath.makedirs(os.path.dirname(os.path.abspath(path)))
    with winpath.open_write(path, encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: str, records: Iterable[Mapping[str, Any]]) -> int:
    winpath.makedirs(os.path.dirname(os.path.abspath(path)))
    n = 0
    with winpath.open_write(path, encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(dict(rec), ensure_ascii=False, default=str) + "\n")
            n += 1
    return n


def write_inventory_summary(path: str, report: InventoryReport, policy: CollectionPolicy) -> None:
    names = [s["name"] for s in report.sources]
    lines: list[str] = []
    lines.append("# Inventory 盘点摘要")
    lines.append("")
    lines.append(f"- run_id：`{report.run_id}`")
    lines.append(f"- 开始时间：{report.started_at}")
    lines.append(f"- 结束时间：{report.finished_at}")
    lines.append(f"- 采集源（只读）：{'、'.join(f'`{n}`' for n in names)}")
    lines.append("")
    lines.append("## 1. 源文件夹")
    lines.append("")
    lines.append("| 源文件夹名称 | 角色 | token | 原始资源数 | 子目录数 |")
    lines.append("|-|-|-|-|-|")
    for src in report.sources:
        lines.append(
            f"| {src['name']} | {src.get('role','')} | `{src.get('token','')}` | "
            f"{src.get('raw_count', 0)} | {src.get('folder_count', 0)} |"
        )
    lines.append("")
    lines.append("## 2. 合并与去重")
    lines.append("")
    lines.append(f"- 去重前原始资源数：**{report.raw_occurrences}**")
    lines.append(f"- 按 token 合并后唯一资源数：**{report.unique_items}**")
    lines.append(f"- 重复出现次数：**{report.duplicate_occurrences}**")
    lines.append(f"- 跨源同 token 资源数：**{report.cross_source_duplicate_tokens}**")
    lines.append("- 说明：同名不同 token 不在盘点阶段合并，正文级去重在 `deduplicate` 阶段完成。")
    lines.append("")
    lines.append("## 3. 每个源文件夹的文件类型数量")
    lines.append("")
    per_source = report.type_counts
    buckets = list(DISPLAY_ORDER)
    extra = sorted({b for n in names for b in per_source.get(n, {})} - set(buckets) - {"文件夹"})
    header = "| 文件类型 | " + " | ".join(names) + " | 合计（去重前） |"
    lines.append(header)
    lines.append("|" + "-|" * (len(names) + 2))
    for bucket in buckets + extra:
        cells = [str(int(per_source.get(n, {}).get(bucket, 0))) for n in names]
        lines.append(f"| {bucket} | " + " | ".join(cells) + f" | {sum(int(c) for c in cells)} |")
    totals = [str(int(report.folder_counts.get(n, {}).get('raw', 0))) for n in names]
    lines.append("| **非文件夹资源合计** | " + " | ".join(totals) +
                 f" | {sum(int(t) for t in totals)} |")
    lines.append("")
    lines.append(f"两个来源按 token 合并去重后：**{report.unique_items}** 条唯一资源。")
    lines.append("")
    lines.append("## 4. 错误与告警")
    lines.append("")
    if report.errors:
        lines.append(f"- 共 {len(report.errors)} 条，详见 `run_manifest.json`。")
        for err in report.errors[:10]:
            lines.append(f"  - {err.get('source')} {err.get('relative_path','')}: {err.get('message')}")
    else:
        lines.append("- 无。")
    lines.append("")
    lines.append("## 5. 口径说明")
    lines.append("")
    lines.append("- 以上均为**原始候选数量**，不等于最终合格数量。")
    lines.append("- 飞书 `drive files list` 不返回文件大小，大小门槛在采集后按本地字节数判定。")
    lines.append("- 全过程只读：未删除、移动、重命名或修改任何云盘源文件。")
    winpath.makedirs(os.path.dirname(os.path.abspath(path)))
    with winpath.open_write(path, encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def run_inventory(*, adapter: Any, sources: Sequence[Mapping[str, Any]],
                  policy: CollectionPolicy, store: StateStore, workspace_dir: str,
                  run_id: str, options: InventoryOptions | None = None,
                  log: LogFn = _noop_log) -> InventoryReport:
    """执行盘点，写产物并落库。"""
    opts = options or InventoryOptions(
        page_size=int(policy.section("source_access").get("page_size", 200)),
        max_depth=int(policy.section("source_access").get("max_depth", 10)),
    )
    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    winpath.makedirs(workspace_dir)
    report = InventoryReport(run_id=run_id, started_at=started)

    all_occurrences: list[dict[str, Any]] = []
    for source in sources:
        name = str(source.get("name"))
        log(f"[inventory] 开始盘点源文件夹：{name}（token={source.get('token')}）")
        occ, errs, subfolders = walk_source(adapter, source, policy, opts, log=log)
        all_occurrences.extend(occ)
        report.errors.extend(errs)
        report.sources.append({
            "name": name,
            "role": source.get("role"),
            "token": source.get("token"),
            "url": source.get("url"),
            "raw_count": len(occ),
            "folder_count": subfolders,
            "directories_with_files": len({o["source_folder_path"] for o in occ}),
        })
        report.type_counts[name] = dict(Counter(o.get("display_bucket") or UNKNOWN for o in occ))
        report.folder_counts[name] = {"raw": len(occ)}
        log(f"[inventory] {name} 完成：{len(occ)} 条非文件夹资源，{len(errs)} 条告警")

    stats = merge_and_count(all_occurrences)
    report.raw_occurrences = int(stats["raw_occurrences"])
    report.unique_items = int(stats["unique_items"])
    report.duplicate_occurrences = int(stats["duplicate_occurrences"])
    report.cross_source_duplicate_tokens = int(stats["cross_source_duplicate_tokens"])
    report.finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    # --- 产物 ---
    raw_path = os.path.join(workspace_dir, "source_inventory_raw.jsonl")
    uniq_path = os.path.join(workspace_dir, "source_inventory.jsonl")
    csv_path = os.path.join(workspace_dir, "source_type_counts.csv")
    md_path = os.path.join(workspace_dir, "inventory_summary.md")
    manifest_path = os.path.join(workspace_dir, "run_manifest.json")

    write_jsonl(raw_path, all_occurrences)
    unique_records = list(stats["unique_records"].values())
    write_jsonl(uniq_path, unique_records)
    write_type_counts_csv(csv_path, sources, stats)
    write_inventory_summary(md_path, report, policy)

    # --- 落库（幂等 upsert，支持续跑）---
    store.start_run(run_id, workspace_dir, config={"capability": "collection.inventory"},
                    policy_digest=str(hash(json.dumps(policy.raw, sort_keys=True, ensure_ascii=False))))
    items = []
    for rec in unique_records:
        item = to_source_item(rec)
        d = item.to_dict()
        d["run_id"] = run_id
        d["relative_path"] = rec["relative_path"]
        d["status"] = "discovered"
        d["stage"] = "inventory"
        d["metadata"] = {**item.metadata, "all_sources": rec["all_sources"],
                         "display_bucket": rec.get("display_bucket")}
        items.append(d)
    store.upsert_items(items)
    store.touch_run(run_id, {"collection.inventory": {"status": "completed",
                                           "unique_items": report.unique_items,
                                           "raw_occurrences": report.raw_occurrences}})

    manifest = {
        "run_id": run_id,
        "capability": "collection.inventory",
        "contract_version": "0.1",
        "policy_path": policy.path,
        "requirement_revisions": policy.section("requirement_revisions"),
        "options": {
            "page_size": opts.page_size, "max_depth": opts.max_depth,
            "include_folders": opts.include_folders,
            "per_source_limit": opts.per_source_limit,
        },
        "report": report.to_dict(),
        "adapter": getattr(adapter, "name", type(adapter).__name__),
        "generated_at": report.finished_at,
    }
    manifest["report"].pop("unique_records", None)
    with winpath.open_write(manifest_path, encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, default=str)

    report.artifacts = [
        {"path": raw_path, "kind": "inventory_raw", "records": len(all_occurrences)},
        {"path": uniq_path, "kind": "inventory_unique", "records": len(unique_records)},
        {"path": csv_path, "kind": "type_counts_csv"},
        {"path": md_path, "kind": "inventory_summary_md"},
        {"path": manifest_path, "kind": "run_manifest"},
    ]
    return report
