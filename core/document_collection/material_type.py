"""资料类型标注：给每份交付文档判一个「教材/课堂笔记/试卷/讲义/作业习题」。

为什么需要这个能力：

    交付表（sheet4 同款）里的「类型」列一直要求填
    「教材、课堂笔记、试卷、讲义、作业习题」，但历史上它**从没被填过**——
    组装交付表的一次性脚本直接把那一列写成空串（`tmp/merge_deliveries.py`），
    于是 1360 行里该列全空。

    仓库里其实有个 `infer_material_type()`（`delivery.py`），但它**只看文件名**：
    在这批语料上只标出约 40/136，而且经常标错——`exam_` / `lecture_` / `misc_`
    是采集分桶前缀，不是体裁（法语 `exam_droit_*` 正文其实是课程讲义；
    俄语 `exam_pravo_…СПРАВОЧНИК…` 是一本带 ISBN 的参考书）。

本能力怎么判：

    1. **不重抽、不读全文**：抽取阶段早就把正文落盘在
       `<workspace>/extracted/<item_id>.txt`，这里只读**头部**（默认 1800 字）。
       抽样显示头部 200–1200 字就足以定体裁。
    2. **规则先行**：多语种头部标记（摘要/Abstract/ملخص、PROGRAMA/Syllabus/Учебный план、
       ЭКЗАМЕНАЦИОННЫЙ БИЛЕТ、Exercice/TD/习题、محاضرة/lecture/讲义…）本地判。
    3. **模型兜底**：规则没命中的，才交给模型（宽口径枚举），要求给 evidence 与 confidence。
    4. **折叠到五类**：先把**真实体裁**读准（学术论文/官方手册/课程大纲/实验指导…
       都在语料里真实存在），再由代码**确定性地折叠**成用户要的五类，
       真实体裁原样记进 `evidence`，不丢信息、不臆造。

落库沿用 screening / release 的既有原语（append-only，**不覆盖任何历史决策**）：

    store.add_decision(item_id, "material_type", <五类标签 或 needs_review>, [rc], evidence)

`material_type` 是一个**独立 stage**，与 screen / acquire / deduplicate / query
都不同层，因此不会污染任何既有判定链；`latest_decisions("screen")` 等读取点也看不到它。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .contracts import (
    ITEM_ACCEPTED,
    ITEM_NEEDS_REVIEW,
    RC_MATERIAL_TYPE,
    RC_OK,
)
from .state import StateStore
from . import winpath

LogFn = Callable[[str], None]


def _noop(msg: str) -> None:  # pragma: no cover
    pass


# 用户口径：交付表「类型」列只允许这五类（严格五类）。
FIVE_TYPES: tuple[str, ...] = ("教材", "课堂笔记", "试卷", "讲义", "作业习题")

# 判定用的**宽口径真实体裁**。语料里确实存在五类之外的资料，
# 先如实读准，再由 FOLD_TO_FIVE 收口，避免"硬塞进最近的一类"。
GENRES: tuple[str, ...] = (
    "教材", "课堂笔记", "试卷", "讲义", "作业习题",
    "学术论文", "官方手册", "课程大纲", "实验指导", "合同协议", "其他",
)

# 真实体裁 -> 五类 的确定性折叠规则（用户已确认）。
FOLD_TO_FIVE: dict[str, str] = {
    "教材": "教材",
    "课堂笔记": "课堂笔记",
    "试卷": "试卷",
    "讲义": "讲义",
    "作业习题": "作业习题",
    # 语料里占比很大的两类，按用户口径收口：
    "学术论文": "教材",      # 系统性正文，按教材对待
    "官方手册": "教材",      # 指南/参考书/规范文件
    "合同协议": "教材",      # 法律文书，按教材对待
    "课程大纲": "讲义",      # Programa / Syllabus / Учебный план
    "实验指导": "作业习题",  # 实验手册，按作业习题对待
    # 判不出来：留空 + needs_review，绝不硬塞
    "其他": "",
}

# 头部标记规则：先强的结构标记（试卷/实验/习题/大纲），后弱的（讲义/教材/论文）。
# 每条是**正则**，用 re.I|re.U 编译；只对拉丁字母加 \b 词边界。
#
# 为什么用词边界 + 保守词表：第一版用裸子串匹配，实测出两个真实误判——
#   * `cours` 命中英文 "discourse" → 把语言学期刊论文标成讲义；
#   * `examen final` 命中合成教材的"考核方式"段落 → 把教材标成试卷。
# 宁缺毋滥：命中不了就留给模型兜底或标 needs_review，绝不硬套。
_HEAD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 试卷 / 题库 / 真题（强结构标记）
    ("试卷", (
        r"экзаменационный\s*билет", r"контрольная\s*работа", r"экзаменационн",
        r"examinations?\s+board", r"examination\s+paper", r"\bexam\s+paper\b",
        r"final\s+examination", r"entrance\s+exam", r"revision\s+questions",
        r"candidate'?s\s+index\s+number", r"instructions?:?\s*answer\s+all\s+questions",
        r"\bmcq\b", r"امتحان\s*نهائي", r"أسئلة\s*الامتحان", r"ورقة\s*الامتحان",
        r"试卷", r"试题", r"考题", r"题库", r"一、\s*选择题", r"判断题",
        r"\bquestion\s+(one|two|1|2)\b", r"answer\s+(all|any)\s+(the\s+)?questions?",
    )),
    # 实验指导 / 实验手册（要求"实验室+报告/实践"，不单收"实验室"一词）
    ("实验指导", (
        r"实验指导", r"实验目的", r"实验原理", r"实验装置", r"实验一", r"实验二",
        r"laboratory\s+manual", r"lab\s+manual", r"laboratory\s+experiment",
        r"travaux\s+pratiques", r"лабораторн\w*\s*(?:работ|практик)",
        r"лабораторный\s+практикум",
    )),
    # 作业习题 / 练习册 / TD
    ("作业习题", (
        r"作业", r"习题", r"练习", r"\bexercices?\b", r"\btd\b", r"travaux\s+dirig",
        r"\btarea\b", r"ejercicios", r"\bworksheet\b", r"\bassignment\b",
        r"\bhomework\b", r"problem\s+set", r"задач[аи]", r"упражнен",
        r"تمارين", r"أوراق\s*العمل",
    )),
    # 课程大纲 / Programa / Syllabus
    ("课程大纲", (
        r"课程大纲", r"教学大纲", r"培养方案", r"\bsyllabus\b",
        r"programa\s+(docente|de\s+estudio)", r"gu[ií]a\s+docente",
        r"plan\s+de\s+estudios", r"учебный\s+план", r"рабочая\s+программа",
        r"منهج", r"الخطة\s*الدراسية",
    )),
    # 课堂笔记 / 学生笔记（笔记语义必须有"笔记"词本身，且要排除两处典型误伤：
    #   * 参考文献里的 "Lecture Notes in Computer Science"（Springer 丛书名）；
    #   * 试卷说明里的 "refer to your lecture notes"（那是让考生看笔记，不是笔记正文）。
    # 这两处若被吞掉，就会把期刊论文/试卷错标成课堂笔记——实测均出现过。）
    ("课堂笔记", (
        r"(?<!your\s)\blecture\s+notes?\b(?!\s+in\b)",
        r"\bclass\s+notes?\b", r"\bcourse\s+notes?\b",
        r"\bstudy\s+notes?\b", r"\bstudent\s+notes?\b",
        r"notes?\s+de\s+cours", r"prises?\s+de\s+notes",
        r"конспект\s+лекц", r"конспект",
        r"apuntes\s+de\s+clase",
        r"ملاحظات", r"课堂笔记", r"课堂记录", r"随堂笔记", r"笔记", r"筆記",
    )),
    # 教材 / 教科书（含"课程材料"式的西语 front matter：合成文档正是这种）
    ("教材", (
        r"教材", r"课本", r"教科书", r"\btextbook\b", r"учебник",
        r"учебное\s+пособие", r"lehrbuch", r"校本教材",
        r"material\s+de\s+estudio", r"créditos\s+ects", r"titulaci[óo]n:",
        r"asignatura:", r"curso\s+acad[ée]mico", r"profesor\s+responsable",
        r"m[ée]todo\s+de\s+evaluaci[óo]n", r"horas\s+te[óo]ricas",
        r"conocimientos\s+previos", r"\bmanual\s+de\b",
        r"chapitre\s+\d+", r"глава\s+\d+", r"第[一二三四五六七八九十\d]+章",
    )),
    # 讲义 / 课件
    ("讲义", (
        r"讲义", r"课件", r"幻灯片", r"\blecture\b", r"\bhandout\b", r"\bcours\b",
        r"le[çc]on", r"محاضرة", r"المحاضرة", r"conferencia", r"лекция", r"лекции",
        r"конспект\s+лекц", r"apuntes",
        r"\blessons?\s*\d", r"\bstudy\s+session\b", r"\bunit\s+\d",
        r"\([A-Z]{2,4}[-\s]?\d{3}\)",
    )),
    # 官方手册 / 指南 / 参考书 / 规范
    ("官方手册", (
        r"guide\s+pratique", r"practical\s+guide", r"\bguidelines\b",
        r"reference\s+manual", r"user\s+manual", r"handbook", r"справочник",
        r"методическ", r"рекомендац", r"who/", r"organisation\s+mondiale",
        r"دليل\s*(?:المستخدم|المعلم|الطالب|عملي)", r"reglamento", r"章程",
    )),
    # 合同 / 协议 / 法律文书（语料里真实存在：船员派遣协议等）
    ("合同协议", (
        r"\bagreement\b", r"\bcontract\b", r"سند\s*تفويض", r"\bmemorandum\b",
    )),
    # 学术论文（摘要/关键词是强信号，但很多教材也有"引言"，所以放最后）
    ("学术论文", (
        r"摘要", r"关键词", r"\babstract\b", r"\bkeywords\b", r"resumen",
        r"palabras\s+clave", r"résumé", r"р[её]зюме", r"аннотация",
        r"ملخص", r"الكلمات\s*المفتاحية", r"بحوث", r"مجلة", r"m[ée]moire",
        r"mots-clés",
    )),
)

# 预编译：一次编译，多次匹配（136 篇也要快）。
_HEAD_RULES_RE: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (genre, tuple(re.compile(p, re.I | re.U) for p in pats))
    for genre, pats in _HEAD_RULES
)

# 笔记类标记只在"标题区"认。真正的笔记文档在标题处就自述体裁
# （实测两篇分别在 121、279 字处）；而正文尾部常出现假命中——
# 参考文献里的 `Lecture Notes, Eberhard Karls Universität…`（引用条目）、
# 西语教材书目里的 `Apuntes de la asignatura`（参考读物清单），
# 这些都不该把期刊论文/教材判成课堂笔记，故单独限制匹配窗口。
_NOTE_GENRE = "课堂笔记"
_NOTE_HEAD_CHARS = 2500


@dataclass
class MaterialTypeOptions:
    """标注参数。item_ids 为空时全池标注（由调用方决定 scope）。"""

    item_ids: Sequence[str] = ()
    dry_run: bool = False
    # 只看头部多少字符：抽样显示 1200 字足够；给 1800 留余量。
    head_chars: int = 1800
    # 是否允许模型兜底（规则未命中时）。CLI 的 --llm 才打开。
    use_llm: bool = False


def _first_match(text: str) -> tuple[str, str]:
    """按规则优先级在正文里找第一个命中，返回 (体裁, 命中的模式)。没有返回 ("", "")。

    笔记类规则只在前 `_NOTE_HEAD_CHARS` 字内匹配：正文尾部的引用条目
    （`Lecture Notes, Universität…`）和参考读物清单（`Apuntes de la asignatura`）
    是典型假命中，会污染期刊论文/教材的判定。
    """
    if not text:
        return "", ""
    for genre, pats in _HEAD_RULES_RE:
        region = text[:_NOTE_HEAD_CHARS] if genre == _NOTE_GENRE else text
        for pat in pats:
            m = pat.search(region)
            if m:
                return genre, m.group(0)
    return "", ""


def infer_from_head(text: str, name: str | None = None) -> tuple[str, str]:
    """从正文头部（必要时叠加文件名）判真实体裁。

    返回 (真实体裁, 命中的模式)。判不出返回 ("", "")。
    文件名参与匹配，但**权重低于正文**：先用正文判，正文判不出才用文件名兜底，
    避免 `exam_droit_*.pdf` 这种"文件名骗人"的情况盖过正文。

    文件名里的 `_` 先换成空格：采集分桶前缀是 `lecture_xxx.pdf` 这种下划线连接，
    而 `_` 属于 \\w，`\\blecture\\b` 在 "lecture_xxx" 上根本匹配不到——实测阿语
    `lecture_*` 全被判成"未知"就是这个原因。
    """
    genre, hit = _first_match(str(text or "")[:12000])
    if genre:
        return genre, hit
    normalized = str(name or "").replace("_", " ")
    return _first_match(normalized)


def fold_to_five(genre: str) -> str:
    """把宽口径真实体裁折叠成用户要的五类；折叠不出的返回空串（不臆造）。"""
    return FOLD_TO_FIVE.get(str(genre or ""), "")


def _load_head(item_id: str, workspace_dir: str,
               extraction: Mapping[str, Any] | None, head_chars: int) -> str:
    """只读抽取文本的头部（不读全文，省 IO）。优先 text_path，回落到约定路径。"""
    candidates: list[str] = []
    recorded = (extraction or {}).get("text_path")
    if recorded:
        candidates.append(str(recorded))
    candidates.append(os.path.join(workspace_dir, "extracted", f"{item_id}.txt"))
    for path in candidates:
        if path and winpath.isfile(path):
            try:
                with winpath.open_read(path, encoding="utf-8-sig") as fh:
                    return fh.read(int(head_chars) or 1800)
            except OSError:
                continue
    return ""


def run_material_type(*, policy: Any, store: StateStore, workspace_dir: str,
                      run_id: str, options: MaterialTypeOptions | None = None,
                      llm: Any | None = None, log: LogFn = _noop) -> dict[str, Any]:
    """逐条标注资料类型并落库（append-only）。返回 RunResult 用的摘要。"""
    opts = options or MaterialTypeOptions()
    decisions = store.latest_decisions("screen")
    items = {str(i["item_id"]): i for i in store.list_items()}
    wanted = {str(x) for x in opts.item_ids} if opts.item_ids else None

    # 只有 screen=accepted 的文档才有资格进交付表；其余跳过（不动它们的判定）。
    targets: list[str] = []
    for item_id, dec in decisions.items():
        if str(dec.get("status")) != ITEM_ACCEPTED:
            continue
        if wanted is not None and item_id not in wanted:
            continue
        targets.append(item_id)
    targets.sort()

    labels: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    by_label: dict[str, int] = {t: 0 for t in FIVE_TYPES}
    by_genre: dict[str, int] = {}

    llm_available = bool(opts.use_llm and llm is not None
                         and getattr(llm, "available", False))

    for item_id in targets:
        item = items.get(item_id) or {}
        name = str(item.get("name") or "")
        extraction = store.get_extraction(item_id)
        head = _load_head(item_id, workspace_dir, extraction, opts.head_chars)

        genre, keyword = infer_from_head(head, name)
        method = "rule" if genre else ""
        confidence = 0.9 if genre else 0.0
        llm_evidence: list[str] = []

        # 规则没命中才叫模型；模型不可用就如实落 needs_review，绝不硬塞。
        if not genre and llm_available:
            document_ref = {
                "item_id": item_id, "name": name,
                "extension": item.get("extension"),
                "language": str((extraction or {}).get("language") or ""),
                "text": head,
            }
            try:
                verdict = llm.classify_material_type(document_ref, _material_type_policy(policy))
                genre = str(verdict.get("genre") or "").strip()
                if genre not in GENRES:
                    genre = ""
                method = "llm" if genre else ""
                confidence = float(verdict.get("confidence") or 0.0)
                llm_evidence = [str(x) for x in (verdict.get("evidence") or [])][:6]
            except Exception as exc:  # noqa: BLE001
                log(f"[material_type] {item_id} 模型判定失败：{type(exc).__name__}")
                genre, method = "", ""

        label = fold_to_five(genre)
        if genre:
            by_genre[genre] = by_genre.get(genre, 0) + 1

        if not label:
            unresolved.append({"item_id": item_id, "name": name,
                               "genre": genre or "unknown", "method": method})
            # 落 needs_review，保留判定轨迹但不给五类标签（不臆造）
            if not opts.dry_run:
                store.add_decision(item_id, "material_type", ITEM_NEEDS_REVIEW,
                                   [RC_MATERIAL_TYPE, "material_type_unresolved"],
                                   {"material_type": "", "material_genre": genre or "unknown",
                                    "method": method, "head_chars": len(head),
                                    "run_id": run_id})
            continue

        by_label[label] = by_label.get(label, 0) + 1
        labels.append({"item_id": item_id, "name": name, "material_type": label,
                       "material_genre": genre, "method": method,
                       "keyword": keyword, "confidence": confidence,
                       "evidence": llm_evidence})
        if not opts.dry_run:
            store.add_decision(item_id, "material_type", ITEM_ACCEPTED,
                               [RC_MATERIAL_TYPE, RC_OK],
                               {"material_type": label, "material_genre": genre,
                                "material_type_method": method, "keyword": keyword,
                                "confidence": confidence, "llm_evidence": llm_evidence,
                                "head_chars": len(head),
                                "labeled_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "run_id": run_id})

    if not opts.dry_run:
        store.commit()

    summary = {
        "total_requested": len(targets),
        "labeled_total": len(labels),
        "unresolved_total": len(unresolved),
        "dry_run": bool(opts.dry_run),
        "by_type": by_label,
        "by_genre": by_genre,
        "method_counts": {m: sum(1 for x in labels if x["method"] == m)
                          for m in ("rule", "llm")},
        "unresolved": unresolved[:50],
        "labels": labels,
    }
    log(f"[material_type] 候选 {summary['total_requested']} 篇："
        f"标注 {summary['labeled_total']} / 未定 {summary['unresolved_total']}"
        + ("（dry-run，未落库）" if opts.dry_run else ""))
    return summary


def _material_type_policy(policy: Any) -> dict[str, Any]:
    """喂给模型的最小策略视图（只给体裁口径，不泄露无关内容）。"""
    return {"genres": list(GENRES), "five_types": list(FIVE_TYPES)}