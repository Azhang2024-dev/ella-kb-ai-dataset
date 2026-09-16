"""TXT / Markdown 抽取器。

难点不是读取，而是编码：多语种语料里 cp1251（俄）、gb18030（中）、
latin-1（法/西）混在一起，直接用 utf-8 会产出大量替换字符并让语言识别全错。
所以这里做多候选解码打分，选"非法字符最少、可打印比例最高"的那个，
并把最终采用的编码写进 stats 作为证据。
"""

from __future__ import annotations

import unicodedata
from typing import Any

from ..winpath import isfile as _isfile, open_read as _open_read
from .base import EXTRACT_EMPTY, EXTRACT_FAILED, Segment, ExtractionResult, finalize

# 顺序即优先级：先严格 UTF-8，再各语种常见编码，最后 latin-1 兜底
_CANDIDATE_ENCODINGS = (
    "utf-8-sig", "utf-8", "utf-16", "gb18030", "big5", "cp1251", "koi8-r",
    "cp1256", "cp1252", "iso-8859-6", "iso-8859-15", "latin-1",
)

_LINE_BLOCK = 400  # 每多少行合成一个 segment，避免 segment 数量爆炸


class TextExtractor:
    name = "text"
    extensions = ("txt", "md", "markdown", "text", "log")

    def can_handle(self, extension: str, media_type: str | None = None) -> bool:
        ext = (extension or "").lower().lstrip(".")
        if ext in self.extensions:
            return True
        return bool(media_type) and media_type.lower().startswith("text/")

    @staticmethod
    def _score(text: str) -> float:
        if not text:
            return -1.0
        bad = 0
        printable = 0
        for ch in text[:20000]:
            cat = unicodedata.category(ch)
            if cat in ("Cc", "Cf", "Co", "Cn") and ch not in "\n\r\t":
                bad += 1
            elif cat[0] in ("L", "N", "P", "Z", "S", "M"):
                printable += 1
        total = bad + printable
        if total == 0:
            return -1.0
        return printable / total - (bad / total) * 2

    def decode_bytes(self, data: bytes) -> tuple[str, str, dict[str, Any]]:
        attempts: dict[str, float] = {}
        best_text, best_enc, best_score = "", "utf-8", -2.0
        for enc in _CANDIDATE_ENCODINGS:
            try:
                text = data.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
            if "\ufffd" in text:
                continue
            score = self._score(text)
            attempts[enc] = round(score, 4)
            if score > best_score:
                best_text, best_enc, best_score = text, enc, score
        if not best_text:
            # 全部失败：用 utf-8 + replace 保证不抛异常，但状态会判成 failed
            best_text = data.decode("utf-8", errors="replace")
            best_enc = "utf-8(replace)"
            attempts[best_enc] = self._score(best_text)
        return best_text, best_enc, {"decode_scores": attempts, "decode_score": round(best_score, 4)}

    def extract(self, path: str, *, max_chars: int | None = None,
                long_doc_threshold: int = 50000) -> ExtractionResult:
        if not _isfile(path):
            return finalize("", (), self.name, status=EXTRACT_FAILED,
                            error=f"文件不存在：{path}")
        try:
            with _open_read(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            return finalize("", (), self.name, status=EXTRACT_FAILED, error=f"读取失败：{exc}")

        text, encoding, decode_stats = self.decode_bytes(data)
        if not text.strip():
            return finalize(text, (), self.name, status=EXTRACT_EMPTY,
                            error="解码后为空", extra_stats={"encoding": encoding, **decode_stats})

        lines = text.splitlines()
        segments: list[Segment] = []
        for start in range(0, len(lines), _LINE_BLOCK):
            chunk = lines[start: start + _LINE_BLOCK]
            segments.append(Segment(
                text="\n".join(chunk),
                kind="line_block",
                locator={"lines": [start + 1, start + len(chunk)], "encoding": encoding},
            ))
        return finalize(
            text, segments, self.name,
            long_doc_threshold=long_doc_threshold,
            extra_stats={"encoding": encoding, "byte_size": len(data), **decode_stats},
        )
