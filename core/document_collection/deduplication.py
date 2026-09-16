"""Task 5（后半）：统一去重。

规则（计划 Task 5 + 需求第五章）：
1. 先比原始 SHA-256：完全相同的文件只保留一个代表；
2. 再比规范化文本哈希：不同容器（例如同一讲义的 pdf 与 docx 导出）正文一致也算重复；
3. 疑似近重复只标记，不自动删除，也不自动淘汰；
4. 保留所有来源路径（item_sources 表 + 报告里的 all_sources）；
5. 记录推荐保留版本与原因。

代表版本选择必须是**确定性**的：同样的输入永远选同一个，
否则「可以复跑」这条验收过不了。排序键 = (正文更长, 定位更多, 来源更早, token 字典序)。
"""

from __future__ import annotations

import csv
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .config import CollectionPolicy
from .contracts import (
    ITEM_REJECTED,
    RC_DUP_EXACT,
    RC_DUP_NORMALIZED,
    RC_DUP_UNIQUE,
    RC_NEAR_DUP_SUSPECT,
)
from .state import StateStore
from . import winpath

LogFn = Callable[[str], None]


def _noop(msg: str) -> None:  # pragma: no cover
    pass


@dataclass
class DedupOptions:
    near_duplicate_char_tolerance: float = 0.02
    mark_near_duplicates: bool = True
    reject_exact_duplicates: bool = True
    include_statuses: tuple[str, ...] = ()   # 空 = 全部已抽取的条目
    limit: int | None = None


def _rows(store: StateStore, opts: DedupOptions) -> list[dict[str, Any]]:
    items = store.list_items(statuses=list(opts.include_statuses) or None)
    rows: list[dict[str, Any]] = []
    for item in items:
        item_id = str(item["item_id"])
        extraction = store.get_extraction(item_id) or {}
        artifact = store.get_artifact(item_id) or {}
        rows.append({
            "item_id": item_id,
            "item_token": item.get("item_token"),
            "name": item.get("name"),
            "source_name": item.get("source_name"),
            "relative_path": (item.get("metadata") or {}).get("relative_path") or item.get("relative_path"),
            "extension": item.get("extension"),
            "item_type": item.get("item_type"),
            "sha256": artifact.get("sha256"),
            "size_bytes": artifact.get("size_bytes"),
            "char_count": extraction.get("char_count") or 0,
            "segment_count": 0,
            "language": extraction.get("language"),
            "page_count": extraction.get("page_count"),
            "source_role": (item.get("metadata") or {}).get("source_role") or "",
            "normalized_fingerprint": _fingerprint_from_decisions(store, item_id),
            "extract_status": extraction.get("status"),
            "all_sources": (item.get("metadata") or {}).get("all_sources") or [],
        })
    # 只有真正拿到本地产物或正文指纹的条目才参与去重；
    # 之前就被 reject 的条目不该在这里再拿一个 "unique" 决定，否则报告会被噪声污染。
    rows = [r for r in rows if r.get("sha256") or r.get("normalized_fingerprint")]
    if opts.limit:
        rows = rows[: int(opts.limit)]
    return rows


def _fingerprint_from_decisions(store: StateStore, item_id: str) -> str | None:
    for decision in reversed(store.decisions_for(item_id)):
        if decision.get("stage") == "extract":
            return (decision.get("evidence") or {}).get("normalized_fingerprint")
    return None


# 需求第五章："优先保留正文质量更高、教育属性更明确、定位更稳定的版本"。
# 这几条都是语义判断，去重阶段还没有语义结论，所以用可确定性计算的代理指标，
# 并把顺序写死，保证同一批数据永远选同一个代表（验收检查点 5）。
_ROLE_PRIORITY = {"primary": 2, "supplement": 1}

REPRESENTATIVE_RULE = (
    "正文更长 > 文件更大 > 有页数定位 > 来源角色 primary 优先 > 路径字典序兜底"
)


def _representative_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """排序键：越大越优先。全部字段都可比较，保证确定性。"""
    page_count = row.get("page_count")
    return (
        int(row.get("char_count") or 0),
        int(row.get("size_bytes") or 0),
        1 if page_count else 0,
        _ROLE_PRIORITY.get(str(row.get("source_role") or ""), 0),
        str(row.get("relative_path") or ""),
        str(row.get("item_id") or ""),
    )


def _pick_representative(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return max(rows, key=_representative_key)


def group_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """返回 sha / normalized / near 三组分组结果。"""
    by_sha: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_fp: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("sha256"):
            by_sha[str(row["sha256"])].append(dict(row))
        if row.get("normalized_fingerprint"):
            by_fp[str(row["normalized_fingerprint"])].append(dict(row))

    sha_groups = {k: v for k, v in by_sha.items() if len(v) > 1}
    fp_groups = {k: v for k, v in by_fp.items() if len(v) > 1}
    return {"sha": dict(sha_groups), "normalized": dict(fp_groups),
            "sha_all": dict(by_sha), "normalized_all": dict(by_fp)}


def find_near_duplicates(rows: Sequence[Mapping[str, Any]], tolerance: float = 0.02) -> list[dict[str, Any]]:
    """疑似近重复：同语言 + 同页数 + 字符数接近，但 sha 与正文指纹都不同。

    只标记，不淘汰（计划 Task 5 明确要求）。
    """
    buckets: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not row.get("sha256") or not row.get("normalized_fingerprint"):
            continue
        key = (row.get("language"), row.get("page_count"))
        buckets[key].append(dict(row))

    suspects: list[dict[str, Any]] = []
    for (_lang, _pages), group in buckets.items():
        if len(group) < 2:
            continue
        group_sorted = sorted(group, key=lambda r: int(r.get("char_count") or 0))
        for i in range(len(group_sorted)):
            for j in range(i + 1, len(group_sorted)):
                a, b = group_sorted[i], group_sorted[j]
                ca, cb = int(a.get("char_count") or 0), int(b.get("char_count") or 0)
                if ca <= 0 or cb <= 0:
                    continue
                if a.get("sha256") == b.get("sha256"):
                    continue
                if a.get("normalized_fingerprint") == b.get("normalized_fingerprint"):
                    continue
                if abs(cb - ca) / max(ca, cb) <= tolerance:
                    suspects.append({
                        "item_a": a["item_id"], "item_b": b["item_id"],
                        "name_a": a.get("name"), "name_b": b.get("name"),
                        "char_count_a": ca, "char_count_b": cb,
                        "delta_ratio": round(abs(cb - ca) / max(ca, cb), 4),
                        "language": a.get("language"),
                        "reason": RC_NEAR_DUP_SUSPECT,
                    })
    return suspects


def run_deduplicate(*, policy: CollectionPolicy, store: StateStore, workspace_dir: str,
                    run_id: str, options: DedupOptions | None = None,
                    log: LogFn = _noop) -> dict[str, Any]:
    """执行去重，写 dedup_report.jsonl / dedup_groups.csv，落库并给决定。"""
    opts = options or DedupOptions()
    winpath.makedirs(workspace_dir)
    rows = _rows(store, opts)
    log(f"[dedup] 参与去重 {len(rows)} 条")

    groups = group_rows(rows)
    duplicates: dict[str, dict[str, Any]] = {}

    # 1) 精确重复（SHA-256）
    for key, members in groups["sha"].items():
        rep = _pick_representative(members)
        for member in members:
            store.set_dedup(key, "sha256", member["item_id"],
                            member["item_id"] == rep["item_id"],
                            "sha256_identical")
            if member["item_id"] != rep["item_id"]:
                duplicates[member["item_id"]] = {
                    "kind": "sha256", "group_key": key,
                    "representative": rep["item_id"],
                    "reason_codes": [RC_DUP_EXACT],
                    "reason": f"与 {rep.get('name')} 字节完全相同",
                }

    # 2) 正文规范化重复
    for key, members in groups["normalized"].items():
        rep = _pick_representative(members)
        for member in members:
            store.set_dedup(key, "normalized_text", member["item_id"],
                            member["item_id"] == rep["item_id"],
                            "normalized_text_identical")
            if member["item_id"] != rep["item_id"] and member["item_id"] not in duplicates:
                duplicates[member["item_id"]] = {
                    "kind": "normalized_text", "group_key": key,
                    "representative": rep["item_id"],
                    "reason_codes": [RC_DUP_NORMALIZED],
                    "reason": f"正文规范化后与 {rep.get('name')} 一致",
                }

    near = find_near_duplicates(rows, opts.near_duplicate_char_tolerance) if opts.mark_near_duplicates else []

    unique_ids = {r["item_id"] for r in rows} - set(duplicates)
    for item_id in unique_ids:
        store.add_decision(item_id, "deduplicate", "unique", [RC_DUP_UNIQUE], {
            "sha_groups": 0, "normalized_groups": 0,
        })

    if opts.reject_exact_duplicates:
        pairs: list[tuple[str, str, str]] = []
        for item_id, info in duplicates.items():
            store.add_decision(item_id, "deduplicate", ITEM_REJECTED, info["reason_codes"], {
                "kind": info["kind"], "group_key": info["group_key"],
                "representative": info["representative"], "reason": info["reason"],
            })
            pairs.append((item_id, ITEM_REJECTED, "deduplicate"))
        store.set_item_stages(pairs)
    for item_id in unique_ids:
        store.set_item_stage(item_id, store.get_item(item_id)["status"], "deduplicate")

    near_ids = sorted({s["item_a"] for s in near} | {s["item_b"] for s in near})
    for item_id in near_ids:
        store.add_decision(item_id, "deduplicate", "near_duplicate_suspect",
                           [RC_NEAR_DUP_SUSPECT], {"marked_only": True})

    # --- 产物 ---
    report_path = os.path.join(workspace_dir, "dedup_report.jsonl")
    with winpath.open_write(report_path, encoding="utf-8") as fh:
        for row in rows:
            dup = duplicates.get(row["item_id"])
            fh.write(json.dumps({
                "item_id": row["item_id"], "name": row.get("name"),
                "source_name": row.get("source_name"),
                "relative_path": row.get("relative_path"),
                "extension": row.get("extension"),
                "sha256": row.get("sha256"),
                "char_count": row.get("char_count"),
                "language": row.get("language"),
                "all_sources": row.get("all_sources"),
                "duplicate": dup,
                "near_duplicate_suspect": row["item_id"] in near_ids,
            }, ensure_ascii=False, default=str) + "\n")

    groups_csv = os.path.join(workspace_dir, "dedup_groups.csv")
    with winpath.open_write(groups_csv, encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["去重类型", "分组键(前16位)", "成员数", "推荐保留", "推荐原因",
                         "成员清单（源文件夹/路径）"])
        for kind, table in (("sha256", groups["sha"]), ("normalized_text", groups["normalized"])):
            for key, members in sorted(table.items(), key=lambda kv: -len(kv[1])):
                rep = _pick_representative(members)
                reason = (f"{REPRESENTATIVE_RULE}；本组选中 "
                          f"{rep.get('source_name')}/{rep.get('relative_path')}"
                          f"（正文 {rep.get('char_count', 0)} 字符）")
                listing = "; ".join(f"{m.get('source_name')}/{m.get('relative_path')}" for m in members)
                writer.writerow([kind, key[:16], len(members),
                                 f"{rep.get('source_name')}/{rep.get('relative_path')}",
                                 reason, listing])

    near_path = os.path.join(workspace_dir, "near_duplicate_suspects.jsonl")
    with winpath.open_write(near_path, encoding="utf-8") as fh:
        for rec in near:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    summary = {
        "total_rows": len(rows),
        "sha256_groups": len(groups["sha"]),
        "normalized_groups": len(groups["normalized"]),
        "duplicate_items": len(duplicates),
        "unique_items": len(unique_ids),
        "near_duplicate_pairs": len(near),
        "near_duplicate_items": len(near_ids),
        "cross_source_duplicate_items": sum(
            1 for r in rows if r["item_id"] in duplicates
            and len({s.get("source_name") for s in (r.get("all_sources") or [])}) > 1
        ),
        "artifacts": [report_path, groups_csv, near_path],
        "elapsed_sec": 0.0,
    }
    store.touch_run(run_id, {"collection.deduplicate": summary})
    log(f"[dedup] 完成：重复 {summary['duplicate_items']} 条，唯一 {summary['unique_items']} 条，"
        f"近重复疑似 {summary['near_duplicate_pairs']} 对")
    return summary
