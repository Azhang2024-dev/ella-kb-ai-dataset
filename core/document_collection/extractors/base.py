"""抽取器公共类型。

抽取产物必须带定位信息（需求 3.1「可追溯性」：页码/章节/Sheet/表格/语义单元），
因为 Query 本身不写定位，但内部台账要保留，用于覆盖检查与审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

EXTRACT_OK = "extracted"
EXTRACT_FAILED = "failed"
EXTRACT_CONVERSION_REQUIRED = "conversion_required"
EXTRACT_EMPTY = "empty"


@dataclass(frozen=True)
class Segment:
    """一段带定位的正文。"""

    text: str
    kind: str                      # page | paragraph | table | slide | sheet | line_block
    locator: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "kind": self.kind, "locator": dict(self.locator)}


@dataclass(frozen=True)
class ExtractionResult:
    status: str
    extractor: str
    text: str = ""
    segments: tuple[Segment, ...] = ()
    char_count: int = 0
    word_count: int = 0
    cjk_char_count: int = 0
    line_count: int = 0
    page_count: int | None = None
    image_area_ratio: float | None = None
    language: str = "unknown"
    language_confidence: float = 0.0
    is_long_document: bool = False
    error: str | None = None
    stats: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_text: bool = False,
                include_segments: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": self.status,
            "extractor": self.extractor,
            "char_count": self.char_count,
            "word_count": self.word_count,
            "cjk_char_count": self.cjk_char_count,
            "line_count": self.line_count,
            "page_count": self.page_count,
            "image_area_ratio": self.image_area_ratio,
            "language": self.language,
            "language_confidence": self.language_confidence,
            "is_long_document": self.is_long_document,
            "error": self.error,
            "segment_count": len(self.segments),
            "stats": dict(self.stats),
        }
        if include_text:
            out["text"] = self.text
        if include_segments:
            out["segments"] = [s.to_dict() for s in self.segments]
        return out


class Extractor(Protocol):
    """单个格式的抽取器。"""

    name: str
    extensions: tuple[str, ...]

    def can_handle(self, extension: str, media_type: str | None = None) -> bool: ...

    def extract(self, path: str, *, max_chars: int | None = None) -> ExtractionResult: ...


def finalize(text: str, segments: Sequence[Segment], extractor: str, *,
             page_count: int | None = None,
             image_area_ratio: float | None = None,
             long_doc_threshold: int = 50000,
             status: str = EXTRACT_OK,
             error: str | None = None,
             extra_stats: Mapping[str, Any] | None = None) -> ExtractionResult:
    """统一填充统计与语言识别，避免每个抽取器各写一遍。"""
    from ..textstats import cjk_char_count, detect_language, text_fingerprint, word_count

    body = text or ""
    lang, conf = detect_language(body)
    stats: dict[str, Any] = {
        "normalized_fingerprint": text_fingerprint(body) if body else None,
        "extractor": extractor,
    }
    if extra_stats:
        stats.update(dict(extra_stats))
    return ExtractionResult(
        status=status,
        extractor=extractor,
        text=body,
        segments=tuple(segments),
        char_count=len(body),
        word_count=word_count(body, lang),
        cjk_char_count=cjk_char_count(body),
        line_count=body.count("\n") + (1 if body else 0),
        page_count=page_count,
        image_area_ratio=image_area_ratio,
        language=lang,
        language_confidence=conf,
        is_long_document=len(body) > long_doc_threshold,
        error=error,
        stats=stats,
    )
