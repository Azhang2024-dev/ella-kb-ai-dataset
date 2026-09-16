"""文本统计与语言识别测试（P0 六语种 + 干扰语种 + 边界样本）。"""

from __future__ import annotations

import pytest

from core.document_collection.textstats import (
    cjk_char_count,
    detect_language,
    is_graphics_dominated,
    normalize_text,
    sha256_bytes,
    sha256_file,
    text_fingerprint,
    text_stats,
    truncate_for_prompt,
    word_count,
)

P0_SAMPLES = {
    "en": ("This chapter introduces the fundamental concepts of macroeconomics. The aggregate "
           "demand curve shows the relationship between the price level and output. In this course "
           "we will examine how monetary policy affects inflation and unemployment. Students should "
           "read the sections carefully and complete the exercises at the end of the chapter. "),
    "fr": ("Ce chapitre presente les notions fondamentales de la comptabilite generale. Le bilan "
           "comptable decrit la situation patrimoniale de l entreprise a une date donnee. Dans ce "
           "cours, nous allons etudier comment les operations courantes sont enregistrees au journal. "
           "Les etudiants doivent faire les exercices proposes a la fin de chaque section. "),
    "es": ("Este capitulo introduce los conceptos fundamentales de la administracion de empresas. "
          "La estructura organizacional describe como se distribuyen las funciones dentro de la "
          "organizacion. En este curso vamos a estudiar como se toman las decisiones estrategicas. "
          "Los estudiantes deben leer con atencion y resolver los ejercicios propuestos al final. "),
    "ru": ("Данная глава посвящена основным понятиям линейной алгебры. Матрица представляет собой "
          "прямоугольную таблицу чисел, расположенных в строках и столбцах. В этом курсе мы рассмотрим, "
          "как выполняются операции сложения и умножения матриц. Студенты должны внимательно прочитать "
          "материал и выполнить упражнения в конце раздела. "),
    "ar": ("يتناول هذا الفصل المفاهيم الأساسية في القانون المدني. يعرف العقد بأنه اتفاق بين طرفين "
          "يرتب آثارا قانونية ملزمة. في هذه المادة الدراسية سنتعرف على أركان العقد وشروط صحته. "
          "يجب على الطلاب قراءة النصوص بعناية وحل التمارين الواردة في نهاية الفصل. "),
    "zh": ("本章介绍护理学的基本概念。护理评估是护理程序的第一步，包括收集病人的主观资料和客观资料。"
          "在本课程中，我们将学习如何进行生命体征测量、如何记录护理病历，以及常见疾病的护理措施。"
          "学生应当认真阅读教材并完成课后练习题。"),
}


@pytest.mark.parametrize("language", sorted(P0_SAMPLES))
def test_p0_languages_are_detected_with_high_confidence(language):
    text = P0_SAMPLES[language] * 3
    detected, confidence = detect_language(text)
    assert detected == language
    assert confidence >= 0.5, f"{language} 置信度过低：{confidence}"


def test_non_p0_scripts_are_not_mistaken_for_p0():
    cases = {
        "ja": "これは日本語のテキストです。本章では基本的な概念を説明します。ひらがなとカタカナを含みます。",
        "ko": "이 장에서는 선형대수학의 기본 개념을 소개합니다. 행렬은 숫자의 직사각형 배열입니다. 학생들은 연습문제를 풀어ください.",
        "th": "บทนี้นำเสนอแนวคิดพื้นฐานของเศรษฐศาสตร์มหภาค นักศึกษาควรอ่านและทำแบบฝึกหัดท้ายบท",
        "el": "Αυτό το κεφάλαιο παρουσιάζει τις βασικές έννοιες της γραμμικής άλγεβρας. Οι φοιτητές πρέπει να λύσουν τις ασκήσεις.",
        "hi": "यह अध्याय रैखिक बीजगणित की मूल अवधारणाओं को प्रस्तुत करता है। छात्रों को अभ्यास करने चाहिए।",
    }
    for expected, text in cases.items():
        detected, _ = detect_language(text * 3)
        assert detected == expected, f"期望 {expected}，实际 {detected}"
        assert detected not in {"zh", "ru", "ar"}


# 这些语种全部真实出现在两个采集源里（docQA.llm 有 swahili/urdu/bengali/tamil/
# gujarati/marathi/hindi/hausa/filipino/indonesian/turkish 等目录）。
# 识别不出它们就会退化成 unknown，或者更糟 —— 被误判成某个 P0 语种而误放行。
OUT_OF_SCOPE_SAMPLES = {
    "sw": ("MAENDELEO YA LUGHA YA KISWAHILI NA ATHARI ZAKE KWA JAMII YA KIARABU. "
           "Tasinifu hii inachunguza chimbuko na maendeleo ya Kiswahili. "
           "Utafiti umetumia mbinu za ushiriki, usaili na hojaji. "
           "Watu wengi wanaona kuwa lugha ni kila kitu katika jamii. "),
    "ur": ("اردو زبان کی تاریخ اور ارتقا پر یہ مقالہ لکھا گیا ہے۔ "
          "اس میں اردو کے چیدہ چیدہ مسائل پر بحث کی گئی ہے۔ "
          "طالب علموں کو چاہیے کہ وہ اسے غور سے پڑھیں اور ڈاکٹر ٹیڑھے سوالات کے جواب دیں۔ "),
    "bn": ("এই অধ্যায়ে বাংলা সাহিত্যের ইতিহাস আলোচনা করা হয়েছে। "
          "ছাত্রদের পাঠ্যসূচি অনুযায়ী এই অংশটি পড়তে হবে এবং প্রশ্নের উত্তর দিতে হবে। "),
    "ta": ("இந்த அத்தியாயத்தில் தமிழ் இலக்கியத்தின் வரலாறு விவரிக்கப்பட்டுள்ளது. "
          "மாணவர்கள் பாடத்திட்டத்தின்படி இந்த பகுதியை படிக்க வேண்டும். "),
    "gu": ("આ પ્રકરણમાં ગુજરાતી સાહિત્યના ઇતિહાસની ચર્ચા કરવામાં આવી છે. "
          "વિદ્યાર્થીઓએ અભ્યાસક્રમ મુજબ આ ભાગ વાંચવો જોઈએ. "),
    "id": ("Bab ini membahas sejarah bahasa dan sastra Indonesia. Dalam mata kuliah ini "
          "mahasiswa akan mempelajari perkembangan bahasa Indonesia dari masa ke masa. "
          "Mereka juga harus mengerjakan latihan yang ada pada akhir setiap bab. "),
    "tl": ("Ang kabanatang ito ay tungkol sa kasaysayan ng wikang Filipino. "
          "Ang mga mag-aaral ay dapat basahin ang bahaging ito at sagutan ang mga tanong "
          "na nasa dulo ng bawat kabanata. "),
    "ha": ("Wannan sashe yana tattaunawa kan tarihin harshen Hausa. "
          "Dalibai suna bukatar su karanta wannan bangare kuma su amsa tambayoyin "
          "da ke karshen kowane sashe. "),
    "tr": ("Bu bölüm Türk dili ve edebiyatının tarihini ele almaktadır. "
          "Öğrenciler bu kısmı dikkatle okumalı ve bölüm sonundaki alıştırmaları "
          "yapmalıdır. "),
    "hi": ("यह अध्याय हिंदी साहित्य के इतिहास को प्रस्तुत करता है। "
          "विद्यार्थियों को इस भाग को पढ़ना चाहिए और अध्याय के अंत में दिए गए अभ्यास करने चाहिए। "),
    "pa": ("ਇਸ ਅਧਿਆਇ ਵਿੱਚ ਪੰਜਾਬੀ ਸਾਹਿਤ ਦੇ ਇਤਿਹਾਸ ਦੀ ਚਰਚਾ ਕੀਤੀ ਗਈ ਹੈ। "
          "ਵਿਦਿਆਰਥੀਆਂ ਨੂੰ ਇਹ ਭਾਗ ਪੜ੍ਹਨਾ ਚਾਹੀਦਾ ਹੈ। "),
}


@pytest.mark.parametrize("language", sorted(OUT_OF_SCOPE_SAMPLES))
def test_out_of_scope_languages_present_in_the_sources_are_detected(language):
    """真实 Pilot 的误放行回归：斯瓦希里语曾被判成 es，乌尔都语曾被判成 ar。"""
    text = OUT_OF_SCOPE_SAMPLES[language] * 4
    detected, confidence = detect_language(text)
    assert detected == language, f"期望 {language}，实际 {detected}（conf={confidence}）"
    assert detected not in {"en", "ru", "fr", "zh", "ar", "es"}, \
        f"{language} 被误判成 P0 语种，会导致范围外文档进入最终集"


def test_urdu_is_not_confused_with_arabic():
    """乌尔都语与阿拉伯语共用字母区，必须靠特有字母区分。"""
    urdu = OUT_OF_SCOPE_SAMPLES["ur"] * 4
    arabic = P0_SAMPLES["ar"] * 4
    assert detect_language(urdu)[0] == "ur"
    assert detect_language(arabic)[0] == "ar"


def test_low_confidence_detection_is_flagged_not_guessed():
    """术语密集、虚词稀少的文本必须给低置信度，让筛选层转复核。"""
    text = "Curriculum vitae: Python, SQL, Docker, Kubernetes, TensorFlow, PyTorch. " * 5
    _lang, confidence = detect_language(text)
    assert confidence < 0.70, f"这种样本不该有高置信度：{confidence}"


def test_short_mixed_cjk_latin_falls_back_to_zh():
    text = "本章介绍 Machine Learning 的基本概念，包括 supervised learning 两大类别。"
    detected, confidence = detect_language(text)
    assert detected == "zh"
    assert confidence < 0.6, "弱证据必须给低置信度，不能装作很确定"


def test_empty_and_numeric_only_text_is_unknown():
    assert detect_language("") == ("unknown", 0.0)
    assert detect_language("   \n\t ") == ("unknown", 0.0)
    detected, confidence = detect_language("12345 67890 111213 141516 171819 202122")
    assert detected == "unknown"
    assert confidence < 0.35


def test_normalize_text_ignores_whitespace_case_and_punctuation():
    a = "Hello, World!  This is a TEST."
    b = "hello world this is a test"
    assert normalize_text(a) == normalize_text(b)
    assert text_fingerprint(a) == text_fingerprint(b)


def test_normalize_text_keeps_cjk_and_cyrillic():
    assert "本章介绍" in normalize_text("本章 介绍！")
    assert "глава" in normalize_text("Глава 1.")


def test_container_change_does_not_change_fingerprint():
    """同一正文换了容器（pdf -> docx 导出）也应被判为重复。"""
    body = "Chapitre 1 Introduction a la comptabilite generale. " * 5
    assert text_fingerprint(body) == text_fingerprint(body.replace(". ", ".\n"))


def test_word_count_mixes_cjk_chars_and_latin_words():
    text = "本章介绍 machine learning 两大类别"
    # 8 个汉字（本章介绍 + 两大类别）+ 2 个拉丁词
    assert word_count(text) == 8 + 2


def test_cjk_char_count():
    assert cjk_char_count("本章abc介绍") == 4


def test_text_stats_records_required_metrics():
    stats = text_stats(P0_SAMPLES["zh"] * 20, page_count=12, image_area_ratio=0.1)
    for key in ("char_count", "word_count", "cjk_char_count", "language",
                "language_confidence", "page_count", "image_area_ratio",
                "chars_per_page", "is_long_document", "normalized_fingerprint"):
        assert key in stats, f"需求 2.2 要求记录 {key}"
    assert stats["language"] == "zh"
    assert stats["chars_per_page"] and stats["chars_per_page"] > 0


def test_long_document_flag_uses_the_50k_threshold():
    assert text_stats("x" * 50001)["is_long_document"] is True
    assert text_stats("x" * 50000)["is_long_document"] is False


def test_graphics_dominance_rules():
    # 有页面但完全没有文字层 -> 图片型/扫描型
    dominated, reasons = is_graphics_dominated(char_count=0, page_count=10, image_area_ratio=0.9)
    assert dominated is True and "no_text_layer_on_10_pages" in reasons[0]
    # 图片面积大 + 文字极少 -> 图形主导
    assert is_graphics_dominated(char_count=100, page_count=50, image_area_ratio=0.8)[0] is True
    # 文字充足的普通教材
    assert is_graphics_dominated(char_count=9000, page_count=10, image_area_ratio=0.2)[0] is False


def test_graphics_dominance_needs_image_evidence():
    """关键：只有"文字少"不足以判图形主导，否则淘汰理由是错的。

    单页 30 字的封面、或文字层损坏的 PDF，都属于「文字密度不足」，
    应该走人工复核，而不是被贴上 graphics_dominated 直接淘汰。
    """
    assert is_graphics_dominated(char_count=30, page_count=1, image_area_ratio=None)[0] is False
    assert is_graphics_dominated(char_count=4000, page_count=200, image_area_ratio=None)[0] is False
    # 图片多但文字也多的插图教材不能被误判
    assert is_graphics_dominated(char_count=40000, page_count=30, image_area_ratio=0.85)[0] is False


def test_text_too_sparse_catches_low_density_without_image_evidence():
    from core.document_collection.textstats import is_text_too_sparse
    assert is_text_too_sparse(char_count=30, page_count=1, min_chars=800)[0] is True
    assert is_text_too_sparse(char_count=4000, page_count=200)[0] is True
    sparse, reasons = is_text_too_sparse(char_count=4000, page_count=200)
    assert any("chars_per_page" in r for r in reasons)
    assert is_text_too_sparse(char_count=9000, page_count=10)[0] is False


def test_sha256_helpers():
    assert sha256_bytes(b"abc") == sha256_bytes(b"abc")
    assert sha256_bytes(b"abc") != sha256_bytes(b"abd")
    assert len(sha256_bytes(b"")) == 64


def test_sha256_file_matches_bytes(workdir):
    path = __import__("os").path.join(workdir, "x.bin")
    with open(path, "wb") as fh:
        fh.write(b"abc" * 1000)
    assert sha256_file(path) == sha256_bytes(b"abc" * 1000)


def test_truncate_for_prompt_keeps_head_middle_and_tail():
    text = "".join(f"{i:06d}" for i in range(20000))
    out = truncate_for_prompt(text, 3000)
    assert len(out) < len(text)
    assert out.startswith("000000")
    assert out.endswith(text[-6:])
    assert "TRUNCATED_MIDDLE" in out
    short = "abc"
    assert truncate_for_prompt(short, 3000) == short
