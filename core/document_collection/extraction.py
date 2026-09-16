"""Task 4：格式识别与内容抽取（extract）。

流程：签名嗅探真实 MIME -> 与扩展名交叉校验 -> 选抽取器 -> 落盘正文与 segments -> 写库。

交叉校验的意义：云盘上的文件名可能骗人（.pdf 实际是 zip / .txt 实际是二进制）。
计划 Task 4 要求"结合文件签名和扩展名识别 MIME"，这里给出明确的三种结论：
    match      签名与扩展名一致
    mismatch   签名与扩展名不一致 -> needs_review（不猜）
    unknown    签名无法识别 -> 按扩展名走，但打上标记
"""

from __future__ import annotations

import json
import os
import time
import zipfile
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .config import CollectionPolicy
from .contracts import (
    ITEM_CONVERSION_REQUIRED,
    ITEM_EXTRACTED,
    ITEM_EXTRACTION_FAILED,
    ITEM_NEEDS_REVIEW,
    RC_EXTRACT_EMPTY,
    RC_EXTRACT_FAILED,
    RC_EXTRACT_OK,
    RC_MIME_UNSUPPORTED,
)
from .extractors import get_extractor
from .parallel import map_ordered
from .state import StateStore
from . import winpath

LogFn = Callable[[str], None]


def _noop(msg: str) -> None:  # pragma: no cover
    pass


_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole2"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
    (b"Rar!\x1a\x07", "application/x-rar"),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF8", "image/gif"),
    (b"BM", "image/bmp"),
    (b"MZ", "application/x-dosexec"),
)

_EXT_TO_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "doc": "application/x-ole2",
    "xls": "application/x-ole2",
    "ppt": "application/x-ole2",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "txt": "text/plain",
    "md": "text/markdown",
}

_ZIP_MEMBER_MAP = (
    ("word/", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    ("xl/", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    ("ppt/", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"),
)


def sniff_media_type(path: str, extension: str | None = None) -> dict[str, Any]:
    """返回 {media_type, verdict, signature, zip_kind}。"""
    ext = (extension or os.path.splitext(path)[1].lower().lstrip(".")) or ""
    result: dict[str, Any] = {
        "media_type": None, "verdict": "unknown", "signature": None,
        "zip_kind": None, "declared_extension": ext,
    }
    try:
        with winpath.open_read(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        result["verdict"] = "unreadable"
        return result

    detected: str | None = None
    for sig, mime in _SIGNATURES:
        if head.startswith(sig):
            detected = mime
            result["signature"] = sig.decode("latin-1", errors="replace")
            break

    if detected == "application/zip":
        # OOXML 全是 zip，靠内部目录区分 docx/xlsx/pptx
        try:
            with zipfile.ZipFile(winpath.long_path(path)) as zf:
                names = zf.namelist()[:200]
        except (zipfile.BadZipFile, OSError):
            names = []
        for prefix, mime, kind in _ZIP_MEMBER_MAP:
            if any(n.startswith(prefix) for n in names):
                detected = mime
                result["zip_kind"] = kind
                break
        else:
            result["zip_kind"] = "zip_other"

    result["media_type"] = detected or ("text/plain" if _looks_textual(path) else None)
    declared = _EXT_TO_MIME.get(ext.lower())
    if detected is None and result["media_type"] == "text/plain":
        # 纯文本没有魔数，扩展名说它是文本就算一致
        result["verdict"] = "match" if ext.lower() in ("txt", "md", "markdown", "csv", "tsv", "log", "text") else "text_no_signature"
    elif detected is None:
        result["verdict"] = "unknown"
    elif declared and detected == declared:
        result["verdict"] = "match"
    elif detected == "application/x-ole2" and ext.lower() in ("doc", "xls", "ppt"):
        result["verdict"] = "match"
    elif result.get("zip_kind") and result["zip_kind"] == ext.lower():
        result["verdict"] = "match"
    else:
        result["verdict"] = "mismatch"
    return result


def _looks_textual(path: str, sample_bytes: int = 4096) -> bool:
    try:
        with winpath.open_read(path, "rb") as fh:
            chunk = fh.read(sample_bytes)
    except OSError:
        return False
    if not chunk:
        return True
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
        return True
    except UnicodeDecodeError:
        pass
    for enc in ("gb18030", "cp1251", "cp1252", "latin-1"):
        try:
            chunk.decode(enc)
            return True
        except UnicodeDecodeError:
            continue
    return False


@dataclass
class ExtractionOptions:
    limit: int | None = None
    include_statuses: tuple[str, ...] = ("acquired", "extraction_failed")
    extensions: tuple[str, ...] = ()
    item_ids: tuple[str, ...] = ()
    concurrency: int = 1
    write_text: bool = True
    write_segments: bool = True
    max_text_bytes_on_disk: int = 20 * 1024 * 1024
    strict_mime: bool = True


def _safe_name(value: str) -> str:
    """派生文件名。上限 64 字符：工作区路径本身可能很深，
    叠加 Windows MAX_PATH(260) 后必须留出余量。"""
    return "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in value)[:64]


def extract_one(*, item: Mapping[str, Any], artifact: Mapping[str, Any] | None,
                policy: CollectionPolicy, workspace_dir: str,
                opts: ExtractionOptions, log: LogFn = _noop) -> dict[str, Any]:
    """抽取单个已采集资源。"""
    item_id = str(item.get("item_id") or item.get("item_token"))
    path = (artifact or {}).get("path")
    ext = ((artifact or {}).get("extension") or item.get("extension") or "").lower()
    if not ext and path:
        ext = os.path.splitext(path)[1].lower().lstrip(".")

    out: dict[str, Any] = {
        "item_id": item_id, "item_token": item.get("item_token"),
        "name": item.get("name"), "extension": ext,
        "artifact_path": path, "status": ITEM_EXTRACTED,
        "reason_codes": [], "extractor": None, "media_type": None,
        "mime_verdict": None, "char_count": 0, "word_count": 0,
        "cjk_char_count": 0, "page_count": None, "text_ratio": None,
        "image_area_ratio": None, "language": "unknown",
        "language_confidence": 0.0, "is_long_document": False,
        "segment_count": 0, "text_path": None, "segments_path": None,
        "error": None,
    }

    if not path or not winpath.isfile(path):
        out["status"] = ITEM_EXTRACTION_FAILED
        out["reason_codes"] = [RC_EXTRACT_FAILED, "artifact_missing"]
        out["error"] = f"本地产物不存在：{path}"
        return out

    sniff = sniff_media_type(path, ext)
    out["media_type"] = sniff["media_type"]
    out["mime_verdict"] = sniff["verdict"]

    if ext in policy.conversion_required_extensions or sniff["media_type"] == "application/x-ole2":
        out["status"] = ITEM_CONVERSION_REQUIRED
        out["reason_codes"] = ["conversion_required", f"mime={sniff['media_type']}"]
        out["error"] = "旧版 Office 二进制格式需要批准转换器"
        return out

    if opts.strict_mime and sniff["verdict"] == "mismatch":
        out["status"] = ITEM_NEEDS_REVIEW
        out["reason_codes"] = ["mime_extension_mismatch"]
        out["error"] = f"签名 {sniff['media_type']} 与扩展名 .{ext} 不一致"
        return out

    if sniff["media_type"] and sniff["media_type"].startswith("image/"):
        out["status"] = ITEM_NEEDS_REVIEW
        out["reason_codes"] = [RC_MIME_UNSUPPORTED, "image_file"]
        out["error"] = "图片文件不进入文本抽取"
        return out

    extractor = get_extractor(ext, sniff["media_type"])
    if extractor is None:
        out["status"] = ITEM_NEEDS_REVIEW
        out["reason_codes"] = [RC_MIME_UNSUPPORTED, "no_extractor"]
        out["error"] = f"没有可用抽取器（ext={ext}, mime={sniff['media_type']}）"
        return out

    out["extractor"] = extractor.name
    try:
        result = extractor.extract(path, long_doc_threshold=policy.full_text_review_threshold_chars)
    except Exception as exc:  # noqa: BLE001 - 抽取失败必须显式记录
        out["status"] = ITEM_EXTRACTION_FAILED
        out["reason_codes"] = [RC_EXTRACT_FAILED]
        out["error"] = f"{type(exc).__name__}: {exc}"[:600]
        return out

    if result.status == "conversion_required":
        out["status"] = ITEM_CONVERSION_REQUIRED
        out["reason_codes"] = ["conversion_required"]
        out["error"] = result.error
        return out
    if result.status in ("failed",):
        out["status"] = ITEM_EXTRACTION_FAILED
        out["reason_codes"] = [RC_EXTRACT_FAILED]
        out["error"] = result.error
        return out
    if result.status == "empty" or not result.text.strip():
        out["status"] = ITEM_NEEDS_REVIEW
        out["reason_codes"] = [RC_EXTRACT_EMPTY]
        out["error"] = result.error or "抽取结果为空"

    page_area = result.page_count or 0
    out.update({
        "char_count": result.char_count,
        "word_count": result.word_count,
        "cjk_char_count": result.cjk_char_count,
        "page_count": result.page_count,
        "image_area_ratio": result.image_area_ratio,
        "language": result.language,
        "language_confidence": result.language_confidence,
        "is_long_document": result.is_long_document,
        "segment_count": len(result.segments),
        "text_ratio": round(result.char_count / max(page_area, 1), 2) if page_area else None,
        "normalized_fingerprint": (result.stats or {}).get("normalized_fingerprint"),
        "extractor_stats": {k: v for k, v in (result.stats or {}).items()
                            if k != "normalized_fingerprint"},
    })

    # 成功也要留正向理由：验收检查点 4 要求「每个决定都有 reason_codes」，
    # 空列表会让 explain / 报告看不出这一条到底凭什么通过。
    reasons = [RC_EXTRACT_OK, f"extractor={extractor.name}"]
    if sniff["verdict"] not in ("match",):
        reasons.append(f"mime_verdict={sniff['verdict']}")
    if result.is_long_document:
        reasons.append("long_document")
    out["reason_codes"] = reasons

    if opts.write_text and result.text:
        text_dir = os.path.join(workspace_dir, "extracted")
        winpath.makedirs(text_dir)
        text_path = os.path.join(text_dir, f"{_safe_name(item_id)}.txt")
        body = result.text
        if len(body.encode("utf-8")) > opts.max_text_bytes_on_disk:
            body = body[: opts.max_text_bytes_on_disk // 4]
            out["reason_codes"].append("text_truncated_on_disk")
        with winpath.open_write(text_path, encoding="utf-8") as fh:
            fh.write(body)
        out["text_path"] = text_path

    if opts.write_segments and result.segments:
        seg_dir = os.path.join(workspace_dir, "segments")
        winpath.makedirs(seg_dir)
        seg_path = os.path.join(seg_dir, f"{_safe_name(item_id)}.jsonl")
        with winpath.open_write(seg_path, encoding="utf-8") as fh:
            for seg in result.segments:
                fh.write(json.dumps(seg.to_dict(), ensure_ascii=False) + "\n")
        out["segments_path"] = seg_path

    return out


def run_extract(*, policy: CollectionPolicy, store: StateStore, workspace_dir: str,
                run_id: str, options: ExtractionOptions | None = None,
                log: LogFn = _noop) -> dict[str, Any]:
    """批量抽取，写 extraction_report.jsonl 并落库。

    并发策略：抽取本身是纯计算+IO（不碰数据库），所以放进线程池并行；
    落库与写报告回到主线程按原顺序执行，保证断点续跑与输出顺序不受并发影响。
    """
    opts = options or ExtractionOptions()
    winpath.makedirs(workspace_dir)
    items = store.list_items(statuses=list(opts.include_statuses) or None)
    if opts.extensions:
        wanted = {e.lower().lstrip(".") for e in opts.extensions}
        items = [i for i in items if (i.get("extension") or "").lower() in wanted]
    if opts.item_ids:
        wanted_ids = {str(x) for x in opts.item_ids}
        items = [i for i in items if str(i.get("item_id")) in wanted_ids]
    if opts.limit:
        items = items[: int(opts.limit)]
    log(f"[extract] 待抽取 {len(items)} 条（并发 {max(1, opts.concurrency)}）")

    # 主线程先把 artifact 读出来（store 非线程安全），worker 只拿只读快照
    artifacts = [store.get_artifact(str(item["item_id"])) for item in items]

    def _work(index: int, item: Mapping[str, Any]) -> dict[str, Any]:
        return extract_one(item=item, artifact=artifacts[index], policy=policy,
                           workspace_dir=workspace_dir, opts=opts, log=log)

    report_path = os.path.join(workspace_dir, "extraction_report.jsonl")
    counts: dict[str, int] = {}
    lang_counts: dict[str, int] = {}
    started = time.time()
    done = {"n": 0}

    with winpath.open_write(report_path, "a", encoding="utf-8") as fh:
        def _persist(index: int, value: Any) -> None:
            item = items[index]
            if isinstance(value, BaseException):
                rec = {"item_id": str(item["item_id"]), "item_token": item.get("item_token"),
                       "name": item.get("name"), "status": ITEM_EXTRACTION_FAILED,
                       "reason_codes": [RC_EXTRACT_FAILED],
                       "error": f"{type(value).__name__}: {value}"[:600]}
            else:
                rec = value
            rec["run_id"] = run_id
            rec["extracted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            counts[rec["status"]] = counts.get(rec["status"], 0) + 1
            if rec.get("language"):
                lang_counts[rec["language"]] = lang_counts.get(rec["language"], 0) + 1

            store.upsert_extraction(
                rec["item_id"],
                extractor=rec.get("extractor"), status=rec["status"],
                char_count=rec.get("char_count"), word_count=rec.get("word_count"),
                cjk_char_count=rec.get("cjk_char_count"), page_count=rec.get("page_count"),
                text_ratio=rec.get("text_ratio"), language=rec.get("language"),
                language_confidence=rec.get("language_confidence"),
                is_long_document=1 if rec.get("is_long_document") else 0,
                segments_path=rec.get("segments_path"), text_path=rec.get("text_path"),
                error=rec.get("error"), extracted_at=rec["extracted_at"],
            )
            artifact = store.get_artifact(rec["item_id"])
            if artifact:
                store.upsert_artifact(rec["item_id"], media_type=rec.get("media_type"))
            store.add_decision(rec["item_id"], "extract", rec["status"], rec["reason_codes"], {
                "char_count": rec.get("char_count"),
                "language": rec.get("language"),
                "language_confidence": rec.get("language_confidence"),
                "page_count": rec.get("page_count"),
                "image_area_ratio": rec.get("image_area_ratio"),
                "mime_verdict": rec.get("mime_verdict"),
                "extractor": rec.get("extractor"),
                "normalized_fingerprint": rec.get("normalized_fingerprint"),
                "segment_count": rec.get("segment_count"),
            })
            # 抽取失败/待复核不改写 item 的采集状态，但 stage 前进，便于续跑
            store.set_item_stage(rec["item_id"], rec["status"], "extract")
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            done["n"] += 1
            if done["n"] % 20 == 0 or done["n"] == len(items):
                store.commit()
                log(f"[extract] {done['n']}/{len(items)} 状态分布 {counts}")

        map_ordered(_work, items, concurrency=opts.concurrency, on_result=_persist)

    store.commit()
    summary = {
        "total_selected": len(items),
        "status_counts": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "language_counts": dict(sorted(lang_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "total_chars": 0,
        "long_documents": 0,
        "elapsed_sec": round(time.time() - started, 2),
        "report_path": report_path,
    }
    for row in store.list_extractions():
        summary["total_chars"] += int(row.get("char_count") or 0)
        summary["long_documents"] += int(row.get("is_long_document") or 0)
    store.touch_run(run_id, {"collection.extract": summary})
    log(f"[extract] 完成：{summary['status_counts']}")
    return summary
