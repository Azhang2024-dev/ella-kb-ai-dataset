"""真实 Pilot 踩坑的回归用例。

这些用例对应的都是 2026-09-09 真实调用飞书云盘时暴露的问题，
不是假想场景；每一条被破坏都会让 Pilot 结果静默失真。
"""

from __future__ import annotations

import json

import pytest

from core.document_collection.acquisition import (
    DEFAULT_SAMPLE_STRATA,
    stratified_sample,
    stratum_key,
)
from core.document_collection.adapters.base import (
    CommandResult,
    PermanentAdapterError,
    TransientAdapterError,
)
from core.document_collection.adapters.lark_cli import LarkCliAdapter, classify_error


# ---------------------------------------------------------------------------
# 分层抽样：第一个维度必须真正交替
# ---------------------------------------------------------------------------
def _items(spec):
    return [{"item_id": i, "item_token": i, "source_name": s, "relative_path": r,
             "extension": e, "item_type": "local_file", "metadata": {"relative_path": r}}
            for i, s, r, e in spec]


def test_sampling_covers_both_sources_even_when_one_has_far_more_strata():
    """回归：按完整层名排序轮转时，docQA.llm 的层全部排在 知识库_2026 之前，
    抽 80 条结果一条都没抽到第一个来源。"""
    spec = []
    # docQA.llm：19 个语种目录 x 3 种扩展名 = 57 个层（ASCII 排序在前）
    for folder in [f"dir{n:02d}" for n in range(19)]:
        for ext in ("pdf", "txt", "docx"):
            for k in range(4):
                spec.append((f"B_{folder}_{ext}_{k}", "docQA.llm", f"{folder}/x{k}.{ext}", ext))
    # 知识库_2026：10 个语种目录（中文，排序在后）
    for folder in ("DOC-FR", "DOC-RU", "DOC-EN", "DOC-ZH", "DOC-AR",
                   "DOC-ES", "DOC-BN", "DOC-TR", "DOC-ID", "DOC-PT"):
        for ext in ("pdf", "md"):
            for k in range(4):
                spec.append((f"A_{folder}_{ext}_{k}", "知识库_2026", f"{folder}/x{k}.{ext}", ext))
    items = _items(spec)
    picked = stratified_sample(items, 80, DEFAULT_SAMPLE_STRATA, seed=20260909)
    assert len(picked) == 80
    by_source = {}
    for item in picked:
        by_source[item["source_name"]] = by_source.get(item["source_name"], 0) + 1
    assert set(by_source) == {"docQA.llm", "知识库_2026"}, by_source
    assert min(by_source.values()) >= 30, f"两个来源应当大致均衡，实际 {by_source}"
    # 前 10 条里两个来源都要出现，证明是真的交替而不是先抽完一个再抽另一个
    assert len({i["source_name"] for i in picked[:10]}) == 2


def test_sampling_covers_rare_extensions():
    spec = [(f"pdf{i}", "A", f"f{i}/x.pdf", "pdf") for i in range(300)]
    spec += [("only_csv", "A", "g/x.csv", "csv")]
    spec += [("only_pptx", "A", "h/x.pptx", "pptx")]
    picked = stratified_sample(_items(spec), 12, seed=5)
    extensions = {i["extension"] for i in picked}
    assert {"csv", "pptx"} <= extensions, f"稀有格式必须被抽到：{extensions}"


def test_sampling_is_reproducible_across_calls():
    spec = [(f"T{i}", "A" if i % 2 else "B", f"f{i % 5}/x{i}.pdf", "pdf") for i in range(50)]
    items = _items(spec)
    a = [i["item_id"] for i in stratified_sample(items, 15, seed=99)]
    b = [i["item_id"] for i in stratified_sample(items, 15, seed=99)]
    assert a == b
    # 打乱输入顺序也不应改变结果（分组前先按 item_id 定序）
    shuffled = list(reversed(items))
    c = [i["item_id"] for i in stratified_sample(shuffled, 15, seed=99)]
    assert a == c


def test_stratum_key_handles_root_level_files():
    item = _items([("T", "知识库_2026", "root_file.pdf", "pdf")])[0]
    assert stratum_key(item, ("source_name", "top_folder", "extension")) == \
        ("知识库_2026", "<root>", "pdf")


# ---------------------------------------------------------------------------
# lark-cli 失败时 JSON 在 stderr，且业务错误码要正确分类
# ---------------------------------------------------------------------------
def _err_body(code, message, etype="api"):
    return json.dumps({"ok": False, "identity": "user",
                       "error": {"type": etype, "subtype": "unknown", "code": code,
                                 "message": message, "log_id": "LOGID123"}},
                      ensure_ascii=False)


def _result_with_stderr(body: str, returncode: int = 1) -> CommandResult:
    return CommandResult(argv=("node", "run.js", "drive", "+export"),
                         returncode=returncode, stdout="", stderr=body, duration_ms=5)


def test_adapter_parses_error_json_from_stderr():
    """实测：lark-cli 成功时 JSON 在 stdout，失败时在 stderr。只看 stdout 会丢错误码。"""
    body = _err_body(1069902, "no permission")

    def runner(argv, timeout, cwd=None):
        return _result_with_stderr(body)

    adapter = LarkCliAdapter("lark-cli", runner=runner, inter_call_delay=0.0, max_retries=0)
    with pytest.raises(PermanentAdapterError) as exc:
        adapter.list_folder("ROOT")
    assert exc.value.detail["code"] == 1069902
    assert exc.value.detail["log_id"] == "LOGID123"
    assert adapter.call_log[0]["error_code"] == 1069902


def test_hybrid_resource_expired_is_retryable():
    """实测：在线 docx 导出返回 code=600 hybrid resource expired，隔几秒重试即成功。"""
    exc = classify_error(json.loads(_err_body(600, "hybrid resource expired")),
                         _result_with_stderr(_err_body(600, "hybrid resource expired")))
    assert isinstance(exc, TransientAdapterError)
    assert exc.retryable is True
    assert exc.detail["code"] == 600


def test_no_permission_and_not_found_are_not_retryable():
    for code, message in ((1069902, "no permission"), (131005, "not found"),
                          (99991663, "app has no required scope")):
        body = _err_body(code, message)
        exc = classify_error(json.loads(body), _result_with_stderr(body))
        assert isinstance(exc, PermanentAdapterError), (code, message)
        assert exc.retryable is False


def test_code_600_is_retried_then_can_succeed():
    ok_body = json.dumps({"ok": True, "data": {"saved_path": "x/y.docx", "size_bytes": 10}},
                         ensure_ascii=False)
    fail_body = _err_body(600, "hybrid resource expired")
    state = {"n": 0}

    def runner(argv, timeout, cwd=None):
        state["n"] += 1
        if state["n"] == 1:
            return _result_with_stderr(fail_body)
        return CommandResult(argv=tuple(argv), returncode=0, stdout=ok_body, stderr="",
                             duration_ms=5)

    slept: list[float] = []
    adapter = LarkCliAdapter("lark-cli", runner=runner, inter_call_delay=0.0,
                             max_retries=2, backoff=(0.0, 0.0), sleep=slept.append)
    data = adapter.export_online("TOK", "docx", "tmp/export", extension="docx")
    assert state["n"] == 2
    assert data["size_bytes"] == 10
    assert slept == [0.0]


def test_persistent_code_600_ends_as_pending_export_not_fake_success():
    """在线 slides 连续重试仍然 600：必须如实标 pending_export。"""
    from conftest import FR_TEXT
    body = _err_body(600, "hybrid resource expired")

    def runner(argv, timeout, cwd=None):
        return _result_with_stderr(body)

    adapter = LarkCliAdapter("lark-cli", runner=runner, inter_call_delay=0.0,
                             max_retries=1, backoff=(0.0,), sleep=lambda _s: None)
    with pytest.raises(TransientAdapterError):
        adapter.export_online("TOK", "slides", "tmp/export", extension="pptx")
    assert len(adapter.call_log) == 2, "应该重试到上限为止"
    assert FR_TEXT  # 仅为确保 conftest 可导入
