"""Task 10：交付能力——入选选取 + 真实文件名落地 + 交付表产出。

背景（2026-09-15 交付口径重构）
--------------------------------
历史批次（p0-langs-20260913）暴露三个交付问题，本模块一次收口：

1. **选取逻辑曾是外部一次性脚本**（`output/_skill_debug/.../build_selection.py`）：
   用**目录名**当语种、用**文件名子串**当学科、自带一套弱噪声词表，与 Core 的口径脱节。
   本模块把选取固化进 Core，并强制二次校验：
   - 语种以**正文判定**（extraction.language）为准，不再看目录名；
   - 学科以**筛选阶段写进 evidence 的 subject**（模型判定优先）为准，且必须 ∈ P0∪P1；
   - 文档类型命中 `exclusions.document_kinds` 的一律排除。

2. **产物目录全是哈希文件名**（内容寻址的 artifacts/<sha256[:2]>/<sha256>.<ext>）：
   人无法一眼看出哪份是哪份。`run_deliver` 额外产出一个 `delivery_files/` 目录，
   用**真实文件名**（硬链接指向内容寻址仓，不复制、不占额外空间；跨盘回退拷贝）。

3. **交付表格式以在线《Ella 知识库 AI 生题数据集》sheet4 为准**：
   列 = 语种 / 文件名 / 格式 / 字符数 / item_id / 类型 / 学科 / 离线文件 / query / query_type，
   **一行一条 query**（一份文档 10 行）。

三种产物都只读状态库与本地文件，不重新计算筛选/出题结论。
"""

from __future__ import annotations

import csv
import os
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .config import CollectionPolicy
from .state import StateStore
from . import winpath

LogFn = Callable[[str], None]


def _noop(msg: str) -> None:  # pragma: no cover
    pass


# ---------------------------------------------------------------------------
# 选项
# ---------------------------------------------------------------------------
@dataclass
class SelectOptions:
    """入选选取参数。"""

    # 每语种入选份数；None = 不设上限（全收）。
    per_language: int | None = 30
    # 处理哪些语种；空 = 取策略 target_languages（P0 + P1_extra）。
    languages: tuple[str, ...] = ()
    # 学科是否必须落在策略 P0∪P1 内（二次校验，与 screen 门禁互为双保险）。
    require_subject_in_scope: bool = True
    # 是否排除命中 exclusions.document_kinds 的类型；None = 读策略。
    exclude_kinds: tuple[str, ...] | None = None
    # 定向白名单：只处理这些 item_id（空 = 全池）。
    item_ids: tuple[str, ...] = ()


@dataclass
class DeliverOptions(SelectOptions):
    """交付参数：在选取之上，控制落盘与表格。"""

    # 真实文件名落地目录（相对 workspace）；产物根的 delivery_files/。
    files_dirname: str = "delivery_files"
    # 交付表文件名。
    xlsx_name: str = "交付表_Query.xlsx"


# ---------------------------------------------------------------------------
# 文档类型推断（sheet4 的"类型"列：教材/课堂笔记/试卷/讲义/作业习题）
# ---------------------------------------------------------------------------
_MATERIAL_TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("试卷", ("试卷", "试题", "考题", "exam", "examen", "экзамен", "امتحان")),
    ("作业习题", ("习题", "练习", "作业", "题库", "exercice", "tarea", "задач", "تمرين")),
    ("实验指导", ("实验", "lab", "laboratoire", "experiment", "المختبر")),
    ("课堂笔记", ("笔记", "课堂", "notes", "apuntes", "конспект", "ملاحظات")),
    ("讲义", ("讲义", "课件", "lecture", "slide", "cours", "conferencia", "лекция", "محاضرة")),
    ("课程大纲", ("大纲", "syllabus", "programa", "guia", "программа", "منهج")),
    ("教材", ("教材", "课本", "教科书", "textbook", "manual", "учебник", "كتاب")),
)


def infer_material_type(name: str | None) -> str:
    """从文件名推断资料类型；推断不出返回空串（不臆造）。"""
    low = str(name or "").lower()
    if not low:
        return ""
    for label, kws in _MATERIAL_TYPE_RULES:
        if any(k.lower() in low for k in kws):
            return label
    return ""


# ---------------------------------------------------------------------------
# 选取
# ---------------------------------------------------------------------------
def _lang_of(extraction: Mapping[str, Any] | None) -> str:
    """语种以**正文判定**为准（extraction.language），不是目录名。"""
    return str((extraction or {}).get("language") or "unknown").lower()


def _subject_of(decision: Mapping[str, Any] | None) -> str:
    """学科取筛选阶段写进 evidence 的 subject（模型判定优先，缺省回落关键词兜底）。"""
    ev = (decision or {}).get("evidence") or {}
    return str(ev.get("subject") or ev.get("subject_hint") or "unknown").lower()


def _excluded_kinds_of(decision: Mapping[str, Any] | None) -> str | None:
    ev = (decision or {}).get("evidence") or {}
    kind = ev.get("exclude_kind")
    if kind and str(kind).lower() not in ("null", "none", ""):
        return str(kind).lower()
    return None


def select_documents(*, policy: CollectionPolicy, store: StateStore,
                     options: SelectOptions | None = None,
                     log: LogFn = _noop) -> dict[str, Any]:
    """从 accepted 池选出交付目标集。返回 {selected:[...], status_counts, by_language}。"""
    opts = options or SelectOptions()
    languages = [str(x).lower() for x in (opts.languages or policy.target_languages)]
    allowed_subjects = {s.lower() for s in policy.target_subjects_for_hint}
    excluded = set(opts.exclude_kinds if opts.exclude_kinds is not None
                   else (policy.section("exclusions").get("document_kinds") or []))
    decisions = store.latest_decisions("screen")
    items = {str(i["item_id"]): i for i in store.list_items()}
    wanted = {str(x) for x in opts.item_ids} if opts.item_ids else None

    rows: list[dict[str, Any]] = []
    dropped = Counter()
    for item_id, dec in decisions.items():
        if str(dec.get("status")) != "accepted":
            continue
        if wanted is not None and item_id not in wanted:
            continue
        item = items.get(item_id)
        if not item:
            dropped["no_item"] += 1
            continue
        extraction = store.get_extraction(item_id)
        lang = _lang_of(extraction)
        if lang not in languages:
            dropped[f"lang_not_target:{lang}"] += 1
            continue
        subject = _subject_of(dec)
        if opts.require_subject_in_scope and subject not in allowed_subjects:
            dropped[f"subject_out_of_scope:{subject}"] += 1
            continue
        kind = _excluded_kinds_of(dec)
        if kind and kind in excluded:
            dropped[f"excluded_kind:{kind}"] += 1
            continue
        rows.append({
            "item_id": item_id,
            "name": item.get("name"),
            "language": lang,
            "subject": subject,
            "subject_zh": _subject_zh(subject),
            "material_type": infer_material_type(item.get("name")),
            "extension": (item.get("extension") or "").lower(),
            "char_count": int((extraction or {}).get("char_count") or 0),
            "source_name": item.get("source_name"),
            "source_path": f"{item.get('source_name')}/"
                           f"{(item.get('metadata') or {}).get('relative_path') or item.get('name')}",
            "education_level": str((dec.get("evidence") or {}).get("education_level") or ""),
            "has_query": bool(store.queries_for(item_id)),
        })

    # 按语种分组，教育信号与字符数排序后取前 N（P0 学科按比例：P0 优先、P1 保底）
    by_lang: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_lang.setdefault(r["language"], []).append(r)

    p0_subjects = {s.lower() for s in (policy.subjects.get("p0") or [])}
    selected: list[dict[str, Any]] = []
    for lang in languages:
        pool = by_lang.get(lang, [])
        # 排序：P0 学科优先 > 有 query（已验证的更值得交付） > 字符数多
        pool.sort(key=lambda r: (
            0 if r["subject"] in p0_subjects else 1,
            0 if r["has_query"] else 1,
            -r["char_count"],
        ))
        # P1 保底：先确保 P1 四类在里面（若池中有），避免被 P0 挤掉
        p1_subjects = {s.lower() for s in (policy.subjects.get("p1") or [])}
        chosen: list[dict[str, Any]] = []
        seen_subj: set[str] = set()
        for r in pool:
            if r["subject"] in p1_subjects and r["subject"] not in seen_subj:
                chosen.append(r); seen_subj.add(r["subject"])
        for r in pool:
            if r in chosen:
                continue
            chosen.append(r)
            if opts.per_language and len(chosen) >= opts.per_language:
                break
        if opts.per_language:
            chosen = chosen[:opts.per_language]
        for i, r in enumerate(chosen, 1):
            r2 = dict(r); r2["lang_seq"] = i
            selected.append(r2)

    selected.sort(key=lambda r: (languages.index(r["language"]), r["lang_seq"]))
    summary = {
        "selected_total": len(selected),
        "by_language": dict(Counter(r["language"] for r in selected)),
        "by_subject": dict(Counter(r["subject"] for r in selected)),
        "dropped": dict(dropped),
        "available_by_language": {k: len(v) for k, v in by_lang.items()},
    }
    log(f"[select] 入选 {summary['selected_total']} 份：{summary['by_language']}"
        f"；剔除 {summary['dropped']}")
    return {"selected": selected, **summary}


def _subject_zh(subject: str) -> str:
    from .llm import SUBJECT_LABELS_ZH
    return SUBJECT_LABELS_ZH.get(subject, subject)


# ---------------------------------------------------------------------------
# 交付：真实文件名落地 + 交付表
# ---------------------------------------------------------------------------
_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(name: str, maxlen: int = 120) -> str:
    """把真实文件名收敛成文件系统安全名（保留可读性）。"""
    clean = _ILLEGAL_FS.sub("_", str(name or "")).strip(" .")
    if not clean:
        clean = "unnamed"
    if len(clean) > maxlen:
        stem, dot, ext = clean.rpartition(".")
        clean = (stem[: maxlen - len(ext) - 1] + dot + ext) if dot else clean[:maxlen]
    return clean


def link_real_files(*, store: StateStore, workspace_dir: str, selected: Sequence[Mapping[str, Any]],
                    files_dirname: str = "delivery_files", log: LogFn = _noop) -> dict[str, Any]:
    """把入选文档按**真实文件名**落到 delivery_files/<语种>/。

    优先硬链接（os.link）指向内容寻址仓：不复制、不额外占盘、仍指向同一份字节；
    跨盘或文件系统不支持时回退 shutil.copy2。重名自动加 __<sha8> 后缀。
    """
    root = os.path.join(workspace_dir, files_dirname)
    linked = copied = missing = 0
    rows: list[dict[str, Any]] = []
    used: dict[tuple[str, str], str] = {}
    for r in selected:
        item_id = str(r["item_id"])
        artifact = store.get_artifact(item_id) or {}
        src = artifact.get("path")
        lang = r["language"]
        if not src or not winpath.isfile(src):
            missing += 1
            rows.append({**r, "offline_file": "", "link_mode": "missing"})
            continue
        target_dir = os.path.join(root, lang)
        winpath.makedirs(target_dir)
        fname = _safe_filename(str(r.get("name") or item_id))
        key = (lang, fname.lower())
        if key in used:  # 同名：加 sha 前 8 位区分
            sha = str(artifact.get("sha256") or "")[:8]
            stem, dot, ext = fname.rpartition(".")
            fname = f"{stem}__{sha}{dot}{ext}" if dot else f"{fname}__{sha}"
        used[key] = fname
        dst = os.path.join(target_dir, fname)
        try:
            if winpath.isfile(dst):
                os.remove(dst)
            os.link(src, dst)          # 硬链接（同盘、NTFS）
            mode = "hardlink"
            linked += 1
        except OSError:
            shutil.copy2(src, dst)     # 跨盘/不支持时回退拷贝
            mode = "copy"
            copied += 1
        rel = os.path.join(files_dirname, lang, fname).replace("\\", "/")
        rows.append({**r, "offline_file": rel, "link_mode": mode})
    log(f"[deliver] 真实文件名落地：硬链接 {linked}，拷贝 {copied}，缺失 {missing} -> {root}")
    return {"rows": rows, "hardlinked": linked, "copied": copied, "missing": missing, "root": root}


def write_delivery_xlsx(*, store: StateStore, workspace_dir: str,
                        rows: Sequence[Mapping[str, Any]], xlsx_name: str,
                        log: LogFn = _noop) -> str:
    """按在线 sheet4 的列产出交付表：一行一条 query。

    列：语种 / 文件名 / 格式 / 字符数 / item_id / 类型 / 学科 / 离线文件 / query / query_type
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    path = os.path.join(workspace_dir, xlsx_name)
    wb = Workbook()
    ws = wb.active
    ws.title = "交付表"
    headers = ["语种", "文件名", "格式", "字符数", "item_id", "类型", "学科",
               "离线文件", "query", "query_type"]
    ws.append(headers)
    hdr_fill = PatternFill("solid", fgColor="D9E1F2")
    for c in ws[1]:
        c.fill = hdr_fill
        c.font = Font(bold=True)

    lang_zh = {"en": "英语", "zh": "中文", "fr": "法语", "ru": "俄语",
               "ar": "阿拉伯语", "es": "西班牙语", "pt": "葡萄牙语"}
    n_q = 0
    for r in rows:
        queries = store.queries_for(str(r["item_id"]))
        # 只出 validated 的 query（与交付口径一致）
        queries = [q for q in queries if str(q.get("status")) == "validated"]
        queries.sort(key=lambda q: int(q.get("seq") or 0))
        base = [
            lang_zh.get(str(r["language"]), str(r["language"])),
            r.get("name"),
            r.get("extension"),
            r.get("char_count"),
            r.get("item_id"),
            r.get("material_type"),
            r.get("subject_zh"),
            r.get("offline_file"),
        ]
        if not queries:
            ws.append(base + ["", ""])
            continue
        for q in queries:
            payload = q.get("payload") or {}
            text = payload.get("text") or q.get("text") or ""
            type_key = payload.get("type_key") or q.get("type_key") or ""
            ws.append(base + [text, type_key])
            n_q += 1

    widths = [8, 42, 7, 10, 30, 10, 16, 34, 60, 16]
    from openpyxl.utils import get_column_letter
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    for row in ws.iter_rows(min_row=2):
        row[8].alignment = Alignment(wrap_text=True, vertical="top")
    wb.save(path)
    log(f"[deliver] 交付表写出：{path}（{len(rows)} 份文档 / {n_q} 条 query）")
    return path


def write_selected_csv(*, workspace_dir: str, rows: Sequence[Mapping[str, Any]],
                       name: str = "selected.csv") -> str:
    path = os.path.join(workspace_dir, name)
    cols = ["lang_seq", "language", "subject", "subject_zh", "material_type", "extension",
            "char_count", "name", "item_id", "source_name", "source_path", "offline_file"]
    with winpath.open_write(path, encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def run_deliver(*, policy: CollectionPolicy, store: StateStore, workspace_dir: str,
                run_id: str, options: DeliverOptions | None = None,
                log: LogFn = _noop) -> dict[str, Any]:
    """完整交付：选取 -> 真实名落地 -> 交付表。返回 RunResult 用的摘要。"""
    opts = options or DeliverOptions()
    winpath.makedirs(workspace_dir)
    sel = select_documents(policy=policy, store=store, options=opts, log=log)
    selected = sel["selected"]
    linked = link_real_files(store=store, workspace_dir=workspace_dir, selected=selected,
                             files_dirname=opts.files_dirname, log=log)
    rows = linked["rows"]
    csv_path = write_selected_csv(workspace_dir=workspace_dir, rows=rows)
    xlsx_path = write_delivery_xlsx(store=store, workspace_dir=workspace_dir, rows=rows,
                                    xlsx_name=opts.xlsx_name, log=log)
    total_q = sum(len([q for q in store.queries_for(str(r["item_id"]))
                       if str(q.get("status")) == "validated"]) for r in rows)
    return {
        "selected_total": len(rows),
        "by_language": sel["by_language"],
        "by_subject": sel["by_subject"],
        "dropped": sel["dropped"],
        "queries_total": total_q,
        "hardlinked": linked["hardlinked"],
        "copied": linked["copied"],
        "missing_files": linked["missing"],
        "files_dir": linked["root"],
        "artifacts": [csv_path, xlsx_path],
    }