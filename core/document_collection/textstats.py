"""文本统计、语言识别与规范化指纹。

为什么自己实现语言识别而不引入 langdetect/fasttext：
1. P0 语种（en/ru/fr/zh/ar/es）在字符层面差异极大，脚本（script）判别已经足够稳；
2. 只有拉丁语系内部需要区分，用高频虚词表即可，误判可由 screening 转 needs_review；
3. 两周内不引入需要训练数据/二进制依赖的库，降低 Windows 环境失败面。

判定不确定时返回 confidence < 阈值，由 screening 决定是 reject 还是 needs_review，
绝不"猜一个语言然后放行"。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter

# ---------------------------------------------------------------------------
# Unicode 脚本区间
# ---------------------------------------------------------------------------
_RANGES: tuple[tuple[str, int, int], ...] = (
    ("cjk", 0x4E00, 0x9FFF),
    ("cjk_ext_a", 0x3400, 0x4DBF),
    ("cjk_compat", 0xF900, 0xFAFF),
    ("hiragana", 0x3040, 0x309F),
    ("katakana", 0x30A0, 0x30FF),
    ("hangul", 0xAC00, 0xD7A3),
    ("hangul_jamo", 0x1100, 0x11FF),
    ("cyrillic", 0x0400, 0x04FF),
    ("arabic", 0x0600, 0x06FF),
    ("arabic_sup", 0x0750, 0x077F),
    ("arabic_pres_a", 0xFB50, 0xFDFF),
    ("arabic_pres_b", 0xFE70, 0xFEFF),
    ("hebrew", 0x0590, 0x05FF),
    ("thai", 0x0E00, 0x0E7F),
    ("greek", 0x0370, 0x03FF),
    ("latin", 0x0041, 0x024F),
    ("latin_ext_add", 0x1E00, 0x1EFF),
    # 两个采集源里真实出现的文字（docQA.llm 有 bengali/tamil/gujarati/marathi/
    # hindi/urdu 等目录）。不识别它们就会退化成 unknown 或者被误判成某个 P0 语种。
    ("devanagari", 0x0900, 0x097F),
    ("bengali", 0x0980, 0x09FF),
    ("gurmukhi", 0x0A00, 0x0A7F),
    ("gujarati", 0x0A80, 0x0AFF),
    ("oriya", 0x0B00, 0x0B7F),
    ("tamil", 0x0B80, 0x0BFF),
    ("telugu", 0x0C00, 0x0C7F),
    ("kannada", 0x0C80, 0x0CFF),
    ("malayalam", 0x0D00, 0x0D7F),
    ("sinhala", 0x0D80, 0x0DFF),
    ("myanmar", 0x1000, 0x109F),
    ("georgian", 0x10A0, 0x10FF),
    ("khmer", 0x1780, 0x17FF),
    ("lao", 0x0E80, 0x0EFF),
    ("tibetan", 0x0F00, 0x0FFF),
    ("armenian", 0x0530, 0x058F),
    ("ethiopic", 0x1200, 0x137F),
    ("thaana", 0x0780, 0x07BF),
    ("syriac", 0x0700, 0x074F),
)

# 脚本 -> 语言代码（单一语言占主导的脚本）
_SCRIPT_LANGUAGE: tuple[tuple[str, str], ...] = (
    ("devanagari", "hi"), ("bengali", "bn"), ("gurmukhi", "pa"), ("gujarati", "gu"),
    ("oriya", "or"), ("tamil", "ta"), ("telugu", "te"), ("kannada", "kn"),
    ("malayalam", "ml"), ("sinhala", "si"), ("myanmar", "my"), ("georgian", "ka"),
    ("khmer", "km"), ("lao", "lo"), ("tibetan", "bo"), ("armenian", "hy"),
    ("ethiopic", "am"), ("thaana", "dv"), ("syriac", "syc"),
    ("hebrew", "he"), ("thai", "th"), ("greek", "el"),
)

# 乌尔都语与阿拉伯语共用阿拉伯字母区，只能靠乌尔都语特有字母区分。
# docQA.llm 里有 urdu 目录，不加这一步就会把乌尔都语误判成阿拉伯语（P0 语种），
# 属于"把范围外语种放进最终集"的严重误放行。
_URDU_MARKERS = set("\u0679\u067e\u0680\u0683\u0684\u0685\u0686\u0687\u0688\u068c"
                    "\u068d\u068e\u068f\u0690\u0691\u0692\u0693\u0698\u069a\u069e"
                    "\u06a0\u06a2\u06a3\u06a5\u06a6\u06a7\u06a8\u06a9\u06aa\u06ab"
                    "\u06ac\u06ad\u06ae\u06af\u06b0\u06b1\u06b2\u06b3\u06ba\u06bb"
                    "\u06bc\u06be\u06bf\u06c0\u06c1\u06c2\u06c3\u06cc\u06cd\u06ce"
                    "\u06cf\u06d0\u06d1\u06d2\u06d3")

# 拉丁语系高频虚词表（只放区分度高的词，避免长表拖慢）
_STOPWORDS: dict[str, tuple[str, ...]] = {
    "en": (
        "the", "of", "and", "to", "in", "is", "that", "for", "it", "with", "as", "was",
        "on", "be", "by", "this", "are", "or", "from", "at", "an", "not", "have", "has",
        "which", "their", "there", "when", "what", "would", "can", "will", "you", "your",
    ),
    "fr": (
        "le", "la", "les", "des", "une", "est", "et", "dans", "pour", "que", "qui", "sur",
        "pas", "plus", "par", "avec", "tout", "faire", "son", "mais", "nous", "comme",
        "ont", "ce", "il", "aux", "du", "cette", "ces", "etre", "vous", "vos", "aussi",
        "peut", "sont", "entre", "dans", "elle", "ses",
    ),
    "es": (
        "el", "la", "los", "las", "una", "uno", "es", "y", "en", "del", "que", "por",
        "con", "para", "su", "al", "lo", "como", "mas", "pero", "sus", "le", "ya",
        "este", "porque", "esta", "entre", "cuando", "muy", "sin", "sobre", "tambien",
        "hasta", "hay", "donde", "quien", "desde", "todo", "nos", "durante", "todos",
        "puede", "son", "ser",
    ),
    "pt": (
        "o", "a", "os", "as", "um", "uma", "de", "da", "do", "que", "e", "em", "para",
        "com", "nao", "no", "se", "na", "por", "mais", "dos", "como", "mas", "foi",
        "ao", "ele", "das", "tem", "seu", "sua", "ou", "ser", "quando", "muito",
        "ha", "nos", "ja", "esta", "eu", "tambem", "so", "pelo", "pela", "ate", "isso",
    ),
    "de": (
        "der", "die", "das", "und", "ist", "von", "zu", "den", "mit", "sich", "des",
        "auf", "fur", "ein", "im", "dem", "nicht", "eine", "als", "auch", "es", "an",
        "werden", "aus", "er", "hat", "dass", "sie", "nach", "wird", "bei", "einer",
        "um", "am", "sind", "noch", "wie", "einem", "uber", "einen", "so", "zum", "war",
    ),
    "it": (
        "il", "lo", "la", "gli", "le", "un", "uno", "una", "di", "da", "in", "con",
        "su", "per", "tra", "fra", "che", "non", "si", "ma", "anche", "come", "piu",
        "suo", "sua", "sono", "essere", "questo", "questa", "questi", "queste", "ha",
        "hanno", "del", "della", "dei", "delle", "alla", "nel", "nella", "sul", "sulla",
    ),
    "tr": (
        "bir", "ve", "bu", "icin", "ile", "de", "da", "mi", "cok", "gibi", "daha",
        "kadar", "her", "ne", "ama", "olan", "var", "bazi", "tum", "ben", "sen",
    ),
    # 斯瓦希里语：真实 Pilot 里出现过一份 128 页的斯瓦希里语博士论文，
    # 因为只命中了少量西班牙语虚词（la/ya 之类）被判成 es 并误放行。
    "sw": (
        "na", "ya", "wa", "za", "ni", "kwa", "katika", "hii", "hiyo", "yake", "wake",
        "mwake", "kama", "lakini", "pia", "hivyo", "ambayo", "kwamba", "kati", "baada",
        "kabla", "zaidi", "sana", "watu", "lugha", "kila", "hapa", "pale", "kuna",
        "wengi", "mwingine", "yote", "ili", "kwa", "juu", "chini", "ndani",
    ),
    "id": (
        "yang", "dan", "di", "untuk", "pada", "dengan", "ini", "itu", "dari", "dalam",
        "tidak", "akan", "juga", "atau", "ada", "mereka", "sudah", "satu", "telah",
        "oleh", "karena", "sehingga", "dapat", "adalah", "kepada", "lebih", "antara",
        "bagi", "hanya", "seperti", "lain", "kita", "saya", "anda", "bahasa",
    ),
    "tl": (
        "ang", "ng", "sa", "na", "ay", "kaya", "para", "ito", "siya", "kami", "tayo",
        "sila", "hindi", "at", "kung", "dahil", "tungkol", "upang", "rin", "din",
        "naman", "lang", "mga", "isang", "lahat", "dito", "doon", "wala", "may",
        "tungkol", "sanaysay", "wikang", "wika",
    ),
    "ha": (
        "cikin", "kuma", "don", "shi", "ita", "suna", "amma", "idan", "saboda", "haka",
        "kamar", "wani", "mutane", "harshe", "kowane", "yi", "ne", "ce", "ba", "in",
        "daga", "zuwa", "guda", "sai", "ko", "yana", "tana", "wannan", "wadannan",
    ),
    "vi": (
        "va", "cua", "la", "trong", "cho", "khong", "voi", "nay", "do", "khi", "tren",
        "duoc", "mot", "nhu", "hoac", "cung", "ma", "cua", "nha", "nguoi", "ho",
    ),
}

_WORD_RE = re.compile(r"[A-Za-zÀ-ɏ]+")
_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^0-9a-z\u00c0-\u024f\u0400-\u04ff\u0600-\u06ff\u4e00-\u9fff]+")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str, chunk_size: int = 1 << 20) -> str:
    from .winpath import long_path

    h = hashlib.sha256()
    with open(long_path(path), "rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def count_scripts(text: str) -> Counter:
    """统计各 Unicode 脚本的字符数。"""
    counter: Counter = Counter()
    for ch in text:
        code = ord(ch)
        for name, lo, hi in _RANGES:
            if lo <= code <= hi:
                counter[name] += 1
                break
    return counter


def _strip_accents(word: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c))


def _urdu_share(text: str) -> float:
    """乌尔都语特有字母在全部字母中的占比。"""
    if not text:
        return 0.0
    hits = sum(1 for ch in text[:20000] if ch in _URDU_MARKERS)
    return hits / max(1, len(text[:20000]))


def detect_language(text: str, sample_chars: int = 20000) -> tuple[str, float]:
    """返回 (语言代码, 置信度 0-1)。

    判定顺序：
    1. 强脚本特征（阿拉伯/CJK/假名/谚文/西里尔/各印度文字/希伯来/泰/希腊等）直接定语言；
       阿拉伯字母区先看乌尔都语特有字母，避免把 urdu 误判成 ar（P0 语种）；
    2. 拉丁为主时用高频虚词命中率区分 en/fr/es/pt/de/it/tr/sw/id/tl/ha/vi；
    3. 虚词一个都没命中（术语密集的短样本）时，回退到弱脚本特征；
    4. 仍不明确 -> ("unknown", 低置信度)，由 screening 转 needs_review。

    绝不"猜一个语言然后放行"：置信度如实反映证据强度，
    筛选层会用 min_confidence_for_accept 卡住低置信度的放行。
    """
    if not text or not text.strip():
        return "unknown", 0.0

    sample = text[:sample_chars]
    counts = count_scripts(sample)
    total_letters = sum(counts.values())
    if total_letters == 0:
        return "unknown", 0.0

    def ratio(*names: str) -> float:
        return sum(counts.get(n, 0) for n in names) / total_letters

    arabic_r = ratio("arabic", "arabic_sup", "arabic_pres_a", "arabic_pres_b")
    cjk_r = ratio("cjk", "cjk_ext_a", "cjk_compat")
    kana_r = ratio("hiragana", "katakana")
    hangul_r = ratio("hangul", "hangul_jamo")
    cyr_r = ratio("cyrillic")
    latin_r = ratio("latin", "latin_ext_add")

    # 1) 强脚本特征
    if arabic_r >= 0.30:
        # 乌尔都语与阿拉伯语共用字母区，必须先看特有字母
        urdu = _urdu_share(sample)
        if urdu >= 0.01:
            return "ur", round(min(1.0, 0.7 + urdu * 8.0), 4)
        return "ar", round(min(1.0, arabic_r * 1.3), 4)
    if kana_r >= 0.05 and cjk_r > 0:
        return "ja", round(min(1.0, (kana_r + cjk_r) * 1.2), 4)
    if hangul_r >= 0.15:
        return "ko", round(min(1.0, hangul_r * 1.2), 4)
    if cjk_r >= 0.25:
        # CJK 为主但混有大量拉丁（中英混排教材）仍判 zh，置信度按占比给
        return "zh", round(min(1.0, cjk_r * 1.2), 4)
    if cyr_r >= 0.30:
        return "ru", round(min(1.0, cyr_r * 1.2), 4)

    # 印度系及其他单一语言脚本
    for script_name, code in _SCRIPT_LANGUAGE:
        value = ratio(script_name)
        if value >= 0.30:
            return code, round(min(1.0, value * 1.25), 4)

    # 2) 拉丁语系：虚词命中打分
    if latin_r >= 0.20:
        words = _WORD_RE.findall(sample)
        if words:
            lowered = [_strip_accents(w).lower() for w in words]
            total_words = len(lowered)
            freq = Counter(lowered)
            scores = {lang: sum(freq[s] for s in stops) / total_words
                      for lang, stops in _STOPWORDS.items()}
            best_lang, best_score = max(scores.items(), key=lambda kv: kv[1])
            if best_score > 0.0:
                ordered = sorted(scores.values(), reverse=True)
                margin = best_score - (ordered[1] if len(ordered) > 1 else 0.0)
                confidence = min(1.0, best_score * 6.0) * (0.6 + min(1.0, margin * 20.0) * 0.4)
                return best_lang, round(confidence, 4)

    # 3) 弱脚本回退（术语密集/短样本时虚词表可能一个都不命中）
    for code, value in (("ar", arabic_r), ("zh", cjk_r), ("ja", kana_r),
                        ("ko", hangul_r), ("ru", cyr_r)):
        if value >= 0.10:
            return code, round(min(0.55, value * 1.2), 4)
    for script_name, code in _SCRIPT_LANGUAGE:
        if ratio(script_name) >= 0.10:
            return code, round(min(0.55, ratio(script_name) * 1.25), 4)

    if latin_r >= 0.20:
        # 拉丁字母为主但无法归入已知语言：交给上层复核，不硬判 en
        return "unknown", round(min(0.3, latin_r), 4)
    return "unknown", 0.0


def normalize_text(text: str) -> str:
    """去重用的规范化：NFKC + casefold + 去空白与标点，只留字母数字汉字。"""
    if not text:
        return ""
    norm = unicodedata.normalize("NFKC", text).casefold()
    norm = _NON_ALNUM_RE.sub("", norm)
    return norm


def text_fingerprint(text: str) -> str:
    """规范化文本 SHA-256，用于跨来源正文去重。"""
    return sha256_bytes(normalize_text(text).encode("utf-8"))


def word_count(text: str, language: str | None = None) -> int:
    """词数：CJK 按字符计，其他按空白分词。"""
    if not text:
        return 0
    counts = count_scripts(text[:200000])
    cjk = counts.get("cjk", 0) + counts.get("cjk_ext_a", 0) + counts.get("cjk_compat", 0)
    kana = counts.get("hiragana", 0) + counts.get("katakana", 0)
    hangul = counts.get("hangul", 0)
    non_cjk = re.sub(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]", " ", text)
    latin_words = len([w for w in _WS_RE.split(non_cjk) if w.strip()])
    return cjk + kana + hangul + latin_words


def cjk_char_count(text: str) -> int:
    counts = count_scripts(text)
    return counts.get("cjk", 0) + counts.get("cjk_ext_a", 0) + counts.get("cjk_compat", 0)


def text_stats(text: str, *, page_count: int | None = None,
               image_area_ratio: float | None = None) -> dict[str, object]:
    """采集阶段要求同时记录的指标（需求 2.2）。"""
    chars = len(text or "")
    lang, conf = detect_language(text or "")
    stats: dict[str, object] = {
        "char_count": chars,
        "word_count": word_count(text or "", lang),
        "cjk_char_count": cjk_char_count(text or ""),
        "line_count": (text or "").count("\n") + (1 if text else 0),
        "language": lang,
        "language_confidence": conf,
        "page_count": page_count,
        "image_area_ratio": image_area_ratio,
        "chars_per_page": round(chars / page_count, 2) if page_count else None,
        "is_long_document": chars > 50000,
        "normalized_fingerprint": text_fingerprint(text or "") if chars else None,
    }
    return stats


def is_graphics_dominated(*, char_count: int, page_count: int | None = None,
                          image_area_ratio: float | None = None,
                          min_chars: int = 800,
                          min_chars_per_page: int = 60,
                          image_area_threshold: float = 0.6) -> tuple[bool, list[str]]:
    """判断"图形主导"（需求 2.3：只有核心知识依赖图形时才淘汰）。

    必须有指向图形的证据才判 True，否则会把"文字密度低"的解析问题
    误标成图形主导，报告里给出的淘汰理由就是错的：
    - 完全没有正文：有页数说明是图片型/扫描型页面，无页数说明抽取不到任何文字；
    - 图片面积占比高，且文字总量或每页文字量都很少。

    只是"文字少但没有图形证据"的情况交给 is_text_too_sparse，走人工复核。
    """
    reasons: list[str] = []
    if char_count <= 0:
        if page_count and page_count > 0:
            reasons.append(f"no_text_layer_on_{page_count}_pages")
        else:
            reasons.append("no_extracted_text")
        return True, reasons

    heavy_images = image_area_ratio is not None and image_area_ratio >= image_area_threshold
    if heavy_images:
        if char_count < min_chars * 4:
            reasons.append(
                f"image_area_ratio={image_area_ratio:.2f}>={image_area_threshold} "
                f"and text sparse (char_count={char_count})"
            )
            return True, reasons
        if page_count and page_count > 0 and char_count / page_count < min_chars_per_page:
            reasons.append(
                f"image_area_ratio={image_area_ratio:.2f}>={image_area_threshold} "
                f"and chars_per_page={char_count / page_count:.1f}<{min_chars_per_page}"
            )
            return True, reasons
    return False, reasons


def is_text_too_sparse(*, char_count: int, page_count: int | None = None,
                       min_chars: int = 800,
                       min_chars_per_page: int = 60) -> tuple[bool, list[str]]:
    """文字密度不足、但没有图形证据 -> 需要人工复核，不能当成图形主导直接淘汰。

    典型场景：PDF 文字层损坏、编码异常、或确实是单页短材料。
    """
    reasons: list[str] = []
    if char_count < min_chars:
        reasons.append(f"char_count={char_count}<{min_chars}")
    if page_count and page_count > 1 and char_count / page_count < min_chars_per_page:
        reasons.append(
            f"chars_per_page={char_count / page_count:.1f}<{min_chars_per_page} "
            f"(pages={page_count})"
        )
    return (len(reasons) > 0), reasons


def truncate_for_prompt(text: str, max_chars: int) -> str:
    """给 LLM 的取样：头 + 中 + 尾，避免只看开头误判长文档。"""
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    mid = max_chars // 4
    tail = max_chars - head - mid
    cut = len(text) // 2
    return (
        text[:head]
        + "\n...[TRUNCATED_MIDDLE]...\n"
        + text[max(0, cut - mid // 2): max(0, cut - mid // 2) + mid]
        + "\n...[TRUNCATED_TAIL_START]...\n"
        + text[-tail:]
    )
