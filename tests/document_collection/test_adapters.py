"""适配器层测试：只读白名单、Windows .cmd 分页坑、录制/回放、脱敏、重试分类。

这里覆盖了 Task 0 实测到的三个真实接口事实，任何一条被破坏都会立刻失败：
1. next_page_token 含 "|"，绝不能经 cmd.exe 二次解析；
2. drive +download/--export 的 --output 必须是相对 workdir 的路径；
3. files list / metas batch_query 都不返回 size。
"""

from __future__ import annotations

import json
import os

import pytest

from conftest import PAGES_DIR, load_page_fixture

from core.document_collection.adapters.base import (
    PermanentAdapterError,
    SafetyViolation,
    TransientAdapterError,
)
from core.document_collection.adapters.lark_cli import (
    DEFAULT_ALLOWLIST,
    LarkCliAdapter,
    classify_error,
    extract_json,
    resolve_cli_argv,
)
from core.document_collection.adapters.recorded import (
    RecordingRunner,
    ReplayRunner,
    command_signature,
    redact_secrets,
)
from core.document_collection.adapters.base import CommandResult


def _stdout(fixture_name: str) -> str:
    with open(os.path.join(PAGES_DIR, fixture_name), "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 接口事实
# ---------------------------------------------------------------------------
def test_page_token_with_pipe_is_passed_as_single_argv_element():
    """回归：lark-cli.CMD 经 cmd.exe 时，token 里的 | 被当成管道，第 2 页全部失败。"""
    captured: list[list[str]] = []

    def runner(argv, timeout, cwd=None):
        captured.append(list(argv))
        return CommandResult(argv=tuple(argv), returncode=0,
                             stdout=_stdout("page2_of_2.json"), stderr="", duration_ms=1)

    adapter = LarkCliAdapter("lark-cli", runner=runner, inter_call_delay=0.0)
    token = "v3|wpjCj2XDkR48CwPCjUQiwpBiMcKCw5nCiRAHwoPCq2VMwpXDi8K_wrvCnsO5U8K"
    page = adapter.list_folder("ROOT", page_token=token, page_size=200)
    assert len(captured) == 1
    argv = captured[0]
    assert token in argv, "page token 必须是独立 argv 元素，不能被拼接进 shell 字符串"
    assert "--page-token" in argv
    assert argv.index("--page-token") + 1 == argv.index(token)
    assert len(page.entries) == 2


def test_resolve_cli_argv_bypasses_cmd_shim_when_possible():
    """真实机器上应解析成 node + run.js；解析不到时退回 .cmd 也不报错。"""
    argv = resolve_cli_argv("lark-cli")
    assert argv, "必须能解析出可执行 argv"
    if len(argv) == 2:
        assert argv[0].lower().endswith(("node.exe", "node"))
        assert argv[1].replace("\\", "/").endswith("@larksuite/cli/scripts/run.js")
    else:
        assert os.path.exists(argv[0])


def test_download_output_is_relative_to_workdir(workdir):
    captured: list[list[str]] = []

    def runner(argv, timeout, cwd=None):
        captured.append((list(argv), cwd))
        return CommandResult(argv=tuple(argv), returncode=0,
                             stdout=_stdout("download_ok.json"), stderr="", duration_ms=1)

    adapter = LarkCliAdapter("lark-cli", runner=runner, inter_call_delay=0.0, workdir=workdir)
    result = adapter.download_file("TOK_A1", os.path.join(workdir, "artifacts", "x.pdf"))
    argv, cwd = captured[0]
    out_value = argv[argv.index("--output") + 1]
    assert not os.path.isabs(out_value), "--output 必须是相对路径，否则 lark-cli 直接拒绝"
    assert out_value == "artifacts/x.pdf"
    assert cwd == os.path.abspath(workdir)
    assert result["size_bytes"] == 2048


def test_download_target_outside_workdir_is_rejected(workdir):
    adapter = LarkCliAdapter("lark-cli", runner=lambda *a, **k: None,
                             inter_call_delay=0.0, workdir=workdir)
    outside = os.path.join(os.path.dirname(os.path.abspath(workdir)), "escape.bin")
    with pytest.raises(PermanentAdapterError):
        adapter.download_file("TOK_A1", outside)


def test_files_list_response_has_no_size_field():
    """实测事实：清单里没有 size，remote_size 只能为 None。"""
    payload = load_page_fixture("page1_of_2.json")
    for entry in payload["data"]["files"]:
        assert "size" not in entry
    metas = load_page_fixture("metas_no_size.json")
    for meta in metas["data"]["metas"]:
        assert "size" not in meta and "size_bytes" not in meta


# ---------------------------------------------------------------------------
# 只读安全
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("args", [
    ["drive", "+delete", "--file-token", "X"],
    ["drive", "+move", "--file-token", "X", "--type", "folder", "--token", "Y"],
    ["drive", "+update-title", "--token", "X", "--title", "hacked"],
    ["drive", "+upload", "--file", "x"],
    ["drive", "+create-folder", "--name", "x"],
    ["drive", "files", "patch", "--token", "X"],
    ["docs", "+update", "--doc", "X"],
])
def test_write_commands_are_blocked(args):
    adapter = LarkCliAdapter("lark-cli", runner=lambda *a, **k: None, inter_call_delay=0.0)
    with pytest.raises(SafetyViolation):
        adapter._invoke(args)


def test_allowlist_covers_exactly_the_readonly_commands():
    assert DEFAULT_ALLOWLIST == (
        ("drive", "files", "list"),
        ("drive", "metas", "batch_query"),
        ("drive", "+inspect"),
        ("drive", "+download"),
        ("drive", "+export"),
    )


def test_allowlist_comes_from_policy(real_policy):
    from_policy = tuple(tuple(str(c).split()) for c in real_policy.allowlisted_lark_commands)
    assert from_policy == DEFAULT_ALLOWLIST


# ---------------------------------------------------------------------------
# 错误分类与重试
# ---------------------------------------------------------------------------
def _result(stdout: str, returncode: int = 0, stderr: str = "") -> CommandResult:
    return CommandResult(argv=("lark-cli",), returncode=returncode,
                         stdout=stdout, stderr=stderr, duration_ms=1)


@pytest.mark.parametrize("fixture,expected", [
    ("error_429.json", TransientAdapterError),
    ("error_500.json", TransientAdapterError),
    ("error_timeout.json", TransientAdapterError),
    ("error_permission.json", PermanentAdapterError),
])
def test_error_classification(fixture, expected):
    exc = classify_error(json.loads(_stdout(fixture)), _result(_stdout(fixture)))
    assert isinstance(exc, expected)


def test_transient_error_is_retried_then_succeeds():
    calls = {"n": 0}

    def runner(argv, timeout, cwd=None):
        calls["n"] += 1
        body = _stdout("error_429.json") if calls["n"] == 1 else _stdout("page2_of_2.json")
        return CommandResult(argv=tuple(argv), returncode=0, stdout=body, stderr="", duration_ms=1)

    slept: list[float] = []
    adapter = LarkCliAdapter("lark-cli", runner=runner, max_retries=2,
                             backoff=(0.0, 0.0), inter_call_delay=0.0, sleep=slept.append)
    page = adapter.list_folder("ROOT", page_size=200)
    assert calls["n"] == 2
    assert len(page.entries) == 2
    assert slept == [0.0]


def test_permanent_error_is_not_retried():
    calls = {"n": 0}

    def runner(argv, timeout, cwd=None):
        calls["n"] += 1
        return CommandResult(argv=tuple(argv), returncode=0,
                             stdout=_stdout("error_permission.json"), stderr="", duration_ms=1)

    adapter = LarkCliAdapter("lark-cli", runner=runner, max_retries=3,
                             backoff=(0.0,), inter_call_delay=0.0, sleep=lambda _s: None)
    with pytest.raises(PermanentAdapterError):
        adapter.list_folder("ROOT", page_size=200)
    assert calls["n"] == 1, "不可重试错误不应该浪费调用次数"


def test_extract_json_tolerates_progress_lines():
    noisy = 'Downloading: TOK...A1\n' + _stdout("download_with_progress.json")
    payload = extract_json(noisy)
    assert payload and payload["ok"] is True
    assert payload["data"]["size_bytes"] == 4096


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------
def test_redact_secrets_removes_tokens_and_keys():
    raw = _stdout("redaction_sample.json")
    cleaned = redact_secrets(raw)
    assert "u-abcdef0123456789ABCDEF" not in cleaned
    assert "t-abcdef0123456789ABCDEF" not in cleaned
    assert "<redacted" in cleaned


def test_recording_and_replay_roundtrip(workdir):
    record_dir = os.path.join(workdir, "recordings")
    bodies = [_stdout("page1_of_2.json"), _stdout("page2_of_2.json")]
    state = {"i": 0}

    def real_runner(argv, timeout, cwd=None):
        body = bodies[min(state["i"], len(bodies) - 1)]
        state["i"] += 1
        return CommandResult(argv=tuple(argv), returncode=0, stdout=body, stderr="", duration_ms=3)

    recording = RecordingRunner(real_runner, record_dir)
    adapter = LarkCliAdapter("lark-cli", runner=recording, inter_call_delay=0.0)
    first = adapter.list_folder("ROOT", page_size=200)
    assert len(first.entries) == 3
    assert len(os.listdir(record_dir)) == 1

    replay = ReplayRunner(record_dir)
    adapter2 = LarkCliAdapter("lark-cli", runner=replay, inter_call_delay=0.0)
    second = adapter2.list_folder("ROOT", page_size=200)
    assert [dict(e) for e in second.entries] == [dict(e) for e in first.entries]
    assert replay.remaining() == {}


def test_replay_missing_recording_raises_instead_of_empty_result(workdir):
    record_dir = os.path.join(workdir, "empty_recordings")
    os.makedirs(record_dir, exist_ok=True)
    replay = ReplayRunner(record_dir)
    adapter = LarkCliAdapter("lark-cli", runner=replay, inter_call_delay=0.0)
    with pytest.raises(PermanentAdapterError):
        adapter.list_folder("ROOT", page_size=200)


def test_command_signature_ignores_local_output_paths():
    a = command_signature(["lark-cli", "drive", "+download", "--file-token", "T",
                           "--output", "one/path.pdf", "--overwrite"])
    b = command_signature(["lark-cli", "drive", "+download", "--file-token", "T",
                           "--output", "another/path.pdf", "--overwrite"])
    assert a == b, "本地输出路径变化不应该导致回放失配"
    c = command_signature(["lark-cli", "drive", "+download", "--file-token", "OTHER",
                           "--output", "one/path.pdf", "--overwrite"])
    assert a != c
