"""录制/回放 Runner。

作用：把真实 lark-cli 的响应录成 JSON 文件，之后测试与回归都跑回放，
不依赖网络也不依赖授权是否过期。计划 10.2「录制响应测试」由本模块实现。

约定：
- 录像文件放在一个目录下，文件名 = 命令签名 + 序号；
- 命令签名由 argv 中"与云端返回相关"的部分决定（剔除 --output 等本地路径参数）；
- 同签名多次调用按录制顺序依次给出；
- 找不到录像时抛 PermanentAdapterError，绝不静默返回空结果。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Sequence

from .. import winpath
from .base import CommandResult, PermanentAdapterError

# 这些参数与"云端返回什么"无关，签名里要剔除，否则本地路径变化会导致回放失配
_VOLATILE_FLAGS = {"--output", "--output-dir", "--file-name", "--overwrite"}
_SECRET_RE = re.compile(
    r"(?i)\b(access[_-]?token|tenant[_-]?access[_-]?token|user[_-]?access[_-]?token"
    r"|app[_-]?secret|api[_-]?key|authorization)\b\s*[=:]\s*\S+"
)
_TOKEN_RE = re.compile(r"\b[uat]-[A-Za-z0-9._\-]{16,}\b")


def redact_secrets(text: str) -> str:
    """把可能的凭据替换成 <redacted>，保证日志/录像里不落密钥。"""
    if not text:
        return text
    out = _SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    return _TOKEN_RE.sub("<redacted-token>", out)


def command_signature(argv: Sequence[str]) -> str:
    """把 argv 归一化成稳定签名（用于录像文件名）。"""
    kept: list[str] = []
    skip_next = False
    for tok in argv:
        if skip_next:
            skip_next = False
            continue
        if tok in _VOLATILE_FLAGS:
            skip_next = True
            continue
        if any(tok.startswith(f"{v}=") for v in _VOLATILE_FLAGS):
            continue
        kept.append(tok)
    joined = " ".join(kept)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", joined)[:80].strip("_")
    return f"{safe}__{digest}"


class RecordingRunner:
    """把真实调用录制成文件。"""

    def __init__(self, inner: Any, record_dir: str):
        self._inner = inner
        self.record_dir = record_dir
        winpath.makedirs(record_dir)
        self._counters: dict[str, int] = {}

    def _next_path(self, sig: str) -> str:
        idx = self._counters.get(sig, 0)
        self._counters[sig] = idx + 1
        return os.path.join(self.record_dir, f"{sig}.{idx:03d}.json")

    def __call__(self, argv: Sequence[str], timeout: int, cwd: str | None = None) -> CommandResult:
        result: CommandResult = self._inner(argv, timeout, cwd)
        path = self._next_path(command_signature(argv))
        payload = {
            "argv": list(argv),
            "cwd": cwd,
            "returncode": result.returncode,
            "stdout": redact_secrets(result.stdout),
            "stderr": redact_secrets(result.stderr),
            "duration_ms": result.duration_ms,
        }
        with winpath.open_write(path, encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return result


class ReplayRunner:
    """按录像回放，不发任何真实请求。"""

    def __init__(self, record_dir: str, *, strict: bool = True):
        self.record_dir = record_dir
        self.strict = strict
        self._queues: dict[str, list[str]] = {}
        self.calls: list[list[str]] = []
        self._load()

    def _load(self) -> None:
        if not winpath.isdir(self.record_dir):
            if self.strict:
                raise PermanentAdapterError(f"录像目录不存在：{self.record_dir}")
            return
        by_sig: dict[str, list[str]] = {}
        for fname in sorted(winpath.listdir(self.record_dir)):
            if not fname.endswith(".json"):
                continue
            sig = fname.rsplit(".", 2)[0]
            by_sig.setdefault(sig, []).append(os.path.join(self.record_dir, fname))
        self._queues = {k: list(v) for k, v in by_sig.items()}

    def __call__(self, argv: Sequence[str], timeout: int, cwd: str | None = None) -> CommandResult:
        self.calls.append(list(argv))
        sig = command_signature(argv)
        queue = self._queues.get(sig)
        if not queue:
            raise PermanentAdapterError(
                f"回放缺少录像：{sig}",
                detail={"signature": sig, "argv": list(argv), "available": sorted(self._queues)[:20]},
            )
        path = queue.pop(0)
        with winpath.open_read(path, encoding="utf-8-sig") as fh:
            payload = json.load(fh)
        return CommandResult(
            argv=tuple(payload.get("argv", list(argv))),
            returncode=int(payload.get("returncode", 0)),
            stdout=payload.get("stdout", ""),
            stderr=payload.get("stderr", ""),
            duration_ms=int(payload.get("duration_ms", 0)),
            cwd=payload.get("cwd", cwd),
        )

    def remaining(self) -> dict[str, int]:
        return {k: len(v) for k, v in self._queues.items() if v}
