"""pytest 公共夹具。

设计目标（对应计划 10.1/10.2）：
1. 全流程可以在**完全不联网**的情况下跑通：FakeCloudAdapter 提供合成云盘树；
2. 同时保留真实录制响应的回放路径（fixtures/lark_drive_pages）；
3. 真实格式的文档夹具在会话开始时用已安装库现场生成，不往仓库里塞二进制；
4. FakeLLM 是确定性实现，保证「同一 fixture 重跑结果一致」这条验收。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Mapping, Sequence

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.document_collection.adapters.base import (  # noqa: E402
    CommandResult,
    ListPage,
    PermanentAdapterError,
    TransientAdapterError,
)
from core.document_collection import winpath  # noqa: E402
from core.document_collection.config import CollectionPolicy  # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
PAGES_DIR = os.path.join(FIXTURE_DIR, "lark_drive_pages")
DOCS_DIR = os.path.join(FIXTURE_DIR, "documents")

# 本机 %TEMP%\pytest-of-admin 存在权限问题（PermissionError WinError 5），
# 因此不使用 pytest 内置的 tmp_path，改用仓库内 output/_pytest_tmp（已被 .gitignore 忽略）。
PYTEST_TMP_ROOT = os.path.join(_REPO_ROOT, "output", "_pytest_tmp")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """把每个阶段的报告挂到 item 上，供 workdir 夹具判断是否保留现场。"""
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


@pytest.fixture
def workdir(request):
    """每个用例一个独立目录；用例失败时保留现场，成功时清理。"""
    import shutil
    import time as _time

    os.makedirs(PYTEST_TMP_ROOT, exist_ok=True)
    safe_name = "".join(ch if ch.isalnum() or ch in "-_." else "_"
                        for ch in request.node.name)[:70]
    path = os.path.join(PYTEST_TMP_ROOT, f"{int(_time.time() * 1000)}_{os.getpid()}_{safe_name}")
    os.makedirs(path, exist_ok=True)
    yield path
    failed = bool(getattr(request.node, "rep_call", None)
                  and request.node.rep_call.failed)
    if not failed:
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def real_policy() -> CollectionPolicy:
    from core.document_collection.config import load_policy
    return load_policy(os.path.join(_REPO_ROOT, "config", "collection_policy.json"))


@pytest.fixture
def policy(real_policy: CollectionPolicy, workdir) -> CollectionPolicy:
    """真实策略的副本，但把源换成本地合成目录，保证测试不碰云盘。"""
    raw = json.loads(json.dumps(real_policy.raw, ensure_ascii=False))
    raw["sources"] = [
        {"name": "知识库_2026", "kind": "fake_cloud", "token": "FOLDER_A",
         "role": "primary", "readonly": True},
        {"name": "docQA.llm", "kind": "fake_cloud", "token": "FOLDER_B",
         "role": "supplement", "readonly": True},
    ]
    raw["llm"]["enabled"] = False
    return CollectionPolicy(path=os.path.join(workdir, "collection_policy.json"), raw=raw)


# ---------------------------------------------------------------------------
# 合成云盘
# ---------------------------------------------------------------------------
def build_fake_tree() -> dict[str, Any]:
    """两个源、多层嵌套、含分页/在线资源/快捷方式/超限文件的合成树。"""
    def file_entry(token: str, name: str, ftype: str = "file", parent: str = "") -> dict[str, Any]:
        return {"token": token, "name": name, "type": ftype, "parent_token": parent,
                "created_time": "1700000000", "modified_time": "1700000001",
                "url": f"https://example.feishu.cn/file/{token}"}

    tree: dict[str, list[dict[str, Any]]] = {
        # 源 A：根目录有 1 个子文件夹 + 若干文件
        "FOLDER_A": [
            {"token": "A_SUB1", "name": "DOC-FR", "type": "folder", "parent_token": "FOLDER_A",
             "url": "https://example.feishu.cn/drive/folder/A_SUB1"},
            file_entry("A_PDF_1", "cours_chimie.pdf", parent="FOLDER_A"),
            file_entry("A_TXT_1", "notes_cours.txt", parent="FOLDER_A"),
            file_entry("A_MD_1", "resume.md", parent="FOLDER_A"),
            file_entry("A_BIG", "huge_textbook.pdf", parent="FOLDER_A"),
            file_entry("A_OLD", "legacy.doc", parent="FOLDER_A"),
            file_entry("A_IMG", "photo.jpg", parent="FOLDER_A"),
            file_entry("A_UNKNOWN", "mystery", parent="FOLDER_A"),
            file_entry("A_ZH_PDF", "neike_huli.pdf", parent="FOLDER_A"),
            file_entry("A_AR_PDF", "qanun_madani.pdf", parent="FOLDER_A"),
            file_entry("A_EN_PDF", "microeconomics.pdf", parent="FOLDER_A"),
            file_entry("A_ES_PDF", "administracion.pdf", parent="FOLDER_A"),
            file_entry("A_IMG_PDF", "graphics_only.pdf", parent="FOLDER_A"),
        ],
        "A_SUB1": [
            file_entry("A_FR_DOCX", "td_physique.docx", parent="A_SUB1"),
            file_entry("A_FR_XLSX", "exercices.xlsx", parent="A_SUB1"),
            file_entry("A_FR_CSV", "donnees.csv", parent="A_SUB1"),
            file_entry("A_FR_PPTX", "cours_slides.pptx", parent="A_SUB1"),
            file_entry("A_ONLINE_DOC", "polycope_en_ligne", ftype="docx", parent="A_SUB1"),
        ],
        # 源 B：根目录 + 一个与源 A 完全相同 token 的重复资源（跨源重复）
        "FOLDER_B": [
            {"token": "B_SUB1", "name": "russian", "type": "folder", "parent_token": "FOLDER_B",
             "url": "https://example.feishu.cn/drive/folder/B_SUB1"},
            file_entry("A_PDF_1", "cours_chimie_copy.pdf", parent="FOLDER_B"),  # 同 token
            {"token": "B_SHORTCUT", "name": "外部链接", "type": "shortcut", "parent_token": "FOLDER_B"},
        ],
        "B_SUB1": [
            file_entry("B_RU_PDF", "lekciya_algebra.pdf", parent="B_SUB1"),
            file_entry("B_RU_TXT", "konspekt.txt", parent="B_SUB1"),
            file_entry("B_ONLINE_SHEET", "tablica", ftype="sheet", parent="B_SUB1"),
            file_entry("B_ONLINE_SLIDES", "prezentaciya", ftype="slides", parent="B_SUB1"),
        ],
    }
    return tree


class FakeCloudAdapter:
    """按合成树响应 list_folder；download/export 从本地夹具目录取真实文件。"""

    name = "fake_cloud"

    def __init__(self, tree: Mapping[str, Sequence[Mapping[str, Any]]],
                 file_provider, *, page_size_cap: int = 3,
                 fail_tokens: Mapping[str, str] | None = None,
                 content_map: Mapping[str, bytes] | None = None):
        self.tree = {k: [dict(e) for e in v] for k, v in tree.items()}
        self.file_provider = file_provider
        self.page_size_cap = page_size_cap
        self.fail_tokens = dict(fail_tokens or {})
        self.content_map = dict(content_map or {})
        self.calls: list[str] = []
        self.fail_once: set[str] = set()

    def list_folder(self, folder_token: str, *, page_token: str | None = None,
                    page_size: int = 200) -> ListPage:
        self.calls.append(f"list:{folder_token}:{page_token}")
        entries = self.tree.get(folder_token, [])
        size = min(int(page_size), self.page_size_cap)
        start = int(page_token) if page_token else 0
        chunk = entries[start: start + size]
        end = start + len(chunk)
        has_more = end < len(entries)
        return ListPage(entries=tuple(chunk), has_more=has_more,
                        next_page_token=str(end) if has_more else None,
                        folder_token=folder_token)

    def inspect(self, token_or_url: str) -> dict[str, Any]:
        self.calls.append(f"inspect:{token_or_url}")
        for parent, entries in self.tree.items():
            for entry in entries:
                if entry["token"] == token_or_url:
                    return {"title": entry["name"], "token": entry["token"],
                            "type": entry["type"], "url": entry.get("url")}
        return {"title": token_or_url, "token": token_or_url, "type": "folder"}

    def download_file(self, item_token: str, dest_rel_path: str, *, workdir: str,
                      overwrite: bool = True, timeout: int | None = None,
                      capability: str = "") -> dict[str, Any]:
        self.calls.append(f"download:{item_token}")
        mode = self.fail_tokens.get(item_token)
        if mode == "transient" and item_token not in self.fail_once:
            self.fail_once.add(item_token)
            raise TransientAdapterError("429 rate limit")
        if mode == "permanent":
            raise PermanentAdapterError("permission_denied")
        if mode == "missing":
            return {"saved_path": os.path.join(workdir, "does_not_exist.bin"), "size_bytes": 0}
        content = self.content_map.get(item_token)
        if content is None:
            content = self.file_provider(item_token)
        dest = os.path.join(workdir, dest_rel_path.replace("/", os.sep))
        winpath.makedirs(os.path.dirname(dest))
        with winpath.open_write(dest, "wb") as fh:
            fh.write(content)
        return {"saved_path": dest, "size_bytes": len(content),
                "rel_path": dest_rel_path}

    def export_online(self, item_token: str, item_type: str, dest_rel_dir: str, *,
                      workdir: str, extension: str | None = None,
                      overwrite: bool = True, timeout: int | None = None,
                      capability: str = "") -> dict[str, Any]:
        self.calls.append(f"export:{item_token}:{item_type}:{extension}")
        mode = self.fail_tokens.get(item_token)
        if mode == "permanent":
            raise PermanentAdapterError("export_not_supported")
        content = self.content_map.get(item_token)
        if content is None:
            content = self.file_provider(item_token)
        out_dir = os.path.join(workdir, dest_rel_dir.replace("/", os.sep))
        winpath.makedirs(out_dir)
        fname = f"{item_token}.{extension or 'bin'}"
        path = os.path.join(out_dir, fname)
        with winpath.open_write(path, "wb") as fh:
            fh.write(content)
        return {"saved_path": path, "size_bytes": len(content), "rel_dir": dest_rel_dir,
                "raw": {"content_type": "application/octet-stream", "saved_path": path,
                        "size_bytes": len(content)}}


@pytest.fixture
def fake_tree() -> dict[str, Any]:
    return build_fake_tree()


# ---------------------------------------------------------------------------
# 真实格式文档夹具（会话级，现场生成）
# ---------------------------------------------------------------------------
EN_TEXT = (
    "Chapter 1 Introduction to Microeconomics. "
    "The demand curve shows the relationship between price and quantity demanded. "
    "Elasticity measures how much quantity responds to a change in price. "
    "In this course students will study consumer theory, producer theory and market structures. "
    "Exercise 1: compute the price elasticity of demand for the following data set. "
    "Exercise 2: explain why a monopoly produces less output than a competitive market. "
) * 12

FR_TEXT = (
    "Chapitre 1 Introduction a la comptabilite generale. "
    "Le bilan comptable decrit la situation patrimoniale de l entreprise a une date donnee. "
    "Dans ce cours, les etudiants vont etudier le journal, le grand livre et la balance. "
    "Exercice 1 : enregistrer les operations courantes au journal. "
    "Exercice 2 : expliquer la difference entre une charge et une immobilisation. "
) * 12

RU_TEXT = (
    "Глава 1 Введение в линейную алгебру. "
    "Матрица представляет собой прямоугольную таблицу чисел, расположенных в строках и столбцах. "
    "В этом курсе студенты изучат операции сложения и умножения матриц, определители и обратные матрицы. "
    "Упражнение 1: вычислите определитель матрицы третьего порядка. "
    "Упражнение 2: найдите собственное значение и собственный вектор. "
) * 12

ZH_TEXT = (
    "第一章 内科护理学总论。护理评估是护理程序的第一步，包括收集病人的主观资料和客观资料。"
    "在本课程中，学生将学习生命体征测量、护理病历书写以及常见疾病的护理措施。"
    "练习题一：请说明发热病人的护理要点。练习题二：请列出心力衰竭病人的主要护理诊断。"
) * 12

AR_TEXT = (
    "الفصل الأول مقدمة في القانون المدني. يعرف العقد بأنه اتفاق بين طرفين يرتب آثارا قانونية ملزمة. "
    "في هذه المادة الدراسية سيتعرف الطلاب على أركان العقد وشروط صحته والآثار المترتبة على الإخلال بالالتزامات. "
    "تمرين الأول: اذكر أركان العقد. تمرين الثاني: اشرح الفرق بين البطلان والقابلية للإبطال. "
) * 8

ES_TEXT = (
    "Capitulo 1 Introduccion a la administracion de empresas. "
    "La estructura organizacional describe como se distribuyen las funciones dentro de la organizacion. "
    "En este curso los estudiantes estudiaran la planeacion, la organizacion, la direccion y el control. "
    "Ejercicio 1: describa las funciones de un gerente de operaciones. "
    "Ejercicio 2: explique la diferencia entre estrategia y tactica. "
) * 12

GRAPHICS_ONLY_PAGES = 6


@pytest.fixture(scope="session")
def generated_documents() -> dict[str, dict[str, Any]]:
    """生成 pdf/docx/pptx/xlsx/csv/txt/md 夹具，返回 {token: {path, kind, text}}。"""
    os.makedirs(DOCS_DIR, exist_ok=True)
    out: dict[str, dict[str, Any]] = {}

    # --- TXT / MD ---
    for token, name, text in (
        ("A_TXT_1", "notes_cours.txt", FR_TEXT),
        ("A_MD_1", "resume.md", "# Resume du cours\n\n" + FR_TEXT),
        ("B_RU_TXT", "konspekt.txt", RU_TEXT),
    ):
        path = os.path.join(DOCS_DIR, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        out[token] = {"path": path, "kind": "text", "text": text, "name": name}

    # --- CSV ---
    csv_path = os.path.join(DOCS_DIR, "donnees.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        fh.write("exercice,notion,enonce\n")
        for i in range(1, 25):
            fh.write(f"{i},elasticite,\"Calculer l elasticite-prix de la demande au point {i}\"\n")
    out["A_FR_CSV"] = {"path": csv_path, "kind": "csv", "text": FR_TEXT, "name": "donnees.csv"}

    # --- XLSX ---
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Exercices"
    ws.append(["exercice", "notion", "enonce"])
    for i in range(1, 40):
        ws.append([i, "bilan", f"Enregistrer l operation {i} au journal"])
    ws2 = wb.create_sheet("Rappel")
    for line in FR_TEXT.split(". "):
        ws2.append([line])
    xlsx_path = os.path.join(DOCS_DIR, "exercices.xlsx")
    wb.save(xlsx_path)
    out["A_FR_XLSX"] = {"path": xlsx_path, "kind": "xlsx", "text": FR_TEXT, "name": "exercices.xlsx"}

    # --- DOCX ---
    from docx import Document
    doc = Document()
    doc.add_heading("TD de physique", level=1)
    for para in FR_TEXT.split(". "):
        if para.strip():
            doc.add_paragraph(para.strip() + ".")
    table = doc.add_table(rows=4, cols=3)
    table.cell(0, 0).text = "Exercice"
    table.cell(0, 1).text = "Notion"
    table.cell(0, 2).text = "Enonce"
    for r in range(1, 4):
        table.cell(r, 0).text = str(r)
        table.cell(r, 1).text = "cinematique"
        table.cell(r, 2).text = f"Calculer la vitesse moyenne {r}"
    docx_path = os.path.join(DOCS_DIR, "td_physique.docx")
    doc.save(docx_path)
    out["A_FR_DOCX"] = {"path": docx_path, "kind": "docx", "text": FR_TEXT, "name": "td_physique.docx"}

    # --- PPTX ---
    from pptx import Presentation
    prs = Presentation()
    blank = prs.slide_layouts[5]
    for i, chunk in enumerate([s for s in EN_TEXT.split(". ") if s.strip()][:8], start=1):
        slide = prs.slides.add_slide(blank)
        slide.shapes.title.text = f"Lecture {i}"
        body = slide.placeholders[1] if len(slide.placeholders) > 1 else None
        if body is not None:
            body.text = chunk.strip() + "."
    pptx_path = os.path.join(DOCS_DIR, "cours_slides.pptx")
    prs.save(pptx_path)
    out["A_FR_PPTX"] = {"path": pptx_path, "kind": "pptx", "text": EN_TEXT, "name": "cours_slides.pptx"}

    # --- PDF：文字版 ---
    # 必须用 insert_htmlbox：PyMuPDF 的 base14 字体（helv/tiro）不含西里尔、
    # 汉字和阿拉伯文字形，用 insert_textbox 会得到"字符数正常但语言识别为 unknown"
    # 的假夹具，从而掩盖真实问题。htmlbox 走自带多脚本字体回退。
    import fitz

    def _make_text_pdf(token: str, name: str, text: str, chars_per_page: int = 900) -> None:
        pdf = fitz.open()
        cursor = 0
        while cursor < len(text):
            page = pdf.new_page()
            chunk = text[cursor: cursor + chars_per_page]
            escaped = (chunk.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            page.insert_htmlbox(fitz.Rect(50, 50, 545, 790), f"<p>{escaped}</p>")
            cursor += chars_per_page
        path = os.path.join(DOCS_DIR, name)
        pdf.save(path)
        pdf.close()
        out[token] = {"path": path, "kind": "pdf", "text": text, "name": name}

    _make_text_pdf("A_PDF_1", "cours_chimie.pdf", FR_TEXT)
    _make_text_pdf("B_RU_PDF", "lekciya_algebra.pdf", RU_TEXT)

    # --- PDF：图形主导（几乎没文字，只有大块图片）---
    pdf = fitz.open()
    png = os.path.join(DOCS_DIR, "_blank_image.png")
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 400, 300))
    pix.set_rect(pix.irect, (200, 200, 200))
    pix.save(png)
    for _ in range(GRAPHICS_ONLY_PAGES):
        page = pdf.new_page()
        page.insert_image(page.rect, filename=png)
    graphics_pdf = os.path.join(DOCS_DIR, "graphics_only.pdf")
    pdf.save(graphics_pdf)
    pdf.close()
    out["A_IMG_PDF"] = {"path": graphics_pdf, "kind": "pdf_graphics", "text": "", "name": "graphics_only.pdf"}

    # --- PDF：中文 / 阿拉伯语 / 英语 / 西班牙语（语种配额验证用）---
    _make_text_pdf("A_ZH_PDF", "neike_huli.pdf", ZH_TEXT)
    _make_text_pdf("A_AR_PDF", "qanun_madani.pdf", AR_TEXT)
    _make_text_pdf("A_EN_PDF", "microeconomics.pdf", EN_TEXT)
    _make_text_pdf("A_ES_PDF", "administracion.pdf", ES_TEXT)

    # --- 超限 TXT（>20MB 门槛的迷你等价物：策略里把上限临时调成 0MB 来测，见用例）---
    return out


@pytest.fixture
def file_provider(generated_documents: Mapping[str, Mapping[str, Any]]):
    """按 token 返回真实文件字节；未登记的 token 给一段法语正文。"""
    def provider(token: str) -> bytes:
        entry = generated_documents.get(token)
        if entry and os.path.isfile(entry["path"]):
            with open(entry["path"], "rb") as fh:
                return fh.read()
        return FR_TEXT.encode("utf-8")
    return provider


@pytest.fixture
def fake_adapter(fake_tree, file_provider, generated_documents) -> FakeCloudAdapter:
    """把合成树里的每个 token 映射到对应格式的真实夹具文件。"""
    kind_by_token = {
        "A_TXT_1": "notes_cours.txt", "A_MD_1": "resume.md", "B_RU_TXT": "konspekt.txt",
        "A_FR_CSV": "donnees.csv", "A_FR_XLSX": "exercices.xlsx",
        "A_FR_DOCX": "td_physique.docx", "A_FR_PPTX": "cours_slides.pptx",
        "A_PDF_1": "cours_chimie.pdf", "B_RU_PDF": "lekciya_algebra.pdf",
        "A_ONLINE_DOC": "td_physique.docx", "B_ONLINE_SHEET": "exercices.xlsx",
        "B_ONLINE_SLIDES": "cours_slides.pptx",
        "A_IMG": "graphics_only.pdf",
        "A_IMG_PDF": "graphics_only.pdf",
        "A_ZH_PDF": "neike_huli.pdf",
        "A_AR_PDF": "qanun_madani.pdf",
        "A_EN_PDF": "microeconomics.pdf",
        "A_ES_PDF": "administracion.pdf",
    }
    content_map: dict[str, bytes] = {}
    for token, fname in kind_by_token.items():
        path = os.path.join(DOCS_DIR, fname)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                content_map[token] = fh.read()
    # 超限文件：30MB 的 pdf 内容（策略上限 pdf=100MB，用例里会临时降到 1MB）
    content_map["A_BIG"] = b"%PDF-1.4\n" + (b"0" * (2 * 1024 * 1024))
    # 无扩展名的未知类型
    content_map["A_UNKNOWN"] = b"\x00\x01\x02\x03binary-not-text"
    # 旧格式 doc：OLE2 头
    content_map["A_OLD"] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
    # 图片
    content_map["A_IMG_PDF"] = content_map.get("A_IMG", b"")
    return FakeCloudAdapter(fake_tree, file_provider, page_size_cap=3,
                            content_map=content_map)


# ---------------------------------------------------------------------------
# 假模型
# ---------------------------------------------------------------------------
class FakeLLM:
    """确定性模型替身：输出严格符合契约的 JSON。"""

    name = "fake"
    available = True

    def __init__(self, *, classify: Mapping[str, Any] | None = None,
                 queries: Sequence[Mapping[str, Any]] | None = None,
                 raise_on: str | None = None):
        self.classify_payload = dict(classify or {
            "education_level": "university",
            "subject": "mathematics_statistics",
            "queryability": "high",
            "exclude_kind": None,
            "evidence": ["Chapitre 1 Introduction a la comptabilite generale",
                         "Exercice 1 : enregistrer les operations courantes"],
            "confidence": 0.86,
            "needs_human_review": False,
        })
        self.queries_payload = list(queries or [])
        self.raise_on = raise_on
        self.calls: list[str] = []

    def classify_document(self, document_ref: Mapping[str, Any],
                          policy: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append("classify")
        if self.raise_on == "classify":
            raise RuntimeError("boom")
        return dict(self.classify_payload)

    def generate_queries(self, document_ref: Mapping[str, Any],
                         policy: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append("query")
        if self.raise_on == "query":
            raise RuntimeError("boom")
        if self.queries_payload:
            return {"queries": [dict(q) for q in self.queries_payload]}
        return {"queries": build_valid_queries(policy, str(document_ref.get("language") or "fr"))}

    def generate_questions(self, document_ref: Mapping[str, Any],
                           queries: Sequence[Mapping[str, Any]],
                           policy: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append("questions")
        if self.raise_on == "questions":
            raise RuntimeError("boom")
        # 确定性替身：每条 query 产出它的题量条题目；故意用 explication 键
        # 验证下游键名归一化（P3）——真实法语模型就是这样漂移的。
        items = []
        for q in queries:
            count = int(q.get("question_count") or 2)
            items.append({
                "query_seq": int(q.get("seq") or len(items) + 1),
                "questions": [
                    {"stem": f"Q{i + 1} for seq {q.get('seq')}",
                     "options": {"A": "a", "B": "b", "C": "c", "D": "d"},
                     "answer": "A", "explication": f"explanation {i + 1}"}
                    for i in range(count)
                ],
            })
        return {"items": items}


QUERY_TEMPLATES: dict[str, dict[str, str]] = {
    "en": {
        "multiple_choice": "Generate {n} multiple-choice questions based on this whole document.",
        "fill_in_blank": "Create {n} fill-in-the-blank items from this whole document.",
        "short_answer": "Write {n} short-answer questions covering this whole document.",
        "true_false": "Produce {n} true-or-false statements about this whole document.",
        "mixed": "Design {n} mixed-format questions based on this whole document.",
    },
    "fr": {
        "multiple_choice": "Redigez {n} questions a choix multiples a partir de ce document entier.",
        "fill_in_blank": "Creez {n} questions a trous a partir de ce document entier.",
        "short_answer": "Formulez {n} questions a reponse courte sur ce document entier.",
        "true_false": "Proposez {n} affirmations vrai ou faux sur ce document entier.",
        "mixed": "Concevez {n} questions de formats varies a partir de ce document entier.",
    },
    "es": {
        "multiple_choice": "Genera {n} preguntas de opcion multiple basadas en todo este documento.",
        "fill_in_blank": "Crea {n} preguntas de completar a partir de todo este documento.",
        "short_answer": "Redacta {n} preguntas de respuesta corta sobre todo este documento.",
        "true_false": "Propone {n} afirmaciones de verdadero o falso sobre todo este documento.",
        "mixed": "Disena {n} preguntas de formatos variados a partir de todo este documento.",
    },
    "ru": {
        "multiple_choice": "Составьте {n} вопросов с выбором ответа по всему этому документу.",
        "fill_in_blank": "Подготовьте {n} заданий с пропусками по всему этому документу.",
        "short_answer": "Сформулируйте {n} вопросов с кратким ответом по всему этому документу.",
        "true_false": "Предложите {n} утверждений верно или неверно по всему этому документу.",
        "mixed": "Разработайте {n} заданий смешанного типа по всему этому документу.",
    },
    "zh": {
        "multiple_choice": "给我出 {n} 道选择题。",
        "fill_in_blank": "帮我编 {n} 道填空题。",
        "short_answer": "围绕本文内容设计 {n} 道简答题。",
        "true_false": "用这份资料出 {n} 道判断题。",
        "mixed": "针对这篇文档，来 {n} 道混合题型题目。",
    },
    "ar": {
        "multiple_choice": "أنشئ {n} أسئلة اختيار من متعدد استنادا إلى هذا المستند بأكمله.",
        "fill_in_blank": "أعد {n} أسئلة فراغية استنادا إلى هذا المستند بأكمله.",
        "short_answer": "اكتب {n} أسئلة قصيرة الإجابة حول هذا المستند بأكمله.",
        "true_false": "قدم {n} عبارات صح أو خطأ حول هذا المستند بأكمله.",
        "mixed": "صمم {n} أسئلة متنوعة الصيغ استنادا إلى هذا المستند بأكمله.",
    },
}


def build_valid_queries(policy: Mapping[str, Any], language: str) -> list[dict[str, Any]]:
    """按策略题型表生成一组"应当通过校验"的 query。

    两个要点：
    1. 文案必须用文档语言，否则 query_language_mismatch 会（正确地）拦下来；
    2. 同题型的两条 query 文案必须不同，否则 query_duplicate 会（正确地）拦下来。
       真实模型天然会给不同内容，夹具也必须做到，否则测的是夹具缺陷而不是产品行为。
    """
    table = QUERY_TEMPLATES.get(language) or QUERY_TEMPLATES["en"]
    # 变体后缀：保证同题型内两条文案不同，且不引入定位词
    variants = {
        "en": ("Focus on the main concepts.", "Focus on the applied exercises."),
        "fr": ("Insistez sur les notions principales.", "Insistez sur les exercices pratiques."),
        "es": ("Centrate en los conceptos principales.", "Centrate en los ejercicios practicos."),
        "ru": ("Сделайте акцент на основных понятиях.", "Сделайте акцент на практических заданиях."),
        "zh": ("侧重基本概念。", "侧重实际练习。"),
        "ar": ("ركز على المفاهيم الاساسية.", "ركز على التمارين العملية."),
    }
    suffixes = variants.get(language, variants["en"])

    queries: list[dict[str, Any]] = []
    seq = 0
    for spec in policy.get("query_types") or []:
        key = str(spec.get("type_key"))
        for occurrence in range(int(spec.get("count", 0))):
            seq += 1
            qc = int(spec.get("question_count", 2))
            text = table.get(key, table["mixed"]).format(n=qc)
            text = f"{text} {suffixes[occurrence % len(suffixes)]}".strip()
            queries.append({
                "seq": seq,
                "type_key": key,
                "question_count": qc,
                "language": language,
                "text": text,
                "evidence_locator": {"page": seq},
                "compatibility_only": bool(spec.get("compatibility_only")),
            })
    return queries


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


# ---------------------------------------------------------------------------
# 录制响应夹具读取
# ---------------------------------------------------------------------------
def load_page_fixture(name: str) -> dict[str, Any]:
    path = os.path.join(PAGES_DIR, name)
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


class FixtureRunner:
    """按 argv 关键字返回预置的 CommandResult，用来测分页/错误分支。"""

    def __init__(self, responses: Mapping[str, Sequence[Mapping[str, Any]]]):
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], timeout: int, cwd: str | None = None) -> CommandResult:
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for key, queue in self.responses.items():
            if key in joined:
                if not queue:
                    continue
                item = queue.pop(0)
                return CommandResult(
                    argv=tuple(argv), returncode=int(item.get("returncode", 0)),
                    stdout=item.get("stdout", ""), stderr=item.get("stderr", ""),
                    duration_ms=int(item.get("duration_ms", 1)), cwd=cwd,
                )
        return CommandResult(argv=tuple(argv), returncode=1, stdout="",
                             stderr=f"no fixture for {joined[:120]}", duration_ms=1, cwd=cwd)
