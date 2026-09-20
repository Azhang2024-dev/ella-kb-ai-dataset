"""统一 LLM 适配层（Task 6 / Task 7 共用）。

为什么不直接调用 core.doc_processor._llm_call：
1. 那是私有函数，签名和重试策略随时可能为 GUI 需求变化；
2. 新流程需要"结构化 JSON + 校验失败只修复一次"的语义，与现有自由文本生成不同；
3. 计划明确要求 Core 不依赖 GUI 侧私有实现。

因此这里独立实现同构的调用逻辑（provider 前缀、extra_body、429/5xx 指数退避），
并把 litellm 作为可选依赖：没装或没配 key 时自动降级为 OfflineLLMAdapter，
流水线不会假装成功，而是把条目转成 needs_review。
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMUnavailable(RuntimeError):
    """没有可用模型（未安装 litellm / 未配置 key / 显式关闭）。"""


class LLMOutputInvalid(RuntimeError):
    """模型返回了无法解析成约定 JSON 的内容。"""


class LLMBudgetExhausted(RuntimeError):
    """推理模型把 max_tokens 预算耗在推理上，导致正文为空（finish_reason=length）。

    这是独立异常而不是 LLMOutputInvalid，因为处理方式完全不同：
    输出格式错误要修提示词，预算耗尽要调大 max_tokens。
    把它当成"格式错误"重试只会浪费时间（上一次 30 篇任务卡最久的就是它）。
    """


def extract_json_object(raw: str) -> dict[str, Any]:
    """从模型输出里提取第一个 JSON 对象；失败抛 LLMOutputInvalid。"""
    if not raw or not raw.strip():
        raise LLMOutputInvalid("模型返回空内容")
    text = raw.strip()
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            return {"items": obj}
        raise LLMOutputInvalid(f"JSON 顶层不是对象：{type(obj).__name__}")
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[idx:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    raise LLMOutputInvalid(f"无法从输出中解析 JSON：{text[:200]}")


def _extract_reasoning_tokens(usage: Any) -> int:
    """从 usage 里取 reasoning_tokens。

    litellm 的 usage.completion_tokens_details 在不同版本/后端下可能是
    dict，也可能是 pydantic 模型（CompletionTokensDetailsWrapper）——
    后者没有 .get()，直接当成 dict 用会抛 AttributeError（实测踩过）。
    这里两种都兼容，取不到就返回 0。
    """
    if usage is None:
        return 0
    details = getattr(usage, "completion_tokens_details", None)
    if details is None and isinstance(usage, Mapping):
        details = usage.get("completion_tokens_details")
    if details is None:
        return 0
    value = None
    if isinstance(details, Mapping):
        value = details.get("reasoning_tokens")
    else:
        value = getattr(details, "reasoning_tokens", None)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def build_model_name(config: Mapping[str, Any]) -> str:
    """与现有 core.smart_crawler._build_full_provider 同构（独立实现，不依赖私有函数）。"""
    provider = str(config.get("llm_provider", "openai") or "openai")
    model = str(config.get("llm_model", "") or "").strip()
    if not model:
        model = {
            "anthropic": "claude-haiku-4-5-20251001",
            "openai": "gpt-4o-mini",
            "gemini": "gemini-2.0-flash",
            "deepseek": "deepseek-chat",
            "ollama": "llama3",
        }.get(provider, "gpt-4o-mini")
    if provider == "custom":
        return f"openai/{model}"
    if "/" in model and model.split("/", 1)[0] == provider:
        return model
    return f"{provider}/{model}"


def build_extra_body(config: Mapping[str, Any]) -> dict[str, Any] | None:
    model = str(config.get("llm_model", "") or "")
    mode = str(config.get("thinking_mode", "auto") or "auto")
    if mode == "disabled":
        return {"thinking": {"type": "disabled"}}
    if mode == "enabled":
        return None
    if mode == "auto" and model.startswith("gemini-3"):
        return {"thinking": {"type": "disabled"}}
    return None


class LLMAdapter(Protocol):
    """计划 Task 6 要求的统一接口。"""

    name: str
    available: bool

    def classify_document(self, document_ref: Mapping[str, Any],
                          policy: Mapping[str, Any]) -> dict[str, Any]: ...

    def classify_material_type(self, document_ref: Mapping[str, Any],
                               policy: Mapping[str, Any]) -> dict[str, Any]: ...

    def generate_queries(self, document_ref: Mapping[str, Any],
                         policy: Mapping[str, Any]) -> dict[str, Any]: ...

    def generate_questions(self, document_ref: Mapping[str, Any],
                           queries: Sequence[Mapping[str, Any]],
                           policy: Mapping[str, Any]) -> dict[str, Any]: ...


class OfflineLLMAdapter:
    """确定性占位实现。

    用途：单元测试、回归复跑、以及未配置模型时的显式降级。
    它**不产出可用的语义判断**，一律抛 LLMUnavailable，
    由调用方把条目标成 needs_review —— 绝不假装筛选通过。
    """

    name = "offline"
    available = False

    def __init__(self, reason: str = "llm_disabled"):
        self.reason = reason

    def classify_document(self, document_ref: Mapping[str, Any],
                          policy: Mapping[str, Any]) -> dict[str, Any]:
        raise LLMUnavailable(f"离线适配器不可用于语义判定（{self.reason}）")

    def classify_material_type(self, document_ref: Mapping[str, Any],
                               policy: Mapping[str, Any]) -> dict[str, Any]:
        raise LLMUnavailable(f"离线适配器不可用于资料类型判定（{self.reason}）")

    def generate_queries(self, document_ref: Mapping[str, Any],
                         policy: Mapping[str, Any]) -> dict[str, Any]:
        raise LLMUnavailable(f"离线适配器不可用于 Query 生成（{self.reason}）")

    def generate_questions(self, document_ref: Mapping[str, Any],
                           queries: Sequence[Mapping[str, Any]],
                           policy: Mapping[str, Any]) -> dict[str, Any]:
        raise LLMUnavailable(f"离线适配器不可用于题目生成（{self.reason}）")


class LiteLLMAdapter:
    """真实模型调用。同步接口，便于 CLI 顺序执行与断点续跑。"""

    name = "litellm"

    def __init__(self, config: Mapping[str, Any], *, max_tokens: int = 3000,
                 temperature: float | None = None, max_retries: int = 3,
                 backoff: Sequence[float] = (5.0, 10.0, 20.0),
                 sleep: Callable[[float], None] = time.sleep,
                 on_call: Callable[[dict[str, Any]], None] | None = None):
        self.config = dict(config)
        self.max_tokens = int(max_tokens)
        self.temperature = temperature
        self.max_retries = int(max_retries)
        self.backoff = tuple(float(b) for b in backoff) or (5.0,)
        self._sleep = sleep
        self._on_call = on_call or (lambda _rec: None)
        self.model = build_model_name(self.config)
        self.available = self._probe()

    def _probe(self) -> bool:
        try:
            import litellm  # noqa: F401
        except ImportError:
            return False
        return bool(str(self.config.get("llm_api_key", "") or "").strip())

    # -- 底层 -------------------------------------------------------------
    def complete(self, prompt: str, *, max_tokens: int | None = None,
                 temperature: float | None = None, scene: str = "docqa") -> str:
        if not self.available:
            raise LLMUnavailable("litellm 或 api_key 不可用")
        import litellm

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "api_key": str(self.config.get("llm_api_key", "")).strip() or None,
            "max_tokens": int(max_tokens or self.max_tokens),
        }
        temp = self.temperature if temperature is None else temperature
        if temp is not None:
            kwargs["temperature"] = float(temp)
        extra = build_extra_body(self.config)
        if extra:
            kwargs["extra_body"] = extra
        base_url = str(self.config.get("llm_base_url", "") or "").strip()
        if base_url:
            kwargs["base_url"] = base_url

        started = time.monotonic()
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = litellm.completion(**kwargs)
                choice = resp.choices[0]
                content = (choice.message.content or "").strip()
                finish_reason = str(getattr(choice, "finish_reason", "") or "")
                latency = int((time.monotonic() - started) * 1000)
                reasoning_tokens = _extract_reasoning_tokens(getattr(resp, "usage", None))
                if not content:
                    # 预算被推理吃光时，空正文不是"输出格式问题"，而是 max_tokens 不够。
                    # 分开报错，才能让人一眼知道要调大 max_tokens 而不是改提示词。
                    if finish_reason == "length":
                        budget_note = (f"finish_reason=length，推理约 {reasoning_tokens} tokens，"
                                       f"max_tokens={kwargs['max_tokens']}")
                        self._on_call({"scene": scene, "model": self.model, "ok": False,
                                       "prompt_chars": len(prompt), "output_chars": 0,
                                       "latency_ms": latency,
                                       "error": f"LLMBudgetExhausted: {budget_note}"})
                        raise LLMBudgetExhausted(budget_note)
                    raise LLMOutputInvalid("模型返回空内容")
                truncated = finish_reason == "length"
                self._on_call({"scene": scene, "model": self.model, "ok": True,
                               "prompt_chars": len(prompt), "output_chars": len(content),
                               "latency_ms": latency,
                               "error": "truncated_finish_reason_length" if truncated else None})
                return content
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                text = str(exc)
                low = text.lower()
                transient = (
                    "429" in text or "rate limit" in low or "ratelimit" in low
                    or bool(re.search(r"\b(500|502|503|504)\b", text))
                    or "timeout" in low or "timed out" in low
                    or "serviceunavailable" in low or "internalserver" in low
                )
                latency = int((time.monotonic() - started) * 1000)
                if transient and attempt < self.max_retries:
                    wait = self.backoff[min(attempt, len(self.backoff) - 1)]
                    self._sleep(wait)
                    continue
                self._on_call({"scene": scene, "model": self.model, "ok": False,
                               "prompt_chars": len(prompt), "output_chars": 0,
                               "latency_ms": latency,
                               "error": f"{type(exc).__name__}: {text}"[:400]})
                raise
        raise LLMUnavailable(f"模型调用失败：{last_exc}")

    # -- 业务方法 ---------------------------------------------------------
    def classify_document(self, document_ref: Mapping[str, Any],
                          policy: Mapping[str, Any]) -> dict[str, Any]:
        prompt = build_classification_prompt(document_ref, policy)
        raw = self.complete(prompt, scene="classify")
        try:
            return extract_json_object(raw)
        except LLMOutputInvalid:
            # 计划 Task 6：输出格式错误最多修复重试一次
            repair = prompt + (
                "\n\n上一次输出无法解析。请只输出一个 JSON 对象，不要任何解释文字、"
                "不要 Markdown 代码围栏。字段：education_level, subject, queryability, "
                "confidence, evidence, needs_human_review, exclude_kind。"
            )
            raw2 = self.complete(repair, scene="classify_repair")
            return extract_json_object(raw2)

    def classify_material_type(self, document_ref: Mapping[str, Any],
                               policy: Mapping[str, Any]) -> dict[str, Any]:
        prompt = build_material_type_prompt(document_ref, policy)
        raw = self.complete(prompt, scene="material_type")
        try:
            return extract_json_object(raw)
        except LLMOutputInvalid:
            repair = prompt + (
                "\n\n上一次输出无法解析。请只输出一个 JSON 对象，不要任何解释文字、"
                "不要 Markdown 代码围栏。字段：genre, confidence, evidence。"
            )
            raw2 = self.complete(repair, scene="material_type_repair")
            return extract_json_object(raw2)

    def generate_queries(self, document_ref: Mapping[str, Any],
                         policy: Mapping[str, Any]) -> dict[str, Any]:
        prompt = build_query_prompt(document_ref, policy)
        # style_repair：句式同质化时由调用方传入的纠正提示，直接追加到提示词尾部重生成。
        style_repair = policy.get("style_repair")
        if style_repair:
            prompt = prompt + str(style_repair)
        raw = self.complete(prompt, max_tokens=int(policy.get("max_tokens", 6000)),
                            scene="query_gen_style_repair" if style_repair else "query_gen")
        try:
            return extract_json_object(raw)
        except LLMOutputInvalid:
            repair = prompt + (
                "\n\n上一次输出无法解析。请只输出一个 JSON 对象，形如 "
                '{"queries":[{"type_key":"...","question_count":N,"language":"..",'
                '"text":"..","evidence_locator":{..},"compatibility_only":false}]}，'
                "不要任何解释文字。"
            )
            raw2 = self.complete(repair, max_tokens=int(policy.get("max_tokens", 6000)),
                                 scene="query_gen_repair")
            return extract_json_object(raw2)

    def generate_questions(self, document_ref: Mapping[str, Any],
                           queries: Sequence[Mapping[str, Any]],
                           policy: Mapping[str, Any]) -> dict[str, Any]:
        prompt = build_questions_prompt(document_ref, queries, policy)
        raw = self.complete(prompt, max_tokens=int(policy.get("max_tokens", 32000)),
                            scene="question_gen")
        try:
            return extract_json_object(raw)
        except LLMOutputInvalid:
            repair = prompt + (
                "\n\n上一次输出无法解析。请只输出一个 JSON 对象，形如 "
                '{"items":[{"query_seq":1,"questions":[{"stem":"..","options":{"A":".."},'
                '"answer":"..","explanation":".."}]}]}，不要任何解释文字。'
            )
            raw2 = self.complete(repair, max_tokens=int(policy.get("max_tokens", 32000)),
                                 scene="question_gen_repair")
            return extract_json_object(raw2)




def build_classification_prompt(document_ref: Mapping[str, Any],
                               policy: Mapping[str, Any]) -> str:
    text = str(document_ref.get("text") or "")
    meta = {k: v for k, v in document_ref.items() if k != "text"}
    subjects = policy.get("subjects") or {}
    subject_lines = _subject_choice_lines(subjects)
    return f"""你是知识库评测集的文档筛选助手。请只依据给出的正文样本判断，不要臆测。

【文档元信息】
{json.dumps(meta, ensure_ascii=False, indent=2)[:2000]}

【正文样本】
{text}

【筛选口径】
- education_level: university | vocational | school | general | unknown
  只有能从正文证明属于大学/高等教育材料（课程、教材、讲义、实验指导、作业、TD、试卷、考试材料）才算 university。
- subject: 只能从这个封闭集合里选一个最贴切的 key（括号内是含义）；正文主题不在其中时必须填 unknown，
  **不要**选用集合外的学科（例如心理学/社会学/政治学/艺术学/文学/历史学/哲学/机械工程都不在范围内）：
{subject_lines}
  判定依据是"文档的研究/教学主题"，不是它出现在哪个文件夹或文件名前缀。
- queryability: high | medium | low，表示正文是否足以支撑 10 条互不相同、可核验的题目。
- exclude_kind: 命中以下任一类别就填对应英文 key，否则填 null：
  {json.dumps(policy.get("exclusions", []), ensure_ascii=False)}
- evidence: 3-6 条直接摘自正文的短句（每条不超过 60 字），用来支撑上面的判断。没有证据就不要给高置信度。
- confidence: 0-1 的小数。
- needs_human_review: 证据不足或存在矛盾时为 true。

只输出一个 JSON 对象：
{{"education_level":"...","subject":"...","queryability":"...","exclude_kind":null,
"evidence":["..."],"confidence":0.0,"needs_human_review":false}}"""


def build_material_type_prompt(document_ref: Mapping[str, Any],
                               policy: Mapping[str, Any]) -> str:
    """资料类型判定提示词（交付表「类型」列用）。

    只给正文头部样本：抽样显示 200–1200 字就足以定体裁，没必要喂全文。
    口径是**宽口径真实体裁**（语料里确实存在学术论文/官方手册/课程大纲等），
    折叠成五类由代码确定性地做，模型不负责收口。
    """
    text = str(document_ref.get("text") or "")[:1800]
    meta = {k: v for k, v in document_ref.items() if k != "text"}
    genres = policy.get("genres") or []
    five = policy.get("five_types") or []
    return f"""你是知识库评测集的资料类型判定助手。请只依据给出的正文样本判断，不要臆测。

【文档元信息】
{json.dumps(meta, ensure_ascii=False, indent=2)[:1200]}

【正文头部样本】
{text}

【真实体裁（genre）只能从这个封闭集合里选一个】：
{json.dumps(genres, ensure_ascii=False)}
说明：
- 学术论文：带摘要/关键词/作者单位/期刊卷期/参考文献的论文。
- 官方手册：官方指南、规范文件、参考书（ISBN/УДК/WHO 等）、合同协议。
- 课程大纲：Programa/Syllabus/Guía docente/Учебный план/Рабочая программа。
- 实验指导：实验目的/装置/步骤类实验手册。
- 教材/讲义/试卷/课堂笔记/作业习题：按字面含义。
- 其他：都不像时选它（不要硬套）。
注意：文件名前缀（exam_/lecture_/misc_）是采集分桶标签，**不是**体裁依据；
以正文头部为准。

- confidence: 0-1 的小数。
- evidence: 1-3 条直接摘自样本的短句（每条不超过 60 字），用来支撑判断。

只输出一个 JSON 对象：
{{"genre":"...","confidence":0.0,"evidence":["..."]}}"""


# 学科 key 的中文释义：喂给分类提示词，让模型知道每个 key 到底指什么，
# 避免只给英文 key 时把"心理学论文"硬塞进最近似的 key（历史缺陷 R1/R2）。
SUBJECT_LABELS_ZH: dict[str, str] = {
    "economics_management_accounting_finance": "经济 / 管理 / 会计 / 金融",
    "computer_science_it": "计算机 / 信息技术",
    "mathematics_statistics": "数学与统计（公共基础课）",
    "law": "法学",
    "education_linguistics": "教育学 / 语言学",
    "clinical_medicine_nursing": "临床医学 / 护理",
    "basic_science": "基础理科（物理 / 化学 / 生物）",
}


def _subject_choice_lines(subjects: Mapping[str, Any]) -> str:
    """把策略的 P0/P1 学科 key 平铺成 "-key（中文释义）" 列表，附优先级别。"""
    lines: list[str] = []
    for tier in ("p0", "p1", "p1_extra"):
        for key in subjects.get(tier, ()) or ():
            k = str(key)
            label = SUBJECT_LABELS_ZH.get(k, "")
            tier_zh = {"p0": "P0", "p1": "P1", "p1_extra": "P1+"}.get(tier, tier)
            lines.append(f"  - {k}（{label}）〔{tier_zh}〕" if label else f"  - {k}〔{tier_zh}〕")
    return "\n".join(lines) or "  - （策略未声明学科）"


def build_query_prompt(document_ref: Mapping[str, Any], policy: Mapping[str, Any]) -> str:
    text = str(document_ref.get("text") or "")
    language = str(document_ref.get("language") or "en")
    types = policy.get("query_types") or []
    rules = {
        "whole_document_only": policy.get("whole_document_only", True),
        "forbidden_reference_kinds": policy.get("forbidden_reference_kinds", []),
        "question_count_min": policy.get("question_count_min", 2),
        "question_count_max": policy.get("question_count_max", 11),
        "language_follows_document": policy.get("language_follows_document", True),
    }
    type_lines = "\n".join(
        f"- {t.get('type_key')}（{t.get('type_zh','')}）：{t.get('count')} 条，每条题量 {t.get('question_count')}"
        + ("，标记 compatibility_only=true" if t.get("compatibility_only") else "")
        for t in types
    )
    return f"""请为下面这份文档生成评测 query。

【硬性规则】
1. 每条 query 面向**整篇文档**，绝对不要在 query 文案里提到片段、章节、页码、Sheet、表格或"第 X 部分"。
2. 每条 query 请求的题目数必须 < 12；同一份文档内不同题型的题量尽量不重复。
3. query 语言必须与文档语言一致：{language}。
4. 不要承诺无法保障的范围或难度（例如"覆盖全书""难度中等以上"）。
5. 每条 query 都要给出内部核验依据 evidence_locator（页码/段落/Sheet/表格），
   这些信息只写进 JSON，不写进 query 文案。
6. 混合题型 query 标记 compatibility_only=true。

【句式要求（重要）】
这些 query 要**模拟真实用户对文档出题的说话方式**，像人随口提需求，而不是套模板：
- 用口语化的短句，例如"给我出5道选择题""根据这篇材料出几道判断题""帮我出2道解答题，带参考答案"。
- 允许指令式、请求式、疑问式混用，可以带"帮我/给我/麻烦/请"等口语词，也可以直接下指令。
- **同一份文档的 10 条 query，开头结构和句式必须各不相同**：不要每条都用"请根据本文档……"开头，
  也不要用同一个动词（如反复用"请根据/请基于/请依据"）。不同条之间至少换用不同的谓语与语气。
- 不要写"第五章""表3"这类定位词，也不要写"覆盖全书"这类承诺。

【可参考的句式（用文档语言表达，10 条各用不同的开场，不要原样照抄同一条）】
1) 直接下指令：给我出 N 道{{题型}}。
2) 以材料为依据：根据这篇材料出几道{{题型}}。
3) 请求帮忙：帮我编 N 道{{题型}}。
4) 给出范围：围绕本文内容设计 N 道{{题型}}。
5) 疑问式：这份文档能出 N 道{{题型}}吗？请写出来。
6) 先陈述需求：我需要 N 道{{题型}}，请依据正文编写。
7) 口语化：用这份资料出 N 道{{题型}}。
8) 客气语气：麻烦按本文内容拟 N 道{{题型}}。
9) 指明对象：针对这篇文档，来 N 道{{题型}}。
10) 从属关系：从这份材料里出 N 道{{题型}}。
（以上是中文示意；请用文档语言写出等价的、口语化且互不重复的表达。）

【题型与条数】
{type_lines}

【规则参数】
{json.dumps(rules, ensure_ascii=False)}

【文档语言】{language}
【正文】
{text}

只输出一个 JSON 对象：
{{"queries":[{{"seq":1,"type_key":"multiple_choice","question_count":5,"language":"{language}",
"text":"...","evidence_locator":{{"page":12}},"compatibility_only":false}}]}}"""


_EXPLANATION_ALIASES = ("explication", "explicación", "explicacion", "explicação",
                        "erklärung", "erklarung", "объяснение", "شرح", "解法",
                        "解析说明", "explanationtext")


def normalize_question(question: Mapping[str, Any]) -> dict[str, Any]:
    """把模型给的题目对象归一成固定键（P3）。

    按语种漂移过：法语把 `explanation` 写成 `explication`，导致下游按固定键取解析拿到空值。
    这里把常见别名折回标准键，并保证 stem/options/answer/explanation 四个键始终存在，
    未知键原样保留（不丢信息）。
    """
    out: dict[str, Any] = dict(question)
    for alias in _EXPLANATION_ALIASES:
        if alias in out and not out.get("explanation"):
            out["explanation"] = out.pop(alias)
        elif alias in out:
            out.pop(alias, None)
    for alias in ("question", "题干", "enonce", "énoncé", "pregunta", "вопрос", "السؤال"):
        if alias in out and not out.get("stem"):
            out["stem"] = out.pop(alias)
        elif alias in out:
            out.pop(alias, None)
    for alias in ("reponse", "réponse", "respuesta", "ответ", "الإجابة", "答案"):
        if alias in out and not out.get("answer"):
            out["answer"] = out.pop(alias)
        elif alias in out:
            out.pop(alias, None)
    if not isinstance(out.get("options"), Mapping):
        out.setdefault("options", {})
    for key in ("stem", "answer", "explanation"):
        value = out.get(key)
        out[key] = "" if value is None else str(value)
    return out


def build_questions_prompt(document_ref: Mapping[str, Any],
                           queries: Sequence[Mapping[str, Any]],
                           policy: Mapping[str, Any]) -> str:
    """参考题目生成提示词（P8 正式化；窗口来自 document_ref，不在此处二次截断）。"""
    text = str(document_ref.get("text") or "")
    language = str(document_ref.get("language") or "en")
    lang_zh = {"en": "英文", "zh": "中文", "fr": "法文", "ru": "俄文",
               "ar": "阿拉伯文", "es": "西班牙文"}.get(language, language)
    query_brief = [
        {"seq": q.get("seq"), "type_key": q.get("type_key"),
         "type_zh": q.get("type_zh"), "question_count": q.get("question_count"),
         "query": q.get("text")}
        for q in queries
    ]
    return f"""下面是一篇文档的全文，以及已经为它确定的 10 条出题 query（含题型与题量）。
请严格按照每条 query 的题型与题量，把对应的题目**实际写出来**，作为参考题目（含标准答案与解析）。

【语言要求】
所有题目、选项、答案、解析都必须使用**{lang_zh}**（与文档正文语言一致）。

【硬性规则】
1. 题目必须依据文档正文出题，答案能在原文中找到依据；不要编造文档里没有的内容。
2. 选择题：题干 + 4 个选项（A/B/C/D）+ 正确答案 + 解析。
3. 判断题：陈述 + 对/错 + 解析。
4. 填空题：题干（留空用 ____ 标记）+ 答案 + 解析。
5. 解答题：题干 + 参考答案 + 解析（关键得分点）。
6. 混合题型：在该条内自由组合上述题型（总数等于该条题量），每种都带答案解析。
7. 每条 query 生成一个 JSON 对象，放进 "items" 数组，含 query_seq（对应 query 的 seq）和 questions 数组。
8. 只输出一个 JSON 对象，不要任何解释文字或 Markdown 围栏。
9. **JSON 的键名必须用英文原样**（stem / options / answer / explanation），
   不要翻译成 explication、pregunta、شرح 等其它语言，否则解析会判缺失。

输出格式：
{{"items":[{{"query_seq":1,"questions":[{{"stem":"...","options":{{"A":"...","B":"...","C":"...","D":"..."}},"answer":"B","explanation":"..."}}]}}]}}

【文档全文】
{text}

【本条文档的 query 列表】
{json.dumps(query_brief, ensure_ascii=False, indent=1)}"""


def load_llm_config(path: str | os.PathLike[str] | None) -> dict[str, Any]:
    if not path:
        return {}
    resolved = os.fspath(path)
    if not os.path.isfile(resolved):
        return {}
    try:
        with open(resolved, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def build_adapter(policy_llm: Mapping[str, Any], *, repo_root: str | None = None,
                  on_call: Callable[[dict[str, Any]], None] | None = None) -> LLMAdapter:
    """按策略构造适配器；不可用时返回 OfflineLLMAdapter（显式降级，不伪装成功）。"""
    if not policy_llm.get("enabled"):
        return OfflineLLMAdapter("policy.llm.enabled=false")
    cfg_path = str(policy_llm.get("config_path") or "")
    if cfg_path and not os.path.isabs(cfg_path):
        cfg_path = os.path.join(repo_root or os.getcwd(), cfg_path)
    config = load_llm_config(cfg_path)
    if not config:
        return OfflineLLMAdapter(f"llm_config_not_found:{cfg_path}")
    try:
        adapter = LiteLLMAdapter(
            config,
            max_tokens=int(policy_llm.get("max_tokens", 3000)),
            temperature=policy_llm.get("temperature"),
            max_retries=int(policy_llm.get("max_retries", 3)),
            on_call=on_call,
        )
    except Exception as exc:  # noqa: BLE001
        return OfflineLLMAdapter(f"init_failed:{type(exc).__name__}")
    if not adapter.available:
        return OfflineLLMAdapter("litellm_or_api_key_unavailable")
    return adapter
