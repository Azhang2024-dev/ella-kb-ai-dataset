"""Task 8：报告与覆盖审计。

报告要回答业务方的四个问题：
1. 两个源文件夹各有多少什么类型的资源（用真实文件夹名，不用"原始/新增"）；
2. 合并去重后还剩多少，淘汰都淘汰在哪一步、为什么；
3. 距离验收配额（P0 语种各 20、P1 学科各 1、格式软目标 3:2:2:1:1:1）还差多少；
4. 这一轮花了多少时间、调了多少次模型。

所有数字都从 SQLite 现算，不从中间文件读，避免"报告和库对不上"。
"""

from __future__ import annotations

import csv
import json
import os
import time
from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence

from .config import CollectionPolicy
from .contracts import (
    ITEM_ACCEPTED,
    ITEM_NEEDS_REVIEW,
    ITEM_REJECTED,
    deciding_reason,
    is_explanatory_reason,
)
from .inventory import DISPLAY_ORDER, display_bucket
from .state import StateStore
from . import winpath

# 需求 6.3 的格式软目标：PDF 36 / DOC-DOCX 24 / PPTX 24 / XLSX-CSV 12 / TXT 12 / MD 12
FORMAT_SOFT_TARGETS: dict[str, int] = {
    "PDF": 36, "DOC/DOCX": 24, "PPT/PPTX": 24,
    "XLSX/CSV": 12, "TXT": 12, "MD": 12,
}


def _display_bucket_of(item: Mapping[str, Any]) -> str:
    meta = item.get("metadata") or {}
    bucket = meta.get("display_bucket")
    if bucket:
        return str(bucket)
    return display_bucket(str(item.get("item_type") or ""), item.get("extension"))


def collect_facts(store: StateStore, policy: CollectionPolicy) -> dict[str, Any]:
    """从库里现算全部报告事实。"""
    items = store.list_items()
    decisions_screen = store.latest_decisions("screen")
    decisions_dedup = store.latest_decisions("deduplicate")
    extractions = {r["item_id"]: r for r in store.list_extractions()}
    artifacts = {r["item_id"]: r for r in store.list_artifacts()}

    per_source_raw: Counter = Counter()
    per_source_bucket: dict[str, Counter] = defaultdict(Counter)
    status_counts: Counter = Counter()
    accepted_by_language: Counter = Counter()
    accepted_by_subject: Counter = Counter()
    accepted_by_bucket: Counter = Counter()
    reason_counts: Counter = Counter()
    review_reason_counts: Counter = Counter()
    long_documents = 0
    total_chars = 0
    dedup_duplicates = 0
    near_duplicates = 0

    for item in items:
        item_id = str(item["item_id"])
        src = str(item.get("source_name") or "?")
        bucket = _display_bucket_of(item)
        per_source_raw[src] += 1
        per_source_bucket[src][bucket] += 1

        decision = decisions_screen.get(item_id)
        status = str(decision["status"]) if decision else str(item.get("status") or "unknown")
        status_counts[status] += 1
        for code in (decision or {}).get("reason_codes") or []:
            if not is_explanatory_reason(code):
                continue        # 跳过"通过了哪个闸门"这类正向/信息性标记
            key = str(code).split("=", 1)[0]
            if status == ITEM_REJECTED:
                reason_counts[key] += 1
            elif status == ITEM_NEEDS_REVIEW:
                review_reason_counts[key] += 1

        extraction = extractions.get(item_id) or {}
        total_chars += int(extraction.get("char_count") or 0)
        long_documents += int(extraction.get("is_long_document") or 0)

        dedup = decisions_dedup.get(item_id)
        if dedup:
            codes = [str(c) for c in (dedup.get("reason_codes") or [])]
            if "exact_duplicate" in codes or "normalized_duplicate" in codes:
                dedup_duplicates += 1
            if "near_duplicate_suspect" in codes:
                near_duplicates += 1

        if status == ITEM_ACCEPTED:
            evidence = (decision or {}).get("evidence") or {}
            lang = str(evidence.get("language") or extraction.get("language") or "unknown")
            subject = str(evidence.get("subject") or "unknown")
            accepted_by_language[lang] += 1
            accepted_by_subject[subject] += 1
            accepted_by_bucket[bucket] += 1

    queries = store.all_queries()
    query_docs = {q["item_id"] for q in queries}
    query_status: Counter = Counter(str(q.get("status")) for q in queries)

    return {
        "item_total": len(items),
        "per_source_raw": dict(per_source_raw),
        "per_source_bucket": {k: dict(v) for k, v in per_source_bucket.items()},
        "status_counts": dict(status_counts),
        "accepted_by_language": dict(accepted_by_language),
        "accepted_by_subject": dict(accepted_by_subject),
        "accepted_by_bucket": dict(accepted_by_bucket),
        "rejected_reason_counts": dict(reason_counts),
        "review_reason_counts": dict(review_reason_counts),
        "long_documents": long_documents,
        "total_chars": total_chars,
        "dedup_duplicates": dedup_duplicates,
        "near_duplicates": near_duplicates,
        "artifacts_total": len(artifacts),
        "extractions_total": len(extractions),
        "query_total": len(queries),
        "query_documents": len(query_docs),
        "query_status": dict(query_status),
        "llm_stats": store.llm_call_stats(),
    }


def quota_gap(facts: Mapping[str, Any], policy: CollectionPolicy) -> dict[str, Any]:
    """对照验收配额算缺口。"""
    accepted_lang = facts["accepted_by_language"]
    accepted_subject = facts["accepted_by_subject"]
    accepted_bucket = facts["accepted_by_bucket"]
    languages = facts.get("_target_languages") or list(policy.target_languages)
    min_per_lang = policy.min_documents_per_language
    subjects_p1 = list(policy.subjects.get("p1") or [])

    lang_rows = []
    for lang in languages:
        got = int(accepted_lang.get(lang, 0))
        lang_rows.append({"language": lang, "required": min_per_lang, "accepted": got,
                          "gap": max(0, min_per_lang - got),
                          "met": got >= min_per_lang})
    subject_rows = []
    for subject in subjects_p1:
        got = int(accepted_subject.get(subject, 0))
        subject_rows.append({"subject": subject, "required": 1, "accepted": got,
                             "gap": max(0, 1 - got), "met": got >= 1})
    format_rows = []
    for bucket, target in FORMAT_SOFT_TARGETS.items():
        got = int(accepted_bucket.get(bucket, 0))
        format_rows.append({"format": bucket, "soft_target": target, "accepted": got,
                            "gap": max(0, target - got)})
    # 在线 Docx / Sheet / Slides 等桶不在 6.3 的软目标里，但确实会被接受。
    # 不列出来的话表格加总会小于 accepted 总数，业务方会以为报告漏算了。
    for bucket in sorted(set(accepted_bucket) - set(FORMAT_SOFT_TARGETS)):
        format_rows.append({"format": bucket, "soft_target": None,
                            "accepted": int(accepted_bucket[bucket]), "gap": None})
    return {
        "languages": lang_rows,
        "subjects": subject_rows,
        "formats": format_rows,
        "language_quota_met": all(r["met"] for r in lang_rows),
        "subject_quota_met": all(r["met"] for r in subject_rows),
        "total_required_documents": min_per_lang * len(languages),
        "total_accepted": sum(int(v) for v in accepted_lang.values()),
    }


def write_coverage_report(path: str, *, facts: Mapping[str, Any], gaps: Mapping[str, Any],
                          policy: CollectionPolicy, run_id: str,
                          sources: Sequence[Mapping[str, Any]]) -> None:
    names = [str(s["name"]) for s in sources]
    lines: list[str] = []
    lines.append("# 采集覆盖与淘汰报告")
    lines.append("")
    lines.append(f"- run_id：`{run_id}`")
    lines.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 策略文件：`{os.path.relpath(policy.path)}`")
    revisions = policy.section("requirement_revisions")
    if revisions:
        rev_text = "；".join(
            f"{k} revision {v.get('revision_id')}" for k, v in revisions.items()
            if isinstance(v, Mapping)
        )
        lines.append(f"- 需求依据：{rev_text}")
    lines.append(f"- 采集源（只读）：{'、'.join(f'`{n}`' for n in names)}")
    lines.append("")

    lines.append("## 1. 盘点与合并")
    lines.append("")
    lines.append("| 源文件夹名称 | 角色 | 非文件夹资源数 |")
    lines.append("|-|-|-|")
    for src in sources:
        name = str(src["name"])
        lines.append(f"| {name} | {src.get('role','')} | {int(facts['per_source_raw'].get(name, 0))} |")
    lines.append(f"| **合计（去重前）** | | **{int(facts['item_total'])}** |")
    lines.append("")
    lines.append(f"- 精确/正文重复淘汰：**{int(facts['dedup_duplicates'])}** 条")
    lines.append(f"- 疑似近重复（只标记不淘汰）：**{int(facts['near_duplicates'])}** 条")
    lines.append("")

    lines.append("## 2. 每个源文件夹的文件类型数量")
    lines.append("")
    lines.append("| 文件类型 | " + " | ".join(names) + " | 合计 |")
    lines.append("|" + "-|" * (len(names) + 2))
    buckets = list(DISPLAY_ORDER)
    extra = sorted({b for n in names for b in facts["per_source_bucket"].get(n, {})} - set(buckets))
    for bucket in buckets + extra:
        cells = [str(int(facts["per_source_bucket"].get(n, {}).get(bucket, 0))) for n in names]
        lines.append(f"| {bucket} | " + " | ".join(cells) + f" | {sum(int(c) for c in cells)} |")
    totals = [str(int(facts["per_source_raw"].get(n, 0))) for n in names]
    lines.append("| **合计** | " + " | ".join(totals) + f" | {sum(int(t) for t in totals)} |")
    lines.append("")
    lines.append("> 以上是原始候选数量，不等于最终合格数量。")
    lines.append("")

    lines.append("## 3. 筛选结果")
    lines.append("")
    lines.append("| 状态 | 数量 |")
    lines.append("|-|-|")
    for status, count in sorted(facts["status_counts"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{status}` | {count} |")
    lines.append("")
    if facts["rejected_reason_counts"]:
        lines.append("### 3.1 淘汰原因分布（只统计真正决定结论的原因，不含已通过的门禁）")
        lines.append("")
        lines.append("| 原因码 | 次数 |")
        lines.append("|-|-|")
        for code, count in sorted(facts["rejected_reason_counts"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| `{code}` | {count} |")
        lines.append("")
    if facts["review_reason_counts"]:
        lines.append("### 3.2 待人工复核原因分布（同上口径）")
        lines.append("")
        lines.append("| 原因码 | 次数 |")
        lines.append("|-|-|")
        for code, count in sorted(facts["review_reason_counts"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| `{code}` | {count} |")
        lines.append("")

    lines.append("## 4. 配额缺口")
    lines.append("")
    lines.append("### 4.1 P0 语种（每种至少 %d 份）" % policy.min_documents_per_language)
    lines.append("")
    lines.append("| 语种 | 需求 | 已合格 | 缺口 | 是否达标 |")
    lines.append("|-|-|-|-|-|")
    for row in gaps["languages"]:
        lines.append(f"| {row['language']} | {row['required']} | {row['accepted']} | "
                     f"{row['gap']} | {'✅' if row['met'] else '❌'} |")
    lines.append("")
    lines.append("### 4.2 P1 学科（每类至少 1 份）")
    lines.append("")
    lines.append("| 学科 | 需求 | 已合格 | 缺口 | 是否达标 |")
    lines.append("|-|-|-|-|-|")
    for row in gaps["subjects"]:
        lines.append(f"| {row['subject']} | {row['required']} | {row['accepted']} | "
                     f"{row['gap']} | {'✅' if row['met'] else '❌'} |")
    lines.append("")
    lines.append("### 4.3 格式分布（软目标，不是硬淘汰条件）")
    lines.append("")
    lines.append("| 格式 | 软目标 | 已合格 | 差额 |")
    lines.append("|-|-|-|-|")
    soft_total = 0
    for row in gaps["formats"]:
        target = "—" if row["soft_target"] is None else row["soft_target"]
        gap = "—" if row["gap"] is None else row["gap"]
        soft_total += int(row["accepted"])
        lines.append(f"| {row['format']} | {target} | {row['accepted']} | {gap} |")
    lines.append(f"| **合计** | | **{soft_total}** | |")
    lines.append("")
    lines.append("> 软目标为 `—` 的行（在线 Docx / Sheet / Slides 等）不在需求 6.3 的 "
                 "3:2:2:1:1:1 比例里，但同样计入合格总数。")
    lines.append("")

    lines.append("## 5. 长文档与字数")
    lines.append("")
    lines.append(f"- 已抽取条目：{int(facts['extractions_total'])}")
    lines.append(f"- 累计字符数：{int(facts['total_chars'])}")
    lines.append(f"- 超过 {policy.full_text_review_threshold_chars} 字符的长文档："
                 f"{int(facts['long_documents'])}（不淘汰，需走全文理解）")
    lines.append("")

    lines.append("## 6. Query")
    lines.append("")
    lines.append(f"- 已生成 query：{int(facts['query_total'])} 条，覆盖 {int(facts['query_documents'])} 份文档")
    if facts["query_status"]:
        lines.append(f"- 状态分布：{json.dumps(facts['query_status'], ensure_ascii=False)}")
    lines.append(f"- 每份文档要求：{policy.queries_per_document} 条")
    lines.append("")

    lines.append("## 7. 成本与耗时")
    lines.append("")
    llm = facts["llm_stats"]
    lines.append(f"- 模型调用：{int(llm.get('calls', 0))} 次（成功 {int(llm.get('ok_calls', 0))} 次）")
    lines.append(f"- 输入字符：{int(llm.get('prompt_chars', 0))}；输出字符：{int(llm.get('output_chars', 0))}")
    lines.append(f"- 累计时延：{int(llm.get('latency_ms', 0))} ms")
    lines.append("")

    lines.append("## 8. 口径与安全声明")
    lines.append("")
    lines.append("- 全过程只读：未删除、移动、重命名或修改任何云盘源文件。")
    lines.append("- 飞书 `drive files list` / `metas batch_query` 不返回文件大小，"
                 "大小门槛按下载后的本地字节数判定。")
    lines.append("- 未启用模型时，语义项（教育属性/学科/可出题性）一律转 `needs_review`，不自动放行。")
    lines.append("- 本报告数字均由 SQLite 现算，可与 `screening_report.jsonl` 逐条对齐。")
    winpath.makedirs(os.path.dirname(os.path.abspath(path)))
    with winpath.open_write(path, encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def write_cost_and_runtime(path: str, *, run_id: str, facts: Mapping[str, Any],
                           stage_summaries: Mapping[str, Any]) -> None:
    payload = {
        "run_id": run_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "llm": facts.get("llm_stats", {}),
        "counts": {
            "items": facts.get("item_total"),
            "artifacts": facts.get("artifacts_total"),
            "extractions": facts.get("extractions_total"),
            "queries": facts.get("query_total"),
            "total_chars": facts.get("total_chars"),
        },
        "stages": {k: {kk: vv for kk, vv in (v or {}).items() if kk != "artifacts"}
                   for k, v in (stage_summaries or {}).items() if isinstance(v, Mapping)},
    }
    winpath.makedirs(os.path.dirname(os.path.abspath(path)))
    with winpath.open_write(path, encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)


def write_delivery_summary(path: str, *, store: StateStore, policy: CollectionPolicy,
                           run_id: str) -> int:
    """一等人读汇总：每份文档一行 —— 文件名 / 类型 / 主题 / 语言 / 学科 / query 数。

    为什么单独做这一张表：原来的产物是一堆按阶段分文件的 JSONL/CSV（inventory、
    acquisition、screening、query_records…），可追溯做到了，但**没有一个"一眼看清
    这批交付了什么"的视图**。上一次跑完 30 篇中文文档时，用户连着两次追问
    "怎么没有汇总报告""每篇对应什么题目"，就是缺这一层。

    只读状态库，不重新计算任何结论。返回写入的行数。
    """
    items = {str(i["item_id"]): i for i in store.list_items()}
    decisions = store.latest_decisions("screen")
    queries_by_item: dict[str, list[dict[str, Any]]] = {}
    for q in store.all_queries():
        queries_by_item.setdefault(str(q["item_id"]), []).append(q)

    rows: list[dict[str, Any]] = []
    for item_id, item in items.items():
        decision = decisions.get(item_id)
        evidence = (decision or {}).get("evidence") or {}
        queries = queries_by_item.get(item_id, [])
        type_brief = "/".join(
            f"{q.get('type_key')}x{q.get('question_count')}"
            for q in sorted(queries, key=lambda r: r.get("seq", 0))
        )
        rows.append({
            "文件名": item.get("name"),
            "文件类型": (item.get("extension") or item.get("item_type") or "").upper(),
            "主题（来自标题）": os.path.splitext(str(item.get("name") or ""))[0][:60],
            "学科": (evidence.get("subject") or evidence.get("subject_hint") or ""),
            "语种": evidence.get("language") or "",
            "字符数": evidence.get("char_count") or "",
            "Query数": len(queries),
            "题型概览": type_brief,
            "状态": (decision or {}).get("status") or item.get("status"),
            "item_id": item_id,
        })
    rows.sort(key=lambda r: (str(r["状态"]) != ITEM_ACCEPTED, str(r["文件名"])))

    fieldnames = ["文件名", "文件类型", "主题（来自标题）", "学科", "语种",
                  "字符数", "Query数", "题型概览", "状态", "item_id"]
    winpath.makedirs(os.path.dirname(os.path.abspath(path)))
    with winpath.open_write(path, encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    # 同时写一份 Markdown，便于直接贴到在线文档/IM
    md_path = os.path.splitext(path)[0] + ".md"
    accepted_rows = [r for r in rows if r["状态"] == ITEM_ACCEPTED]
    lines = [
        "# 交付汇总",
        "",
        f"- run_id：`{run_id}`",
        f"- 条目总数：{len(rows)}；合格（accepted）：{len(accepted_rows)}",
        f"- 已生成 query 的文档：{sum(1 for r in rows if r['Query数'])}",
        "",
        "| 文件名 | 类型 | 学科 | 语种 | 字符数 | Query数 | 状态 |",
        "|-|-|-|-|-|-|-|",
    ]
    for r in rows[:200]:
        lines.append(
            f"| {str(r['文件名'])[:48]} | {r['文件类型']} | {r['学科']} | {r['语种']} "
            f"| {r['字符数']} | {r['Query数']} | {r['状态']} |"
        )
    with winpath.open_write(md_path, encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return len(rows)


def run_report(*, policy: CollectionPolicy, store: StateStore, workspace_dir: str,
               run_id: str) -> dict[str, Any]:
    """生成全部报告产物，返回 RunResult 用的摘要。"""
    winpath.makedirs(workspace_dir)
    facts = collect_facts(store, policy)
    gaps = quota_gap(facts, policy)
    run_row = store.get_run(run_id) or {}
    stage_summaries = {}
    try:
        stage_summaries = json.loads(run_row.get("stage_state_json") or "{}")
    except json.JSONDecodeError:
        stage_summaries = {}

    sources = policy.sources
    coverage_path = os.path.join(workspace_dir, "coverage_report.md")
    cost_path = os.path.join(workspace_dir, "cost_and_runtime.json")
    delivery_path = os.path.join(workspace_dir, "delivery_summary.csv")
    write_coverage_report(coverage_path, facts=facts, gaps=gaps, policy=policy,
                          run_id=run_id, sources=sources)
    write_cost_and_runtime(cost_path, run_id=run_id, facts=facts,
                           stage_summaries=stage_summaries)
    write_delivery_summary(delivery_path, store=store, policy=policy, run_id=run_id)

    # 汇总 CSV：把 accepted/rejected/review 合成一张带来源与理由的总表
    all_decisions_path = os.path.join(workspace_dir, "all_decisions.csv")
    rows: list[dict[str, Any]] = []
    decisions = store.latest_decisions("screen")
    items_by_id = {str(i["item_id"]): i for i in store.list_items()}
    for item_id, decision in decisions.items():
        item = items_by_id.get(item_id, {})
        meta = item.get("metadata") or {}
        rows.append({
            "item_id": item_id,
            "name": item.get("name"),
            "source_name": item.get("source_name"),
            "source_path": f"{item.get('source_name')}/{meta.get('relative_path') or item.get('name')}",
            "url": item.get("url"),
            "item_type": item.get("item_type"),
            "extension": item.get("extension"),
            "status": decision.get("status"),
            "deciding_reason": deciding_reason(decision.get("reason_codes") or []),
            "reason_codes": ";".join(str(c) for c in (decision.get("reason_codes") or [])),
            "language": (decision.get("evidence") or {}).get("language"),
            "char_count": (decision.get("evidence") or {}).get("char_count"),
            "size_bytes": (decision.get("evidence") or {}).get("size_bytes"),
            "education_level": (decision.get("evidence") or {}).get("education_level")
            or (decision.get("evidence") or {}).get("education_level_hint"),
            "subject": (decision.get("evidence") or {}).get("subject")
            or (decision.get("evidence") or {}).get("subject_hint"),
            "artifact_path": (store.get_artifact(item_id) or {}).get("path"),
        })
    rows.sort(key=lambda r: (str(r["status"]), str(r["source_name"]), str(r["source_path"])))
    if rows:
        with winpath.open_write(all_decisions_path, encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    return {
        "facts": facts,
        "quota_gap": gaps,
        "artifacts": [coverage_path, cost_path, all_decisions_path,
                      delivery_path, os.path.splitext(delivery_path)[0] + ".md"],
        "status_counts": facts["status_counts"],
        "accepted_total": gaps["total_accepted"],
        "language_quota_met": gaps["language_quota_met"],
        "subject_quota_met": gaps["subject_quota_met"],
    }
