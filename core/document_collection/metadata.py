"""确定性元数据兜底：学科与教育属性关键词推断（多语种，含阿拉伯语）。

背景（改动计划 P7 / debug 报告 D3/D4）：未启用语义复核时 `subject`/`education_level`
全空，入选清单只能用文件名启发式，且阿语学科词缺失（ar 40 份只标出 5 份）。
真实模型判定仍是首选；本模块提供**不依赖模型**的确定性兜底，只在模型没给结论时补位，
绝不覆盖模型的判断（模型说 unknown 时才回落）。

词表按"一个学科 key 对应多语种关键词"组织，6 个 P0 语种都覆盖。
只做**包含匹配**，因此宁可漏判也不误判：命中即给 key，未命中给 unknown 并保留 evidence。
"""

from __future__ import annotations

import re
from typing import Any, Sequence

# 与策略 subjects.p0 / p1 的 key 对齐；新增学科时同步补词表。
SUBJECT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "economics_management_accounting_finance": (
        # en
        "economics", "economy", "finance", "financial", "accounting", "management",
        "marketing", "business", "microeconomics", "macroeconomics", "audit",
        # zh
        "经济", "金融", "会计", "财务", "管理", "市场营销", "工商", "审计",
        # fr
        "economie", "économie", "gestion", "comptabilite", "comptabilité",
        "finance", "marketing", "entreprise",
        # es
        "economia", "economía", "gestion", "gestión", "contabilidad", "finanzas",
        "empresa", "administracion", "administración",
        # ru
        "экономика", "финансы", "бухгалтер", "менеджмент", "маркетинг",
        # ar
        "اقتصاد", "مالية", "محاسبة", "إدارة", "الأعمال", "تجارة", "تسويق", "مصرف",
    ),
    "computer_science_it": (
        "computer", "software", "algorithm", "programming", "database",
        "machine learning", "neural", "artificial intelligence", "network",
        "计算机", "软件", "算法", "程序", "数据库", "人工智能", "机器学习", "网络",
        "informatique", "algorithme", "logiciel", "programmation", "reseau", "réseau",
        "informatica", "informática", "algoritmo", "programacion", "programación",
        "информатика", "программирование", "алгоритм", "база данных",
        "حاسوب", "برمجة", "خوارزم", "قاعدة بيانات", "شبكات", "ذكاء اصطناعي",
    ),
    "mathematics_statistics": (
        "mathematics", "mathematical", "statistics", "statistical", "algebra",
        "calculus", "geometry", "probability", "theorem",
        "数学", "统计", "代数", "微积分", "几何", "概率", "定理",
        "mathematiques", "mathématiques", "statistique", "algebre", "algèbre",
        "geometrie", "géométrie", "probabilite", "probabilité",
        "matematicas", "matemáticas", "estadistica", "estadística", "algebra",
        "geometria", "geometría", "probabilidad",
        "математика", "статистика", "алгебра", "геометрия", "вероятность",
        "رياضيات", "إحصاء", "جبر", "هندسة", "احتمال",
    ),
    "law": (
        "law", "legal", "jurisprudence", "civil code", "criminal", "constitution",
        "法律", "法学", "民法", "刑法", "宪法", "法规",
        "droit", "juridique", "code civil", "penal", "pénal", "constitution",
        "derecho", "juridico", "jurídico", "civil", "penal", "constitucion", "constitución",
        "право", "юридическ", "гражданск", "уголовн", "конституц",
        "قانون", "القانون", "المدني", "الجنائي", "دستور", "حقوق", "تشريع",
    ),
    "education_linguistics": (
        "education", "pedagog", "didactic", "linguistic", "language teaching",
        "learning", "curriculum",
        "教育", "教学", "语言学", "课程", "教材",
        "education", "éducation", "pedagogie", "pédagogie", "didactique",
        "linguistique", "enseignement",
        "educacion", "educación", "pedagogia", "pedagogía", "didactica",
        "didáctica", "linguistica", "lingüística", "ensenanza", "enseñanza",
        "образован", "педагог", "лингвист", "преподаван",
        "تعليم", "تربية", "لسانيات", "تدريس", "منهج", "لغة",
    ),
    "clinical_medicine_nursing": (
        "medicine", "medical", "clinical", "nursing", "patient", "diagnosis",
        "anatomy", "pharmacology",
        "医学", "临床", "护理", "病人", "诊断", "解剖", "药理",
        "medecine", "médecine", "clinique", "soins", "infirmier", "patient",
        "diagnostic", "anatomie", "pharmacologie",
        "medicina", "clínica", "clinica", "enfermeria", "enfermería", "paciente",
        "diagnostico", "diagnóstico", "anatomia", "anatomía", "farmacologia",
        "медицин", "клинич", "сестринск", "пациент", "диагноз", "анатомия",
        "طب", "الطب", "تمريض", "مريض", "تشخيص", "تشريح", "صيدلة", "سريري",
    ),
    "basic_science": (
        "physics", "chemistry", "biology", "science", "laboratory", "experiment",
        "物理", "化学", "生物", "科学", "实验",
        "physique", "chimie", "biologie", "science", "laboratoire", "experience",
        "fisica", "física", "quimica", "química", "biologia", "biología",
        "ciencia", "laboratorio", "experimento",
        "физик", "химия", "биолог", "наук", "лаборатор", "эксперимент",
        "فيزياء", "كيمياء", "أحياء", "علم", "مختبر", "تجربة",
    ),
}

# 教育属性关键词：只用于"模型没给结论"时的兜底提示，不作为放行依据。
EDUCATION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "university": (
        "university", "undergraduate", "faculty", "lecture", "syllabus", "textbook",
        "курс", "лекция", "университет", "студент",
        "大学", "本科", "教材", "讲义", "课程", "教学大纲",
        "universite", "université", "cours", "etudiant", "étudiant", "licence", "master",
        "universidad", "curso", "estudiante", "grado", "apuntes",
        "جامعة", "محاضرة", "منهج", "طالب", "كلية", "مقرر",
    ),
    "school": (
        "high school", "secondary school", "primary school", "pupil",
        "中学", "高中", "初中", "小学",
        "lycee", "lycée", "college", "collège", "eleve", "élève",
        "colegio", "instituto", "alumno", "secundaria",
        "школ", "ученик", "класс",
        "مدرسة", "الثانوي", "الابتدائي", "تلميذ", "طالب مدرسة",
    ),
}

_WS_RE = re.compile(r"\s+")
# 匹配用的定长扫描窗口：关键词命中只反映"文档讲了什么"，无需全篇。
_SCAN_CHARS = 20000


def _haystack(name: str | None, text: str | None) -> str:
    """文件名权重更高（标题最直接），拼成一个小写的匹配串。"""
    head = f"{(name or '')} " * 3
    body = (text or "")[:_SCAN_CHARS]
    return (head + body).lower()


def infer_subject(name: str | None, text: str | None,
                  allowed: Sequence[str] | None = None) -> tuple[str, list[str]]:
    """按关键词表推断学科 key；返回 (subject|unknown, 命中的关键词)。

    allowed 用于把结果限制在策略声明的学科集合内（如 P0/P1），避免给出未登记 key。
    """
    hay = _haystack(name, text)
    if not hay.strip():
        return "unknown", []
    allow = {str(a).lower() for a in allowed} if allowed else None
    best_key, best_hits = "unknown", []
    for key, words in SUBJECT_KEYWORDS.items():
        if allow is not None and key.lower() not in allow:
            continue
        hits = [w for w in words if w and w.lower() in hay]
        # 命中数多者胜；同分保持词表声明顺序（deterministic）
        if len(hits) > len(best_hits):
            best_key, best_hits = key, hits
    return (best_key, best_hits) if best_hits else ("unknown", [])


def infer_education_level(name: str | None, text: str | None) -> tuple[str, list[str]]:
    """按关键词表推断教育属性；返回 (level|unknown, 命中的关键词)。"""
    hay = _haystack(name, text)
    if not hay.strip():
        return "unknown", []
    # university 优先：出现"课程/讲义/教材"等词时即便也命中 school 词，仍按高校材料算
    for level in ("university", "school"):
        hits = [w for w in EDUCATION_KEYWORDS[level] if w and w.lower() in hay]
        if hits:
            return level, hits
    return "unknown", []


def metadata_hints(name: str | None, text: str | None, *,
                   allowed_subjects: Sequence[str] | None = None) -> dict[str, Any]:
    """一次性给出学科/教育属性兜底提示，供 screening 写进 evidence。"""
    subject, subject_hits = infer_subject(name, text, allowed=allowed_subjects)
    level, level_hits = infer_education_level(name, text)
    return {
        "subject_hint": subject,
        "subject_hint_keywords": subject_hits[:6],
        "education_level_hint": level,
        "education_level_hint_keywords": level_hits[:6],
    }
