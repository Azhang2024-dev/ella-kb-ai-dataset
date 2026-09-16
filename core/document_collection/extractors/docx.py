"""DOCX 抽取器（python-docx）。

按 body 的真实子节点顺序遍历，保证"段落 -> 表格 -> 段落"的原始顺序不丢，
并给每段/每表打上 locator（paragraph_index / table_index / rows / cols），
满足需求 3.1 的可追溯性。
"""

from __future__ import annotations

from typing import Any, Iterator

from ..winpath import isfile as _isfile, long_path as _long_path
from .base import EXTRACT_EMPTY, EXTRACT_FAILED, Segment, ExtractionResult, finalize


def _iter_block_items(parent: Any) -> Iterator[Any]:
    """按文档顺序产出 Paragraph 与 Table（python-docx 官方推荐写法）。"""
    from docx.document import Document as _Document
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table, _Cell
    from docx.text.paragraph import Paragraph

    if isinstance(parent, _Document):
        parent_elm = parent.element.body
    elif isinstance(parent, _Cell):
        parent_elm = parent._tc
    else:
        raise ValueError(f"不支持的 parent 类型：{type(parent)}")

    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


class DocxExtractor:
    name = "docx"
    extensions = ("docx",)

    def can_handle(self, extension: str, media_type: str | None = None) -> bool:
        ext = (extension or "").lower().lstrip(".")
        if ext in self.extensions:
            return True
        return "wordprocessingml" in (media_type or "").lower()

    def extract(self, path: str, *, max_chars: int | None = None,
                long_doc_threshold: int = 50000) -> ExtractionResult:
        if not _isfile(path):
            return finalize("", (), self.name, status=EXTRACT_FAILED,
                            error=f"文件不存在：{path}")
        try:
            from docx import Document
        except ImportError as exc:  # pragma: no cover
            return finalize("", (), self.name, status=EXTRACT_FAILED,
                            error=f"python-docx 不可用：{exc}")

        try:
            doc = Document(_long_path(path))
        except Exception as exc:  # noqa: BLE001
            return finalize("", (), self.name, status=EXTRACT_FAILED,
                            error=f"打开 DOCX 失败：{type(exc).__name__}: {exc}")

        parts: list[str] = []
        segments: list[Segment] = []
        paragraph_index = 0
        table_index = 0
        heading_count = 0
        try:
            for block in _iter_block_items(doc):
                cls_name = type(block).__name__
                if cls_name == "Paragraph":
                    paragraph_index += 1
                    text = (block.text or "").strip()
                    style = ""
                    try:
                        style = str(block.style.name or "")
                    except Exception:  # noqa: BLE001
                        style = ""
                    if style.lower().startswith("heading") or style.startswith("标题"):
                        heading_count += 1
                    if not text:
                        continue
                    parts.append(text)
                    segments.append(Segment(
                        text=text, kind="paragraph",
                        locator={"paragraph_index": paragraph_index, "style": style},
                    ))
                elif cls_name == "Table":
                    table_index += 1
                    rows: list[list[str]] = []
                    for row in block.rows:
                        rows.append([ (cell.text or "").strip() for cell in row.cells ])
                    flat = "\n".join(" | ".join(r) for r in rows)
                    if flat.strip():
                        parts.append(flat)
                        segments.append(Segment(
                            text=flat, kind="table",
                            locator={"table_index": table_index,
                                     "rows": len(rows),
                                     "cols": max((len(r) for r in rows), default=0)},
                        ))
        except Exception as exc:  # noqa: BLE001
            return finalize("\n".join(parts), tuple(segments), self.name,
                            status=EXTRACT_FAILED,
                            error=f"遍历 DOCX 正文失败：{type(exc).__name__}: {exc}",
                            extra_stats={"paragraph_count": paragraph_index,
                                         "table_count": table_index})

        image_count = 0
        try:
            image_count = len(doc.inline_shapes)
        except Exception:  # noqa: BLE001
            image_count = 0

        core: dict[str, Any] = {}
        try:
            cp = doc.core_properties
            core = {k: (str(getattr(cp, k) or "")[:200])
                    for k in ("title", "author", "subject", "keywords", "category", "comments")}
        except Exception:  # noqa: BLE001
            core = {}

        stats = {
            "paragraph_count": paragraph_index,
            "table_count": table_index,
            "heading_count": heading_count,
            "inline_image_count": image_count,
            "core_properties": core,
        }
        text = "\n".join(parts)
        if not text.strip():
            return finalize("", tuple(segments), self.name, status=EXTRACT_EMPTY,
                            error="DOCX 正文为空", extra_stats=stats)
        return finalize(text, segments, self.name, long_doc_threshold=long_doc_threshold,
                        extra_stats=stats)
