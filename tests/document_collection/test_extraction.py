"""抽取与格式识别测试：每种已支持格式至少一个夹具（计划 Task 4 验证项）。"""

from __future__ import annotations

import os

import pytest

from core.document_collection.extraction import sniff_media_type
from core.document_collection.extractors import get_extractor, supported_extensions
from core.document_collection.extractors.base import EXTRACT_EMPTY
from core.document_collection.extractors.docx import DocxExtractor
from core.document_collection.extractors.pdf import PdfExtractor
from core.document_collection.extractors.pptx import PptxExtractor
from core.document_collection.extractors.text import TextExtractor
from core.document_collection.extractors.xlsx import TabularExtractor


@pytest.fixture(scope="module")
def docs(generated_documents):
    return generated_documents


def _path(docs, token):
    return docs[token]["path"]


# ---------------------------------------------------------------------------
# 格式识别
# ---------------------------------------------------------------------------
def test_sniff_matches_extension_for_real_files(docs):
    for token, expected in (("A_PDF_1", "match"), ("A_FR_DOCX", "match"),
                            ("A_FR_XLSX", "match"), ("A_FR_PPTX", "match")):
        path = _path(docs, token)
        ext = os.path.splitext(path)[1].lstrip(".")
        info = sniff_media_type(path, ext)
        assert info["verdict"] == expected, (token, info)


def test_sniff_detects_ooxml_kind_inside_zip(docs):
    info = sniff_media_type(_path(docs, "A_FR_DOCX"), "docx")
    assert info["media_type"].endswith("wordprocessingml.document")
    assert info["zip_kind"] == "docx"
    info = sniff_media_type(_path(docs, "A_FR_XLSX"), "xlsx")
    assert info["zip_kind"] == "xlsx"
    info = sniff_media_type(_path(docs, "A_FR_PPTX"), "pptx")
    assert info["zip_kind"] == "pptx"


def test_sniff_flags_extension_mismatch(docs, workdir):
    """把 PDF 改名成 .txt，必须判 mismatch，而不是照着扩展名硬抽。"""
    fake = os.path.join(workdir, "renamed.txt")
    with open(_path(docs, "A_PDF_1"), "rb") as src, open(fake, "wb") as dst:
        dst.write(src.read())
    info = sniff_media_type(fake, "txt")
    assert info["verdict"] == "mismatch"
    assert info["media_type"] == "application/pdf"


def test_sniff_identifies_ole2_for_legacy_office(workdir):
    legacy = os.path.join(workdir, "legacy.doc")
    with open(legacy, "wb") as fh:
        fh.write(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
    info = sniff_media_type(legacy, "doc")
    assert info["media_type"] == "application/x-ole2"
    assert info["verdict"] == "match"


def test_sniff_marks_binary_without_extension_as_unknown(workdir):
    blob = os.path.join(workdir, "mystery")
    with open(blob, "wb") as fh:
        fh.write(b"\x00\x01\x02\x03\x04\x05binary")
    info = sniff_media_type(blob, "")
    assert info["verdict"] == "unknown"
    assert info["media_type"] is None


def test_extractor_registry_covers_the_planned_formats():
    exts = set(supported_extensions())
    assert {"pdf", "txt", "md", "docx", "pptx", "xlsx", "csv"} <= exts
    for ext in ("pdf", "txt", "md", "docx", "pptx", "xlsx", "csv"):
        assert get_extractor(ext) is not None, ext
    assert get_extractor("zip") is None
    assert get_extractor("") is None


# ---------------------------------------------------------------------------
# 各格式抽取
# ---------------------------------------------------------------------------
def test_txt_extraction_keeps_line_ranges_and_encoding(docs):
    result = TextExtractor().extract(_path(docs, "A_TXT_1"))
    assert result.status == "extracted"
    assert result.language == "fr"
    assert result.char_count > 500
    assert result.segments and result.segments[0].kind == "line_block"
    assert result.segments[0].locator["lines"][0] == 1
    assert result.stats["encoding"]


def test_txt_extraction_handles_non_utf8_encodings(workdir):
    """俄语 cp1251 文本不能被 utf-8 硬解成乱码。"""
    text = "Глава 1 Введение в линейную алгебру. Матрица это таблица чисел. " * 40
    path = os.path.join(workdir, "cp1251.txt")
    with open(path, "wb") as fh:
        fh.write(text.encode("cp1251"))
    result = TextExtractor().extract(path)
    assert result.status == "extracted"
    assert result.language == "ru", result.stats
    assert "Глава" in result.text


def test_md_extraction(docs):
    result = TextExtractor().extract(_path(docs, "A_MD_1"))
    assert result.status == "extracted"
    assert "# Resume du cours" in result.text


@pytest.mark.parametrize("token,expected_lang", [
    ("A_PDF_1", "fr"), ("B_RU_PDF", "ru"), ("A_ZH_PDF", "zh"),
    ("A_AR_PDF", "ar"), ("A_EN_PDF", "en"), ("A_ES_PDF", "es"),
])
def test_pdf_extraction_keeps_page_locator_and_language(docs, token, expected_lang):
    result = PdfExtractor().extract(_path(docs, token))
    assert result.status == "extracted", result.error
    assert result.language == expected_lang
    assert result.page_count and result.page_count >= 1
    assert result.segments
    pages = [s.locator["page"] for s in result.segments if s.kind == "page"]
    assert pages == sorted(set(pages)), "页码定位必须递增且唯一"
    assert pages[0] == 1


def test_pdf_graphics_only_is_reported_with_image_evidence(docs):
    result = PdfExtractor().extract(_path(docs, "A_IMG_PDF"))
    assert result.status == EXTRACT_EMPTY
    assert result.char_count == 0
    assert result.page_count == 6
    assert result.image_area_ratio and result.image_area_ratio > 0.5
    assert result.stats["scanned_no_text_layer"] is True


def test_pdf_missing_file_is_a_clear_failure(workdir):
    result = PdfExtractor().extract(os.path.join(workdir, "nope.pdf"))
    assert result.status == "failed"
    assert "不存在" in (result.error or "")


def test_pdf_corrupt_file_is_a_clear_failure(workdir):
    path = os.path.join(workdir, "corrupt.pdf")
    with open(path, "wb") as fh:
        fh.write(b"%PDF-1.4\nthis is not a real pdf body\n")
    result = PdfExtractor().extract(path)
    assert result.status in ("failed", "empty")
    assert result.error


def test_docx_extraction_preserves_paragraph_and_table_order(docs):
    result = DocxExtractor().extract(_path(docs, "A_FR_DOCX"))
    assert result.status == "extracted"
    assert result.language == "fr"
    kinds = [s.kind for s in result.segments]
    assert "paragraph" in kinds and "table" in kinds
    # 表格出现在若干段落之后（夹具就是这么排的）
    assert kinds.index("table") > kinds.index("paragraph")
    table_seg = [s for s in result.segments if s.kind == "table"][0]
    assert table_seg.locator["rows"] == 4 and table_seg.locator["cols"] == 3
    para_seg = [s for s in result.segments if s.kind == "paragraph"][0]
    assert para_seg.locator["paragraph_index"] == 1
    assert result.stats["heading_count"] >= 1


def test_pptx_extraction_keeps_slide_numbers(docs):
    result = PptxExtractor().extract(_path(docs, "A_FR_PPTX"))
    assert result.status == "extracted"
    assert result.page_count == result.stats["slide_count"] >= 1
    slides = [s.locator["slide"] for s in result.segments if s.kind == "slide"]
    assert slides == list(range(1, len(slides) + 1))


def test_xlsx_extraction_keeps_sheet_and_range(docs):
    result = TabularExtractor().extract(_path(docs, "A_FR_XLSX"))
    assert result.status == "extracted"
    assert result.language == "fr"
    assert result.stats["sheet_count"] == 2
    sheets = [s.locator["sheet"] for s in result.segments]
    assert "Exercices" in sheets and "Rappel" in sheets
    exercice = [s for s in result.segments if s.locator["sheet"] == "Exercices"][0]
    assert exercice.locator["range"].startswith("A1:")
    assert exercice.locator["cols"] == 3


def test_csv_extraction_sniffs_delimiter_and_encoding(docs):
    result = TabularExtractor().extract(_path(docs, "A_FR_CSV"))
    assert result.status == "extracted"
    assert result.stats["delimiter"] == ","
    assert result.stats["rows_read"] == 25   # 1 表头 + 24 数据行
    assert result.segments[0].locator["rows"] == 25


def test_csv_semicolon_delimiter_is_detected(workdir):
    path = os.path.join(workdir, "semi.csv")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("a;b;c\n")
        for i in range(30):
            fh.write(f"{i};valeur {i};autre {i}\n")
    result = TabularExtractor().extract(path)
    assert result.status == "extracted"
    assert result.stats["delimiter"] == ";"


def test_legacy_xls_is_marked_conversion_required(workdir):
    path = os.path.join(workdir, "old.xls")
    with open(path, "wb") as fh:
        fh.write(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
    result = TabularExtractor().extract(path)
    assert result.status == "conversion_required"
    assert result.error


def test_extraction_is_deterministic(docs):
    """验收检查点 5：同一份文件重复抽取结果一致。"""
    first = PdfExtractor().extract(_path(docs, "A_PDF_1"))
    second = PdfExtractor().extract(_path(docs, "A_PDF_1"))
    assert first.text == second.text
    assert first.char_count == second.char_count
    assert first.language == second.language
    assert first.stats["normalized_fingerprint"] == second.stats["normalized_fingerprint"]
    assert [s.to_dict() for s in first.segments] == [s.to_dict() for s in second.segments]


def test_long_document_flag_uses_policy_threshold(docs):
    result = PdfExtractor().extract(_path(docs, "A_EN_PDF"), long_doc_threshold=1000)
    assert result.is_long_document is True
    result2 = PdfExtractor().extract(_path(docs, "A_EN_PDF"), long_doc_threshold=10_000_000)
    assert result2.is_long_document is False
