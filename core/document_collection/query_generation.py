"""Task 7：Query 生成与校验。

需求口径（在线采集计划 4.1 / 4.2，revision 201）：
- 每份文档 10 条 query：选择题2(题量5)、填空题2(3)、解答题2(2)、判断题2(7)、混合题型2(9)；
- 每条 query 请求的题目数必须 < 12；同一份文档内不同题型的题量尽量不重复；
- query 面向整篇文档，**不得**出现片段、章节、页码、Sheet、表格等定位词；
- query 语言跟随文档语言；
- 内部台账保留 evidence_locator（页码/段落/Sheet/表格），但不写进 query 文案；
- 混合题型标记 compatibility_only=true，预期行为"丢弃或降级处理"。

生成失败或校验不过时不产出"看起来像成功"的结果：
条目状态转 needs_review，并把失败原因写进 validation report。
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .config import CollectionPolicy
from .contracts import (
    RC_CONTENT_AUDIT_REJECTED,
    RC_LLM_BUDGET_EXHAUSTED,
    RC_QUERY_COUNT,
    RC_QUERY_DUP,
    RC_QUERY_FRAGMENT_REF,
    RC_QUERY_LANG,
    RC_QUERY_NO_EVIDENCE,
    RC_QUERY_QUESTION_COUNT,
    RC_QUERY_QUESTION_DUP,
    RC_QUERY_STYLE_REPEATED,
)
from .state import StateStore
from .parallel import map_ordered
from . import winpath
from .textstats import detect_language, normalize_text, truncate_for_prompt

LogFn = Callable[[str], None]


def _noop(msg: str) -> None:  # pragma: no cover
    pass


# 定位词黑名单：query 文案里出现这些就判不合格（多语种）
_FRAGMENT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("zh", r"第\s*[0-9一二三四五六七八九十百]+\s*(章|节|页|部分|篇|单元|讲|课)"),
    ("zh", r"(本章|本节|本页|上一章|下一节|上文|下文|前述章节)"),
    ("zh", r"(如表|见下表|上表|下表|表中|表格\s*[0-9一二三四五六七八九十]+|工作表| sheet)"),
    ("zh", r"(如图所示|见下图|上图|下图|插图|示意图\s*[0-9]+)"),
    ("en", r"\b(chapter|section|page|part|unit|lesson|slide|sheet|worksheet|table|figure|appendix)\s*[#]?\s*\d+\b"),
    ("en", r"\b(in the (above|following|previous) (section|chapter|table|figure|page))\b"),
    ("en", r"\b(on page|see table|see figure|as shown in)\b"),
    ("fr", r"\b(chapitre|section|page|partie|tableau|feuille|figure|diapositive|annexe)\s*(n[°o]?\s*)?\d+\b"),
    ("fr", r"\b(dans le (chapitre|tableau|section) (ci-dessus|suivant|précédent))\b"),
    ("fr", r"\b(voir (le )?(tableau|la figure|la page))\b"),
    ("es", r"\b(cap[íi]tulo|secci[óo]n|p[áa]gina|parte|tabla|hoja|figura|diapositiva|anexo)\s*(n[°o]?\s*)?\d+\b"),
    ("es", r"\b(en (el|la) (cap[íi]tulo|tabla|secci[óo]n|p[áa]gina) (anterior|siguiente|de arriba))\b"),
    ("es", r"\b(ver (la )?(tabla|figura|p[áa]gina))\b"),
    ("ru", r"\b(глава|раздел|страница|часть|таблица|лист|рисунок|слайд|приложение)\s*(№\s*)?\d+\b"),
    ("ru", r"\b(в (этой|следующей|предыдущей) (главе|разделе|таблице|части))\b"),
    ("ru", r"\b(см\.\s*(таблицу|рисунок|страницу))\b"),
    ("ar", r"(الفصل|الصفحة|الجدول|القسم|الجزء|الشكل|الشريحة)\s*(رقم\s*)?[0-9٠-٩]+"),
    ("ar", r"(في (الجدول|الفصل|الصفحة) (السابق|التالي|أعلاه))"),
)

_COMPILED_FRAGMENT_PATTERNS = tuple(
    (lang, re.compile(pat, re.IGNORECASE)) for lang, pat in _FRAGMENT_PATTERNS
)

# 同一脚本族内语言可互通（例如 fr/es 都是拉丁），语言校验按族放宽
_SCRIPT_FAMILY = {
    "en": "latin", "fr": "latin", "es": "latin", "pt": "latin", "de": "latin",
    "it": "latin", "tr": "latin",
    "zh": "cjk", "ja": "cjk",
    "ru": "cyrillic", "uk": "cyrillic", "bg": "cyrillic",
    "ar": "arabic", "he": "hebrew", "th": "thai", "el": "greek", "hi": "devanagari",
    "ko": "hangul",
}


@dataclass
class QueryOptions:
    limit: int | None = None
    include_statuses: tuple[str, ...] = ("accepted",)
    # 定向白名单：只给这些 item_id 生成。空 = 不限制（走 include_statuses）。
    # 上一次 30 篇中文任务就是因为没有这个字段，只能写一次性脚本绕开 CLI。
    item_ids: tuple[str, ...] = ()
    # 并发 worker 数：生题几乎全耗时在模型往返，并发 4-8 倍提速且不改变结论
    concurrency: int = 1
    # 喂给模型的正文窗口（字符）。None = 取策略 content_rules.prompt_text_chars（默认 5 万），
    # 这是端侧/云侧入库窗口的同一口径；不在代码里写死，避免与 policy 漂移。
    text_sample_chars: int | None = None
    allow_partial: bool = True
    require_evidence_locator: bool = True
    strict_language: bool = False


def find_fragment_references(text: str) -> list[str]:
    """返回命中的定位词证据（用于报告里说明为什么拒绝）。"""
    if not text:
        return []
    hits: list[str] = []
    for lang, pattern in _COMPILED_FRAGMENT_PATTERNS:
        for match in pattern.finditer(text):
            hits.append(f"{lang}:{match.group(0).strip()}")
    # 去重但保序
    seen: set[str] = set()
    out: list[str] = []
    for hit in hits:
        key = hit.lower()
        if key not in seen:
            seen.add(key)
            out.append(hit)
    return out


# 句式签名：抽出 query 开头的"动作词 + 语气"，把同质化的开场收敛到同一签名。
# 目标不是精确分类，而是抓住"多条 query 都用请根据/请基于/请依据"这类重复。
_OPENING_STOPWORDS = (
    "这篇", "这份", "本文", "本材料", "本份", "该文档", "此文档", "上述", "下列", "下面",
    "所给", "给定", "整篇", "整体", "全文", "提供", "这些", "以下",
)
_OPENING_VERBS = (
    "请根据", "请基于", "请依据", "请结合", "请围绕", "请针对", "请为我", "请帮我", "请帮忙",
    "请从", "请以", "请用", "请按", "请按照", "请你",
    "根据", "基于", "依据", "结合", "围绕", "针对", "按照", "使用", "利用",
    "给我", "帮我", "麻烦", "我需要", "我想", "来", "出", "编", "写", "设计", "拟", "命制", "编写",
)


def opening_signature(text: str) -> str:
    """抽取 query 的**开场签名**（粗粒度，够判断"是否老用同一个套路"）。

    中文优先匹配"请根据/请基于/帮我/给我…"这类开场动词；其他语种取第一个词。
    返回小写签名串，供 validate_queries 统计重复。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    stripped = raw.lower().lstrip(" 　\t。，,.、:：;；!！?？\"'“”‘’()（）[]【】")
    # 中文/东亚：找最长匹配的开场动词（覆盖面词"请根据/给我"，也覆盖"围绕/设计"等直接指令）
    best = ""
    for verb in _OPENING_VERBS:
        if stripped.startswith(verb.lower()) and len(verb) > len(best):
            best = verb
    if best:
        return best
    # 其他语种：取第一个词（字母/数字骨架），去掉标点
    token = re.split(r"[\s,.;:!?，。；：！？]+", stripped)
    token = [t for t in token if t]
    return (token[0] if token else "")[:20]


def validate_queries(queries: Sequence[Mapping[str, Any]], *,
                     policy: CollectionPolicy, document_language: str,
                     opts: QueryOptions | None = None) -> dict[str, Any]:
    """对一份文档的 query 集合做全部确定性校验。"""
    opts = opts or QueryOptions()
    qp = policy.query_plan
    types = policy.query_types
    expected_total = policy.queries_per_document
    qmin = int(qp.get("question_count_min", 2))
    qmax = int(qp.get("question_count_max", 11))

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    if len(queries) != expected_total:
        errors.append({"code": RC_QUERY_COUNT,
                       "message": f"query 数量 {len(queries)} != 需求 {expected_total}"})

    expected_by_type = Counter({str(t["type_key"]): int(t["count"]) for t in types})
    actual_by_type = Counter(str(q.get("type_key") or "") for q in queries)
    for type_key, expected in expected_by_type.items():
        actual = actual_by_type.get(type_key, 0)
        if actual != expected:
            errors.append({"code": RC_QUERY_COUNT,
                           "message": f"题型 {type_key} 数量 {actual} != 需求 {expected}"})
    unexpected = set(actual_by_type) - set(expected_by_type)
    for type_key in sorted(unexpected):
        errors.append({"code": RC_QUERY_COUNT,
                       "message": f"出现未定义题型 {type_key}（{actual_by_type[type_key]} 条）"})

    expected_qc = {str(t["type_key"]): int(t["question_count"]) for t in types}
    compatibility_keys = {str(t["type_key"]) for t in types if t.get("compatibility_only")}
    seen_qc: dict[int, list[str]] = {}
    seen_texts: dict[str, int] = {}
    seen_styles: dict[str, list[int]] = {}

    for index, query in enumerate(queries, start=1):
        type_key = str(query.get("type_key") or "")
        text = str(query.get("text") or "").strip()
        qc_raw = query.get("question_count")
        locator = query.get("evidence_locator")
        lang = str(query.get("language") or "").lower()

        if not text:
            errors.append({"code": RC_QUERY_COUNT, "seq": index,
                           "message": "query 文案为空"})
            continue

        # 题量范围
        try:
            qc = int(qc_raw)
        except (TypeError, ValueError):
            errors.append({"code": RC_QUERY_QUESTION_COUNT, "seq": index,
                           "message": f"question_count 非法：{qc_raw!r}"})
            qc = None
        if qc is not None:
            if not (qmin <= qc <= qmax):
                errors.append({"code": RC_QUERY_QUESTION_COUNT, "seq": index,
                               "message": f"题量 {qc} 不在 [{qmin}, {qmax}]（需求要求 < 12）"})
            if type_key in expected_qc and qc != expected_qc[type_key]:
                warnings.append({"code": RC_QUERY_QUESTION_COUNT, "seq": index,
                                 "message": f"题量 {qc} 与题型建议值 {expected_qc[type_key]} 不一致"})
            seen_qc.setdefault(qc, []).append(type_key)

        # 定位词
        hits = find_fragment_references(text)
        if hits:
            errors.append({"code": RC_QUERY_FRAGMENT_REF, "seq": index,
                           "message": "query 文案包含片段/章节/页码/表格定位词",
                           "hits": hits[:8]})

        # 语言
        detected, confidence = detect_language(text)
        if document_language and document_language != "unknown":
            same = (detected == document_language)
            if not same and not opts.strict_language:
                same = _SCRIPT_FAMILY.get(detected) == _SCRIPT_FAMILY.get(document_language)
            if not same:
                errors.append({"code": RC_QUERY_LANG, "seq": index,
                               "message": f"query 语言 {detected} 与文档语言 {document_language} 不一致",
                               "confidence": confidence})
        if lang and document_language and lang != document_language:
            warnings.append({"code": RC_QUERY_LANG, "seq": index,
                             "message": f"声明语言 {lang} 与文档语言 {document_language} 不一致"})

        # 重复
        key = normalize_text(text)
        if key in seen_texts:
            errors.append({"code": RC_QUERY_DUP, "seq": index,
                           "message": f"与第 {seen_texts[key]} 条 query 正文重复"})
        else:
            seen_texts[key] = index

        # 句式同质化：记录本条的开场套路，循环结束后统一判定
        seen_styles.setdefault(opening_signature(text), []).append(index)

        # 证据
        if opts.require_evidence_locator and not locator:
            errors.append({"code": RC_QUERY_NO_EVIDENCE, "seq": index,
                           "message": "缺少 evidence_locator（内部台账必须保留定位）"})

        # 混合题型标记
        if type_key in compatibility_keys and not query.get("compatibility_only"):
            warnings.append({"code": "compatibility_flag_missing", "seq": index,
                             "message": f"{type_key} 应标记 compatibility_only=true"})

    # 题量尽量不重复：按题型维度看
    if qp.get("question_counts_should_not_repeat", True):
        type_qc: dict[str, set[int]] = {}
        for query in queries:
            tk = str(query.get("type_key") or "")
            try:
                type_qc.setdefault(tk, set()).add(int(query.get("question_count")))
            except (TypeError, ValueError):
                continue
        collapsed = {tk: sorted(v) for tk, v in type_qc.items() if len(v) == 1}
        counter = Counter(v[0] for v in collapsed.values())
        for qc, times in counter.items():
            if times > 1:
                warnings.append({"code": RC_QUERY_QUESTION_DUP,
                                 "message": f"题量 {qc} 被 {times} 个题型重复使用（需求要求尽量不重复）"})

    # 句式多样化：同一文档内同一开场套路最多出现 max_style_repeat 次。
    # 需求明确"句式类型不能过于重复"，这是**错误**（不是警告）——句式和正文一样是交付质量的一部分。
    # 默认 3：10 条 query 分 5 类题型，按题型换开场时每签名 2 次属正常；
    # 上限 3 既容得下合理复用，又能拦住"10 条全用请根据本文档"这种真雷同（历史实测 33 次）。
    max_repeat = int(qp.get("max_style_repeat", 3))
    for sig, seqs in seen_styles.items():
        if len(seqs) > max_repeat:
            errors.append({"code": RC_QUERY_STYLE_REPEATED,
                           "message": f"开场句式「{sig}」被 {len(seqs)} 条 query 重复使用"
                                      f"（上限 {max_repeat}）：{seqs}",
                           "seq": seqs[0], "signature": sig, "count": len(seqs)})

    return {
        "ok": not errors,
        "query_count": len(queries),
        "expected_count": expected_total,
        "errors": errors,
        "warnings": warnings,
        "type_counts": dict(actual_by_type),
        "question_counts": {str(k): v for k, v in sorted(seen_qc.items())},
        "document_language": document_language,
    }


def build_document_ref(item: Mapping[str, Any], extraction: Mapping[str, Any] | None,
                       text: str, max_chars: int) -> dict[str, Any]:
    """组装喂给模型的文档引用；**只在这里做一次截断**（输入窗口的唯一执行点）。

    提示词模板不再自己切一刀（旧实现 llm.py 里还有一处 text[:16000]），
    否则改 policy 不生效、且两次截断口径不同。截断留痕（原始/入模字符数、是否截断）
    一并写进 document_ref，供 query 产物与报告记录"质量下限"。
    """
    prompt_text = truncate_for_prompt(text, max_chars)
    return {
        "item_id": item.get("item_id"),
        "name": item.get("name"),
        "source_name": item.get("source_name"),
        "relative_path": (item.get("metadata") or {}).get("relative_path"),
        "extension": item.get("extension"),
        "language": (extraction or {}).get("language") or "unknown",
        "char_count": (extraction or {}).get("char_count"),
        "page_count": (extraction or {}).get("page_count"),
        "text_chars": len(text),
        "prompt_chars": len(prompt_text),
        "truncated": len(text) > max_chars,
        "text": prompt_text,
    }


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


def _classify_llm_failure(exc: Exception) -> str:
    """把模型调用失败归类成 reason_code。

    `content_audit_rejected` 单独成一类：网关对含敏感内容的输入返回
    `BadRequestError ... inappropriate content`，属**外部内容审核**，重跑必复现，
    处置是换源而不是重试——不能和"输出格式不对（llm_output_invalid）"混为一谈。
    """
    name = type(exc).__name__
    if name == "LLMUnavailable":
        return "llm_unavailable"
    if name == "LLMBudgetExhausted":
        # 推理预算被吃光：调大 policy.llm.max_tokens 后重跑，不要当格式错误修提示词
        return RC_LLM_BUDGET_EXHAUSTED
    text = str(exc).lower()
    if "inappropriate content" in text or "content_policy" in text or "content policy" in text:
        return RC_CONTENT_AUDIT_REJECTED
    return "llm_output_invalid"


def generate_for_item(*, item: Mapping[str, Any], extraction: Mapping[str, Any] | None,
                      policy: CollectionPolicy, llm: Any, workspace_dir: str,
                      opts: QueryOptions) -> dict[str, Any]:
    """为单份文档生成并校验 10 条 query。"""
    item_id = str(item["item_id"])
    text = _load_text(item_id, workspace_dir, extraction)
    document_language = str((extraction or {}).get("language") or "unknown")
    record: dict[str, Any] = {
        "item_id": item_id, "name": item.get("name"),
        "source_name": item.get("source_name"),
        "document_language": document_language,
        "status": "generated", "queries": [], "validation": {},
        "reason_codes": [], "error": None,
    }
    if not text.strip():
        record["status"] = "needs_review"
        record["reason_codes"] = ["no_text_for_query_generation"]
        return record

    max_chars = opts.text_sample_chars or policy.prompt_text_chars
    document_ref = build_document_ref(item, extraction, text, max_chars)
    record["text_chars"] = document_ref["text_chars"]
    record["prompt_chars"] = document_ref["prompt_chars"]
    record["truncated"] = document_ref["truncated"]
    try:
        payload = llm.generate_queries(document_ref, {
            **policy.query_plan,
            "max_tokens": int(policy.llm.get("max_tokens", 6000)),
        })
    except Exception as exc:  # noqa: BLE001
        record["status"] = "needs_review"
        record["error"] = f"{type(exc).__name__}: {exc}"[:500]
        record["reason_codes"] = [_classify_llm_failure(exc)]
        return record

    raw_queries = payload.get("queries") if isinstance(payload, Mapping) else None
    if not isinstance(raw_queries, list):
        record["status"] = "needs_review"
        record["reason_codes"] = ["llm_output_invalid"]
        record["error"] = "模型输出缺少 queries 数组"
        return record

    queries: list[dict[str, Any]] = _normalize_raw_queries(raw_queries, policy, document_language)

    validation = validate_queries(queries, policy=policy,
                                 document_language=document_language, opts=opts)
    # 句式同质化是可修复的（换措辞即可），不必直接落 needs_review：
    # 首次校验若**仅**因句式重复失败，带一句纠正提示重生成一次再判。
    if not validation["ok"] and _only_style_errors(validation["errors"]) \
            and not getattr(opts, "_style_retry_done", False):
        retry_prompt = (
            "\n\n注意：上一次生成的多条 query 开头的句子结构过于雷同。"
            f"请重写全部 {len(queries)} 条 query，确保**任意两条的开头句式都不同**"
            "（不要重复使用'请根据本文档/请基于本文档/请依据本文档'这类开场），"
            "用更口语化、更像真人提需求的说法，题量、题型、语言与原要求一致。"
        )
        try:
            payload2 = llm.generate_queries(document_ref, {
                **policy.query_plan, "max_tokens": int(policy.llm.get("max_tokens", 6000)),
                "style_repair": retry_prompt,
            })
            raw2 = payload2.get("queries") if isinstance(payload2, Mapping) else None
            if isinstance(raw2, list):
                queries2 = _normalize_raw_queries(raw2, policy, document_language)
                validation2 = validate_queries(queries2, policy=policy,
                                               document_language=document_language, opts=opts)
                if validation2["ok"] or len(validation2["errors"]) < len(validation["errors"]):
                    queries, validation = queries2, validation2
                    record["style_repaired"] = True
        except Exception:  # noqa: BLE001 - 重试失败就保留首次结果，不让整条崩
            pass

    record["queries"] = queries
    record["validation"] = validation
    record["status"] = "validated" if validation["ok"] else "needs_review"
    if not validation["ok"]:
        record["reason_codes"] = sorted({e["code"] for e in validation["errors"]})
    return record


def _only_style_errors(errors: Sequence[Mapping[str, Any]]) -> bool:
    """全部错误是否都是句式重复（这类才值得带纠正提示重试）。"""
    return bool(errors) and all(e.get("code") == RC_QUERY_STYLE_REPEATED for e in errors)


def _normalize_raw_queries(raw_queries: Sequence[Any], policy: CollectionPolicy,
                           document_language: str) -> list[dict[str, Any]]:
    """把模型原始 queries 列表归一成内部结构（generate_for_item 与重试路径共用）。"""
    compatibility_keys = {str(t["type_key"]) for t in policy.query_types if t.get("compatibility_only")}
    out: list[dict[str, Any]] = []
    for seq, query in enumerate(list(raw_queries)[: policy.queries_per_document + 5], start=1):
        if not isinstance(query, Mapping):
            continue
        type_key = str(query.get("type_key") or "")
        out.append({
            "seq": int(query.get("seq") or seq),
            "type_key": type_key,
            "type_zh": next((str(t.get("type_zh") or "") for t in policy.query_types
                             if str(t.get("type_key")) == type_key), ""),
            "question_count": query.get("question_count"),
            "language": str(query.get("language") or document_language),
            "text": str(query.get("text") or "").strip(),
            "evidence_locator": query.get("evidence_locator") or {},
            "compatibility_only": bool(query.get("compatibility_only"))
            or type_key in compatibility_keys,
            "expected_behavior": "丢弃或降级处理" if type_key in compatibility_keys else None,
        })
    return out


def run_query_generation(*, policy: CollectionPolicy, store: StateStore, workspace_dir: str,
                         run_id: str, llm: Any, options: QueryOptions | None = None,
                         log: LogFn = _noop) -> dict[str, Any]:
    """批量生成 query，产出 records / evidence / validation report。"""
    opts = options or QueryOptions()
    winpath.makedirs(workspace_dir)
    items = store.list_items(statuses=list(opts.include_statuses) or None)
    if opts.item_ids:
        wanted = {str(x) for x in opts.item_ids}
        items = [i for i in items if str(i.get("item_id")) in wanted]
    if opts.limit:
        items = items[: int(opts.limit)]
    log(f"[query] 待生成 {len(items)} 份文档 x {policy.queries_per_document} 条 query"
        f"（并发 {max(1, opts.concurrency)}）")

    records_path = os.path.join(workspace_dir, "query_records.jsonl")
    evidence_path = os.path.join(workspace_dir, "query_evidence.jsonl")
    counts: Counter = Counter()
    error_codes: Counter = Counter()
    records: list[dict[str, Any]] = []
    started = time.time()
    done = {"n": 0}

    # 主线程先取好 extraction 快照，worker 不碰 store
    extractions = [store.get_extraction(str(item["item_id"])) for item in items]

    def _work(index: int, item: Mapping[str, Any]) -> dict[str, Any]:
        return generate_for_item(item=item, extraction=extractions[index], policy=policy,
                                 llm=llm, workspace_dir=workspace_dir, opts=opts)

    with winpath.open_write(records_path, encoding="utf-8") as rec_fh, \
         winpath.open_write(evidence_path, encoding="utf-8") as ev_fh:
        def _persist(index: int, value: Any) -> None:
            item = items[index]
            if isinstance(value, BaseException):
                record = {"item_id": str(item["item_id"]), "name": item.get("name"),
                          "status": "needs_review", "queries": [], "validation": {},
                          "reason_codes": [_classify_llm_failure(value)],
                          "error": f"{type(value).__name__}: {value}"[:500]}
            else:
                record = value
            item_id = str(record["item_id"])
            extraction = extractions[index]
            record["run_id"] = run_id
            record["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            records.append(record)
            counts[record["status"]] += 1
            for code in record.get("reason_codes") or []:
                error_codes[code] += 1

            for query in record["queries"]:
                store.upsert_query(
                    item_id, int(query["seq"]),
                    type_key=query["type_key"],
                    question_count=int(query.get("question_count") or 0),
                    language=query.get("language"),
                    compatibility_only=1 if query.get("compatibility_only") else 0,
                    payload=query,
                    validation=record.get("validation") or {},
                    status=record["status"],
                    created_at=record["generated_at"],
                )
                ev_fh.write(json.dumps({
                    "item_id": item_id, "seq": query["seq"],
                    "type_key": query["type_key"],
                    "question_count": query.get("question_count"),
                    "language": query.get("language"),
                    "compatibility_only": query.get("compatibility_only"),
                    "evidence_locator": query.get("evidence_locator"),
                    "document_language": record.get("document_language"),
                    "char_count": (extraction or {}).get("char_count"),
                    "page_count": (extraction or {}).get("page_count"),
                    "text_chars": record.get("text_chars"),
                    "prompt_chars": record.get("prompt_chars"),
                    "truncated": record.get("truncated"),
                }, ensure_ascii=False, default=str) + "\n")

            store.add_decision(item_id, "query", record["status"],
                               record.get("reason_codes") or ["ok"], {
                                   "query_count": len(record["queries"]),
                                   "validation_ok": bool((record.get("validation") or {}).get("ok")),
                                   "errors": (record.get("validation") or {}).get("errors", [])[:10],
                               })
            rec_fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            rec_fh.flush()
            ev_fh.flush()
            done["n"] += 1
            if done["n"] % 10 == 0 or done["n"] == len(items):
                store.commit()
                log(f"[query] {done['n']}/{len(items)} 状态 {dict(counts)}")

        map_ordered(_work, items, concurrency=opts.concurrency, on_result=_persist)

    store.commit()
    write_query_validation_report(
        os.path.join(workspace_dir, "query_validation_report.md"),
        records=records, policy=policy, run_id=run_id,
        elapsed_sec=round(time.time() - started, 2),
        llm_stats=store.llm_call_stats(),
    )
    summary = {
        "total_documents": len(items),
        "status_counts": dict(counts),
        "error_codes": dict(error_codes.most_common()),
        "total_queries": sum(len(r["queries"]) for r in records),
        "validated_documents": counts.get("validated", 0),
        "truncated_documents": sum(1 for r in records if r.get("truncated")),
        "artifacts": [records_path, evidence_path,
                      os.path.join(workspace_dir, "query_validation_report.md")],
        "elapsed_sec": round(time.time() - started, 2),
    }
    store.touch_run(run_id, {"collection.generate_queries": summary})
    log(f"[query] 完成：{summary['status_counts']}，共 {summary['total_queries']} 条")
    return summary


def write_query_validation_report(path: str, *, records: Sequence[Mapping[str, Any]],
                                  policy: CollectionPolicy, run_id: str,
                                  elapsed_sec: float,
                                  llm_stats: Mapping[str, Any] | None = None) -> None:
    lines: list[str] = []
    lines.append("# Query 生成校验报告")
    lines.append("")
    lines.append(f"- run_id：`{run_id}`")
    lines.append(f"- 每份文档要求：{policy.queries_per_document} 条 query")
    type_desc = ", ".join(
        f"{t.get('type_zh') or t.get('type_key')}x{t.get('count')}(题量{t.get('question_count')})"
        for t in policy.query_types
    )
    lines.append(f"- 题型配置：{type_desc}")
    lines.append(f"- 文档数：{len(records)}")
    ok = sum(1 for r in records if r.get("status") == "validated")
    lines.append(f"- 校验通过：{ok}")
    lines.append(f"- 需复核：{sum(1 for r in records if r.get('status') == 'needs_review')}")
    truncated = [r for r in records if r.get("truncated")]
    if truncated:
        worst = max(truncated, key=lambda r: int(r.get("text_chars") or 0))
        lines.append(f"- 正文超过输入窗口被截断：{len(truncated)} 份"
                     f"（最长 {worst.get('text_chars')} 字符 → 入模 {worst.get('prompt_chars')}）")
    lines.append(f"- 耗时：{elapsed_sec}s")
    if llm_stats:
        lines.append(f"- 模型调用：{llm_stats.get('calls', 0)} 次，成功 {llm_stats.get('ok_calls', 0)} 次，"
                     f"输入 {llm_stats.get('prompt_chars', 0)} 字符，输出 {llm_stats.get('output_chars', 0)} 字符")
    lines.append("")
    lines.append("## 失败原因分布")
    lines.append("")
    codes: Counter = Counter()
    for record in records:
        for code in record.get("reason_codes") or []:
            codes[code] += 1
    if codes:
        lines.append("| 原因码 | 次数 |")
        lines.append("|-|-|")
        for code, count in codes.most_common():
            lines.append(f"| `{code}` | {count} |")
    else:
        lines.append("- 无。")
    lines.append("")
    lines.append("## 明细（前 30 份）")
    lines.append("")
    lines.append("| 文档 | 来源 | 语言 | query 数 | 状态 | 主要问题 |")
    lines.append("|-|-|-|-|-|-|")
    for record in records[:30]:
        errors = (record.get("validation") or {}).get("errors") or []
        brief = "; ".join(e.get("message", "")[:60] for e in errors[:2]) or (record.get("error") or "-")
        lines.append(
            f"| {str(record.get('name'))[:40]} | {record.get('source_name')} | "
            f"{record.get('document_language')} | {len(record.get('queries') or [])} | "
            f"{record.get('status')} | {brief[:80]} |"
        )
    winpath.makedirs(os.path.dirname(os.path.abspath(path)))
    with winpath.open_write(path, encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
