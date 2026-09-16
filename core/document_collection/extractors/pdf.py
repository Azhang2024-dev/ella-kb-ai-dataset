"""PDF 抽取器（PyMuPDF）。

同时产出需求 2.2/2.3 要的三类证据：
- 页码定位（segment.locator.page）；
- 字符数 / 页数 / 每页字符数；
- 图片面积占比 image_area_ratio，用于"图形主导"判定。

扫描版 PDF（有页面但没有文字层）不直接判失败，而是标记 scanned_no_text_layer，
交给 screening 走 needs_review —— 因为需求说"无法稳定抽取正文的扫描文件"才排除，
判定权在规则层，不在抽取器。
"""

from __future__ import annotations

from typing import Any

from ..winpath import isfile as _isfile, long_path as _long_path
from .base import EXTRACT_EMPTY, EXTRACT_FAILED, Segment, ExtractionResult, finalize


class PdfExtractor:
    name = "pdf"
    extensions = ("pdf",)

    def can_handle(self, extension: str, media_type: str | None = None) -> bool:
        ext = (extension or "").lower().lstrip(".")
        if ext in self.extensions:
            return True
        return (media_type or "").lower() == "application/pdf"

    def extract(self, path: str, *, max_chars: int | None = None,
                long_doc_threshold: int = 50000) -> ExtractionResult:
        if not _isfile(path):
            return finalize("", (), self.name, status=EXTRACT_FAILED,
                            error=f"文件不存在：{path}")
        try:
            import fitz  # PyMuPDF
        except ImportError as exc:  # pragma: no cover
            return finalize("", (), self.name, status=EXTRACT_FAILED,
                            error=f"PyMuPDF 不可用：{exc}")

        stats: dict[str, Any] = {}
        try:
            doc = fitz.open(_long_path(path))
        except Exception as exc:  # noqa: BLE001 - 损坏/加密 PDF 很常见
            return finalize("", (), self.name, status=EXTRACT_FAILED,
                            error=f"打开 PDF 失败：{type(exc).__name__}: {exc}")

        try:
            if doc.needs_pass:
                # 尝试空密码（很多 PDF 只是设了权限密码）
                if not doc.authenticate(""):
                    return finalize("", (), self.name, status=EXTRACT_FAILED,
                                    error="PDF 需要密码，无法抽取")
            page_count = doc.page_count
            stats["encrypted"] = bool(doc.is_encrypted)
            stats["pdf_metadata"] = {
                k: str(v)[:200] for k, v in (doc.metadata or {}).items()
                if k in ("title", "author", "subject", "creator", "producer", "keywords")
            }

            pages_text: list[str] = []
            segments: list[Segment] = []
            image_area_total = 0.0
            page_area_total = 0.0
            pages_with_images = 0

            for index in range(page_count):
                try:
                    page = doc.load_page(index)
                except Exception as exc:  # noqa: BLE001 - 单页损坏不应拖垮整份
                    pages_text.append("")
                    segments.append(Segment(text="", kind="page",
                                            locator={"page": index + 1,
                                                     "error": f"{type(exc).__name__}: {exc}"[:200]}))
                    continue
                text = page.get_text("text") or ""
                pages_text.append(text)
                if text.strip():
                    segments.append(Segment(
                        text=text, kind="page",
                        locator={"page": index + 1, "char_count": len(text)},
                    ))
                try:
                    rect = page.rect
                    page_area = float(rect.width * rect.height)
                except Exception:  # noqa: BLE001
                    page_area = 0.0
                page_area_total += page_area
                img_area = 0.0
                try:
                    for info in page.get_image_info(xrefs=True) or []:
                        bbox = info.get("bbox")
                        if not bbox or len(bbox) != 4:
                            continue
                        w = abs(float(bbox[2]) - float(bbox[0]))
                        h = abs(float(bbox[3]) - float(bbox[1]))
                        img_area += min(w * h, page_area)
                except Exception:  # noqa: BLE001
                    img_area = 0.0
                if img_area > 0:
                    pages_with_images += 1
                image_area_total += min(img_area, page_area)

            full_text = "\n".join(pages_text)
            image_area_ratio = (
                round(image_area_total / page_area_total, 4) if page_area_total > 0 else None
            )
            stats.update({
                "pages_with_text": sum(1 for t in pages_text if t.strip()),
                "pages_with_images": pages_with_images,
                "image_area_ratio": image_area_ratio,
                "scanned_no_text_layer": page_count > 0 and not full_text.strip(),
            })
            if not full_text.strip():
                return finalize("", tuple(segments), self.name, page_count=page_count,
                                image_area_ratio=image_area_ratio, status=EXTRACT_EMPTY,
                                error="PDF 无文字层（疑似扫描件）", extra_stats=stats)
            return finalize(full_text, segments, self.name, page_count=page_count,
                            image_area_ratio=image_area_ratio,
                            long_doc_threshold=long_doc_threshold, extra_stats=stats)
        finally:
            try:
                doc.close()
            except Exception:  # noqa: BLE001
                pass
