"""参考题目生成（Task 7 之后的新能力，改动计划 P3/P4/P8）。

背景：Core 原本没有"题目生成"能力，参考题目（题干/选项/答案/解析）只能靠
`output/_skill_debug/.../gen_questions.py` 这类一次性脚本 —— 无参数化、无测试、
无产物契约，且脚本只写 jsonl 不写状态库，重演了状态库/jsonl 不一致（debug D5/D9）。
本模块把它提为正式能力：

- 消费上一阶段 `query_records.jsonl` 里 **validated** 的文档与其 10 条 query；
- 复用 litellm 适配器与预算处理（同一个 `complete()`，同样的 429/5xx 退避）；
- 输入窗口与 query 同源（`content_rules.prompt_text_chars`）；
- 产物契约对齐 query：`questions/<item_id>.json` + `question_records.jsonl` + `question_summary.json`，
  并**同时 upsert 状态库 questions 表**（jsonl 与库同写，P4）；
- 解析后做**键名归一化**（P3），把 explication/شرح 等漂移键折回 explanation；
- 断点续跑：`questions/<item_id>.json` 已存在且条数一致则跳过。
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from . import winpath
from .config import CollectionPolicy
from .llm import normalize_question
from .parallel import map_ordered
from .state import StateStore
from .textstats import truncate_for_prompt

LogFn = Callable[[str], None]


def _noop(msg: str) -> None:  # pragma: no cover
    pass


@dataclass
class QuestionOptions:
    limit: int | None = None
    item_ids: tuple[str, ...] = ()
    concurrency: int = 1
    # 喂给模型的正文窗口（字符）。None = 取策略 content_rules.prompt_text_chars。
    text_sample_chars: int | None = None
    resume: bool = True


def _load_text(item_id: str, workspace_dir: str,
               extraction: Mapping[str, Any] | None) -> str:
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


def read_validated_query_records(path: str) -> list[dict[str, Any]]:
    """读 query_records.jsonl，只保留 validated 且带 queries 的记录。"""
    if not winpath.isfile(path):
        return []
    out: list[dict[str, Any]] = []
    with winpath.open_read(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("status") == "validated" and record.get("queries"):
                out.append(record)
    return out


def _count_questions(payload: Mapping[str, Any]) -> int:
    return sum(len(it.get("questions") or []) for it in (payload.get("items") or []))


def generate_for_item(*, record: Mapping[str, Any], extraction: Mapping[str, Any] | None,
                      policy: CollectionPolicy, llm: Any, workspace_dir: str,
                      questions_dir: str, opts: QuestionOptions) -> dict[str, Any]:
    """为一份文档生成全部参考题目（10 条 query -> 题目集）。"""
    item_id = str(record["item_id"])
    queries = list(record.get("queries") or [])
    out_path = os.path.join(questions_dir, f"{item_id}.json")
    result: dict[str, Any] = {
        "item_id": item_id, "name": record.get("name"),
        "language": record.get("document_language"),
        "query_count": len(queries), "status": "generated",
        "question_total": 0, "reason_codes": [], "error": None,
    }

    # 断点续跑：已存在且 query 条数一致则跳过
    if opts.resume and winpath.isfile(out_path):
        try:
            existing = json.loads(winpath.open_read(out_path, encoding="utf-8-sig").read())
            if len(existing.get("items") or []) == len(queries) and existing.get("items"):
                result.update({"status": "skipped",
                               "question_total": int(existing.get("question_total") or 0)})
                return result
        except (OSError, json.JSONDecodeError):
            pass

    text = _load_text(item_id, workspace_dir, extraction)
    if not text.strip():
        result.update({"status": "needs_review",
                       "reason_codes": ["no_text_for_question_generation"]})
        return result

    max_chars = opts.text_sample_chars or policy.prompt_text_chars
    prompt_text = truncate_for_prompt(text, max_chars)
    document_ref = {
        "item_id": item_id, "name": record.get("name"),
        "language": record.get("document_language") or "unknown",
        "text_chars": len(text), "prompt_chars": len(prompt_text),
        "truncated": len(text) > max_chars, "text": prompt_text,
    }
    result["text_chars"] = len(text)
    result["prompt_chars"] = len(prompt_text)
    result["truncated"] = len(text) > max_chars

    try:
        payload = llm.generate_questions(document_ref, queries, {
            **policy.query_plan, "max_tokens": int(policy.question_generation.get(
                "max_tokens", policy.llm.get("max_tokens", 32000))),
        })
    except Exception as exc:  # noqa: BLE001
        from .query_generation import _classify_llm_failure
        result.update({"status": "needs_review",
                       "reason_codes": [_classify_llm_failure(exc)],
                       "error": f"{type(exc).__name__}: {exc}"[:500]})
        return result

    raw_items = payload.get("items") if isinstance(payload, Mapping) else None
    if not isinstance(raw_items, list):
        result.update({"status": "needs_review", "reason_codes": ["llm_output_invalid"],
                       "error": "模型输出缺少 items 数组"})
        return result

    # 键名归一化（P3）并做结构完整性检查：每条 query 都要有题目
    normalized: list[dict[str, Any]] = []
    for it in raw_items:
        if not isinstance(it, Mapping):
            continue
        questions = [normalize_question(q) for q in (it.get("questions") or [])
                     if isinstance(q, Mapping)]
        normalized.append({"query_seq": int(it.get("query_seq") or len(normalized) + 1),
                           "questions": questions})
    total = sum(len(it["questions"]) for it in normalized)
    if len(normalized) != len(queries) or total == 0:
        result.update({"status": "needs_review", "reason_codes": ["question_structure_incomplete"],
                       "error": f"items={len(normalized)}/{len(queries)} 题目数={total}"})
        return result

    out = {
        "item_id": item_id, "name": record.get("name"),
        "language": record.get("document_language"),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "query_count": len(queries), "question_total": total, "items": normalized,
        "text_chars": result.get("text_chars"), "prompt_chars": result.get("prompt_chars"),
        "truncated": result.get("truncated"),
    }
    winpath.makedirs(questions_dir)
    with winpath.open_write(out_path, encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    result.update({"status": "ok", "question_total": total})
    return result


def run_question_generation(*, policy: CollectionPolicy, store: StateStore, workspace_dir: str,
                            run_id: str, llm: Any, options: QuestionOptions | None = None,
                            log: LogFn = _noop) -> dict[str, Any]:
    """批量生成参考题目，返回 RunResult 用的摘要。"""
    opts = options or QuestionOptions()
    winpath.makedirs(workspace_dir)
    qcfg = policy.question_generation
    questions_dir = os.path.join(workspace_dir, str(qcfg.get("artifact_dir") or "questions"))

    records = read_validated_query_records(os.path.join(workspace_dir, "query_records.jsonl"))
    if opts.item_ids:
        wanted = {str(x) for x in opts.item_ids}
        records = [r for r in records if str(r.get("item_id")) in wanted]
    if opts.limit:
        records = records[: int(opts.limit)]
    log(f"[questions] 待生成 {len(records)} 份文档的参考题目"
        f"（并发 {max(1, opts.concurrency)}，窗口 "
        f"{opts.text_sample_chars or policy.prompt_text_chars} 字符）")

    records_path = os.path.join(workspace_dir, "question_records.jsonl")
    summary_path = os.path.join(workspace_dir, "question_summary.json")
    counts: Counter = Counter()
    error_codes: Counter = Counter()
    all_rows: list[dict[str, Any]] = []
    started = time.time()
    done = {"n": 0}

    extractions = {str(r["item_id"]): store.get_extraction(str(r["item_id"])) for r in records}

    def _work(index: int, record: Mapping[str, Any]) -> dict[str, Any]:
        return generate_for_item(record=record, extraction=extractions[str(record["item_id"])],
                                 policy=policy, llm=llm, workspace_dir=workspace_dir,
                                 questions_dir=questions_dir, opts=opts)

    def _persist(index: int, value: Any) -> None:
        record = records[index]
        item_id = str(record["item_id"])
        if isinstance(value, BaseException):
            from .query_generation import _classify_llm_failure
            result = {"item_id": item_id, "name": record.get("name"),
                      "status": "needs_review", "question_total": 0,
                      "reason_codes": [_classify_llm_failure(value)],
                      "error": f"{type(value).__name__}: {value}"[:500]}
        else:
            result = value
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        for code in result.get("reason_codes") or []:
            error_codes[code] += 1

        # jsonl 与状态库同写（P4）：产物落盘 + questions 表 upsert + 决策链
        out_path = os.path.join(questions_dir, f"{item_id}.json")
        payload: dict[str, Any] | None = None
        if winpath.isfile(out_path):
            try:
                payload = json.loads(winpath.open_read(out_path, encoding="utf-8-sig").read())
            except (OSError, json.JSONDecodeError):
                payload = None
        if payload:
            all_rows.append(payload)
            for it in payload.get("items") or []:
                store.upsert_question(
                    item_id, int(it.get("query_seq") or 0),
                    language=payload.get("language"),
                    payload={"query_seq": it.get("query_seq"),
                             "questions": it.get("questions") or []},
                    status=result["status"],
                    created_at=payload.get("generated_at") or time.strftime("%Y-%m-%dT%H:%M:%S"),
                )
        store.add_decision(item_id, "questions", result["status"],
                           result.get("reason_codes") or ["ok"],
                           {"question_total": result.get("question_total"),
                            "query_count": result.get("query_count"),
                            "truncated": result.get("truncated")})
        done["n"] += 1
        if done["n"] % 10 == 0 or done["n"] == len(records):
            store.commit()
            log(f"[questions] {done['n']}/{len(records)} 状态 {dict(counts)}")

    map_ordered(_work, records, concurrency=opts.concurrency, on_result=_persist)
    store.commit()

    # question_records.jsonl：一行一份文档。**合并写**而不是覆盖——
    # 定向重跑（--item-id）只处理子集，若直接覆盖会把没重跑的文档记录冲掉，
    # 造成 jsonl 落后于状态库（与 P4 要解决的错位是同一类问题）。
    merged: dict[str, dict[str, Any]] = {}
    if winpath.isfile(records_path):
        with winpath.open_read(records_path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    prev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if prev.get("item_id"):
                    merged[str(prev["item_id"])] = prev
    for row in all_rows:
        merged[str(row["item_id"])] = row
    with winpath.open_write(records_path, encoding="utf-8") as fh:
        for key in sorted(merged):
            fh.write(json.dumps(merged[key], ensure_ascii=False, default=str) + "\n")

    total_questions = sum(int(r.get("question_total") or 0) for r in all_rows)
    summary = {
        "total_documents": len(records),
        "status_counts": dict(counts),
        "error_codes": dict(error_codes.most_common()),
        "total_questions": total_questions,
        "documents_with_questions": len(all_rows),
        "artifacts": [records_path, summary_path, questions_dir],
        "elapsed_sec": round(time.time() - started, 2),
    }
    with winpath.open_write(summary_path, encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, default=str)
    store.touch_run(run_id, {"collection.generate_questions": summary})
    log(f"[questions] 完成：{summary['status_counts']}，共 {total_questions} 题")
    return summary
