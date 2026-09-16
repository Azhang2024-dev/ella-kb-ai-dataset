"""抽取器注册表。

新增格式只需要在这里登记一个实现类，上层 dispatch 不用改。
"""

from __future__ import annotations

from typing import Any, Sequence

from .base import (
    EXTRACT_EMPTY,
    EXTRACT_FAILED,
    EXTRACT_OK,
    ExtractionResult,
    Segment,
    finalize,
)
from .docx import DocxExtractor
from .pdf import PdfExtractor
from .pptx import PptxExtractor
from .text import TextExtractor
from .xlsx import TabularExtractor

DEFAULT_EXTRACTORS: tuple[Any, ...] = (
    PdfExtractor(),
    DocxExtractor(),
    PptxExtractor(),
    TabularExtractor(),
    TextExtractor(),   # 放最后：它的 can_handle 对 text/* 兜底
)

__all__ = [
    "DEFAULT_EXTRACTORS",
    "DocxExtractor",
    "EXTRACT_EMPTY",
    "EXTRACT_FAILED",
    "EXTRACT_OK",
    "ExtractionResult",
    "PdfExtractor",
    "PptxExtractor",
    "Segment",
    "TabularExtractor",
    "TextExtractor",
    "finalize",
    "get_extractor",
    "supported_extensions",
]


def get_extractor(extension: str | None, media_type: str | None = None,
                  extractors: Sequence[Any] | None = None) -> Any | None:
    """按扩展名 + MIME 选抽取器；找不到返回 None（调用方决定 reject 还是 review）。"""
    ext = (extension or "").lower().lstrip(".")
    for candidate in (extractors or DEFAULT_EXTRACTORS):
        try:
            if candidate.can_handle(ext, media_type):
                return candidate
        except Exception:  # noqa: BLE001 - 单个抽取器异常不应影响其他格式
            continue
    return None


def supported_extensions(extractors: Sequence[Any] | None = None) -> tuple[str, ...]:
    out: list[str] = []
    for candidate in (extractors or DEFAULT_EXTRACTORS):
        for ext in getattr(candidate, "extensions", ()):
            if ext not in out:
                out.append(ext)
    return tuple(sorted(out))
