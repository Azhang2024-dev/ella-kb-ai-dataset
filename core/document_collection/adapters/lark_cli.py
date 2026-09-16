"""lark-cli 适配器（只读）。

实测确认的接口事实（Task 0，2026-09-09，lark-cli 1.0.85）：
1. `drive files list --folder-token X --page-size N --format json`
   返回 {ok, identity, data:{files:[...], has_more, next_page_token}, _notice}；
   每条 file 只有 created_time/modified_time/name/owner_id/parent_token/token/type/url，
   **没有 size 字段**。
2. `drive metas batch_query --data {...}` 返回 metas，同样**没有 size**。
   => 大小门槛只能在下载后按本地字节数判定（策略里已记录该限制）。
3. `drive +download --file-token X --output P` 的 P **必须是相对当前工作目录的路径**，
   绝对路径会被拒绝：`--output must be a relative path within the current directory`。
   => 适配器统一以 workdir 作为 cwd，并把目标路径转成相对路径。
   返回 {ok, data:{saved_path, size_bytes}}。
4. `drive +export --token X --doc-type T --file-extension E --output-dir D`
   用于在线 docx/sheet/slides/bitable/wiki 导出。
5. stdout 可能混有进度行，因此 JSON 解析要能在文本中定位第一个 JSON 对象。

安全：只允许 DEFAULT_ALLOWLIST 里的只读子命令；任何写操作在适配器层直接抛
SafetyViolation，不依赖调用方自觉。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence

from .base import (
    CommandResult,
    ListPage,
    PermanentAdapterError,
    SafetyViolation,
    TransientAdapterError,
)
from .. import winpath
from .recorded import redact_secrets

DEFAULT_ALLOWLIST: tuple[tuple[str, ...], ...] = (
    ("drive", "files", "list"),
    ("drive", "metas", "batch_query"),
    ("drive", "+inspect"),
    ("drive", "+download"),
    ("drive", "+export"),
)

Runner = Callable[[Sequence[str], int, str | None], CommandResult]

_HTTP_5XX_RE = re.compile(r"\b(500|502|503|504)\b")
_TRANSIENT_SUBSTRINGS = (
    "429", "rate limit", "ratelimit", "too many requests", "timeout",
    "timed out", "serviceunavailable", "internalserver", "eof",
    "connection reset", "temporarily",
    # 实测（2026-09-09）：在线文档导出会返回 code=600 "hybrid resource expired"，
    # 同一份 docx 隔几秒重试就成功了，说明它是服务端资源缓存过期，属于可重试。
    # 但在线 slides 连续重试 3 次仍然 600，所以重试次数用完后要如实标 pending_export。
    "hybrid resource expired",
)
# 飞书业务错误码里可重试的部分。1069902(no permission)、131005(not found) 不在此列。
_TRANSIENT_API_CODES = {600}


def resolve_cli_path(cli_path: str | None = None) -> str:
    """定位 lark-cli 可执行文件。

    Windows 上 npm 全局包会生成 lark-cli.cmd / lark-cli.ps1，
    subprocess 不经 shell 时需要显式拿到 .cmd 的绝对路径。
    """
    if cli_path:
        if os.path.isabs(cli_path) and os.path.exists(cli_path):
            return cli_path
        found = shutil.which(cli_path)
        if found:
            return found
        raise PermanentAdapterError(f"找不到 lark-cli：{cli_path}")
    for candidate in ("lark-cli", "lark-cli.cmd", "lark"):
        found = shutil.which(candidate)
        if found:
            return found
    raise PermanentAdapterError(
        "PATH 中找不到 lark-cli；请在策略 source_access.lark_cli_path 里配置绝对路径"
    )


def resolve_cli_argv(cli_path: str | None = None) -> tuple[str, ...]:
    """返回真正要执行的 argv 前缀。

    实测踩坑（2026-09-09）：npm 在 Windows 上生成的是 `lark-cli.CMD` 垫片，
    CreateProcess 执行 .cmd 时会走 cmd.exe 做二次解析。飞书分页返回的
    `next_page_token` 形如 `v3|wpjCj2XDkR48...`，其中的 `|` 被 cmd.exe 当成管道，
    于是第 2 页及以后全部失败：
        'v3|wpj...' is not recognized as an internal or external command
    解决办法是绕过垫片，直接 `node <npm>/node_modules/@larksuite/cli/scripts/run.js`，
    参数由 Python 的 list2cmdline 正确引用，不经过任何 shell。
    """
    resolved = resolve_cli_path(cli_path)
    if resolved.lower().endswith((".cmd", ".bat")):
        npm_dir = os.path.dirname(os.path.abspath(resolved))
        run_js = os.path.join(npm_dir, "node_modules", "@larksuite", "cli", "scripts", "run.js")
        node_exe = os.path.join(npm_dir, "node.exe")
        if not os.path.isfile(node_exe):
            node_exe = shutil.which("node") or ""
        if os.path.isfile(run_js) and node_exe:
            return (node_exe, run_js)
    return (resolved,)


def subprocess_runner(argv: Sequence[str], timeout: int, cwd: str | None = None) -> CommandResult:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        duration = int((time.monotonic() - started) * 1000)
        return CommandResult(
            argv=tuple(argv), returncode=-1,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=f"timeout after {timeout}s",
            duration_ms=duration, cwd=cwd,
        )
    duration = int((time.monotonic() - started) * 1000)
    return CommandResult(
        argv=tuple(argv), returncode=proc.returncode,
        stdout=proc.stdout or "", stderr=proc.stderr or "",
        duration_ms=duration, cwd=cwd,
    )


def extract_json(text: str) -> dict[str, Any] | None:
    """从可能混有进度行的输出里提取第一个完整 JSON 对象。"""
    if not text:
        return None
    stripped = text.lstrip("\ufeff").strip()
    try:
        obj = json.loads(stripped)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for idx, ch in enumerate(stripped):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(stripped[idx:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def classify_error(payload: Mapping[str, Any] | None, result: CommandResult) -> Exception:
    """把 CLI 失败归成可重试 / 不可重试。

    实测：lark-cli 成功时 JSON 走 stdout，失败时 JSON 走 **stderr**（rc=1/2）。
    只看 stdout 会丢掉结构化的 error.code，导致把可重试错误误判成永久错误。
    """
    err = (payload or {}).get("error") or {}
    message = str(err.get("message") or result.stderr or result.stdout or "unknown error")
    message = redact_secrets(message)
    blob = message.lower()
    etype = str(err.get("type") or "").lower()
    esub = str(err.get("subtype") or "").lower()
    code = err.get("code")
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        code_int = None

    transient = (
        etype in ("rate_limit", "timeout", "network", "server_error")
        or esub in ("rate_limit", "timeout", "too_many_requests", "server_error")
        or any(s in blob for s in _TRANSIENT_SUBSTRINGS)
        or bool(_HTTP_5XX_RE.search(message))
        or result.returncode == -1
        or (code_int is not None and code_int in _TRANSIENT_API_CODES)
    )
    detail = {
        "type": etype or None,
        "subtype": esub or None,
        "code": code_int if code_int is not None else (str(code) if code else None),
        "returncode": result.returncode,
        "retryable": transient,
        "log_id": err.get("log_id"),
        "message": message[:400],
    }
    if transient:
        return TransientAdapterError(message[:400], detail=detail)
    return PermanentAdapterError(message[:400], detail=detail)


class LarkCliAdapter:
    """飞书云盘只读适配器。"""

    name = "lark_cli"

    def __init__(
        self,
        cli_path: str | None = None,
        *,
        timeout: int = 180,
        max_retries: int = 3,
        backoff: Sequence[float] = (2.0, 5.0, 10.0),
        allowlist: Sequence[Sequence[str]] = DEFAULT_ALLOWLIST,
        runner: Runner | None = None,
        inter_call_delay: float = 0.2,
        identity: str = "user",
        workdir: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._runner: Runner = runner or subprocess_runner
        self._sleep = sleep
        self.timeout = int(timeout)
        self.max_retries = int(max_retries)
        self.backoff = tuple(float(b) for b in backoff) or (2.0,)
        self.allowlist: tuple[tuple[str, ...], ...] = tuple(tuple(a) for a in allowlist)
        self.inter_call_delay = float(inter_call_delay)
        self.identity = identity
        self.workdir = os.path.abspath(workdir) if workdir else os.getcwd()
        # cli_path 延迟解析：回放模式下机器上可能根本没装 lark-cli
        self._cli_path_override = cli_path
        self._resolved_argv: tuple[str, ...] | None = None
        self.call_log: list[dict[str, Any]] = []

    @property
    def cli_argv(self) -> tuple[str, ...]:
        if self._resolved_argv is None:
            self._resolved_argv = resolve_cli_argv(self._cli_path_override)
        return self._resolved_argv

    @property
    def cli_path(self) -> str:
        """兼容旧调用方：返回 argv 的第一个元素。"""
        return self.cli_argv[0]

    # -- 安全 -------------------------------------------------------------
    def _assert_allowed(self, args: Sequence[str]) -> None:
        head = tuple(a for a in args if not a.startswith("-"))[:3]
        for allowed in self.allowlist:
            prefix = tuple(allowed)
            if head[: len(prefix)] == prefix:
                return
        raise SafetyViolation(
            f"命令不在只读白名单内，已拒绝执行：{' '.join(args[:4])}（白名单：{[list(a) for a in self.allowlist]}）"
        )

    # -- 底层调用 ---------------------------------------------------------
    def _invoke(self, args: Sequence[str], *, timeout: int | None = None,
                cwd: str | None = None, capability: str = "") -> dict[str, Any]:
        self._assert_allowed(args)
        argv = [*self.cli_argv, *args, "--format", "json"]
        if self.identity:
            argv += ["--as", self.identity]
        effective_timeout = int(timeout or self.timeout)
        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            result = self._runner(argv, effective_timeout, cwd)
            # 成功时 JSON 在 stdout；失败时 lark-cli 把 JSON 打到 stderr，两处都要看
            payload = extract_json(result.stdout) or extract_json(result.stderr)
            error_body = ((payload or {}).get("error") or {}) if payload else {}
            self.call_log.append({
                "capability": capability,
                "argv": [a for a in argv if a not in self.cli_argv],
                "returncode": result.returncode,
                "duration_ms": result.duration_ms,
                "attempt": attempt + 1,
                "ok": bool(payload and payload.get("ok")),
                "error_code": error_body.get("code"),
                "error_message": redact_secrets(str(error_body.get("message") or ""))[:200] or None,
            })
            if payload and payload.get("ok") is True:
                if self.inter_call_delay:
                    self._sleep(self.inter_call_delay)
                return dict(payload.get("data") or {})
            exc = classify_error(payload, result)
            last_exc = exc
            if not exc.retryable or attempt >= self.max_retries:
                break
            wait = self.backoff[min(attempt, len(self.backoff) - 1)]
            self._sleep(wait)

        assert last_exc is not None
        raise last_exc

    # -- SourceAdapter 实现 ----------------------------------------------
    def list_folder(self, folder_token: str, *, page_token: str | None = None,
                    page_size: int = 200, capability: str = "collection.inventory") -> ListPage:
        args = ["drive", "files", "list", "--folder-token", folder_token,
                "--page-size", str(int(page_size))]
        if page_token:
            args += ["--page-token", page_token]
        data = self._invoke(args, capability=capability)
        files = data.get("files") or []
        next_token = data.get("next_page_token") or None
        has_more = bool(data.get("has_more"))
        # 服务端有时 has_more=false 但仍给 next_page_token；以 has_more 为准，
        # 同时保留 token 以便排障。
        return ListPage(
            entries=tuple(dict(f) for f in files),
            has_more=has_more,
            next_page_token=next_token if has_more else None,
            folder_token=folder_token,
            raw=data,
        )

    def inspect(self, token_or_url: str, capability: str = "collection.inventory") -> dict[str, Any]:
        args = ["drive", "+inspect", "--url", token_or_url]
        return self._invoke(args, capability=capability)

    def metas(self, doc_refs: Sequence[Mapping[str, str]],
              capability: str = "collection.inventory") -> list[dict[str, Any]]:
        body = {
            "request_docs": [{"doc_token": r["doc_token"], "doc_type": r["doc_type"]} for r in doc_refs],
            "with_url": True,
        }
        args = ["drive", "metas", "batch_query", "--data", json.dumps(body, ensure_ascii=False)]
        data = self._invoke(args, capability=capability)
        return [dict(m) for m in (data.get("metas") or [])]

    def _rel_to_workdir(self, workdir: str, path: str) -> str:
        """把目标路径转成相对 workdir 的 POSIX 风格路径（lark-cli 的硬要求）。"""
        abs_path = path if os.path.isabs(path) else os.path.join(workdir, path)
        rel = os.path.relpath(abs_path, workdir)
        if rel.startswith(".."):
            raise PermanentAdapterError(
                f"下载目标必须位于工作目录内：workdir={workdir}, target={abs_path}"
            )
        return rel.replace(os.sep, "/")

    def download_file(self, item_token: str, dest_rel_path: str, *, workdir: str | None = None,
                      overwrite: bool = True, timeout: int | None = None,
                      capability: str = "collection.acquire") -> dict[str, Any]:
        wd = os.path.abspath(workdir or self.workdir)
        winpath.makedirs(wd)
        rel = self._rel_to_workdir(wd, dest_rel_path)
        parent = os.path.dirname(os.path.join(wd, rel))
        if parent:
            winpath.makedirs(parent)
        args = ["drive", "+download", "--file-token", item_token, "--output", rel]
        if overwrite:
            args.append("--overwrite")
        data = self._invoke(args, timeout=timeout, cwd=wd, capability=capability)
        saved = data.get("saved_path") or os.path.join(wd, rel)
        return {"saved_path": saved, "size_bytes": data.get("size_bytes"), "rel_path": rel}

    def export_online(self, item_token: str, item_type: str, dest_rel_dir: str, *,
                      workdir: str | None = None, extension: str | None = None,
                      overwrite: bool = True, timeout: int | None = None,
                      capability: str = "collection.acquire") -> dict[str, Any]:
        wd = os.path.abspath(workdir or self.workdir)
        winpath.makedirs(wd)
        rel_dir = self._rel_to_workdir(wd, dest_rel_dir)
        winpath.makedirs(os.path.join(wd, rel_dir))
        ext = (extension or "pdf").lower().lstrip(".")
        args = ["drive", "+export", "--token", item_token, "--doc-type", item_type.lower(),
                "--file-extension", ext, "--output-dir", rel_dir]
        if overwrite:
            args.append("--overwrite")
        data = self._invoke(args, timeout=timeout, cwd=wd, capability=capability)
        saved = data.get("saved_path") or data.get("file_path") or data.get("output_path")
        return {
            "saved_path": saved,
            "size_bytes": data.get("size_bytes"),
            "rel_dir": rel_dir,
            "raw": data,
        }
