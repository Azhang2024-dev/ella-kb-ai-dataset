"""适配器基类与错误分类。

数据源适配器是唯一与外部世界（飞书云盘 / 本地目录 / 未来的其他网盘）接触的层。
上层（inventory / acquisition）只依赖本文件的抽象，因此换数据源时只改适配器。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


class AdapterError(RuntimeError):
    """适配器调用失败的基类。"""

    def __init__(self, message: str, *, retryable: bool = False, detail: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.detail = dict(detail or {})


class TransientAdapterError(AdapterError):
    """可重试：429 限流、5xx、网络超时。"""

    def __init__(self, message: str, detail: Mapping[str, Any] | None = None):
        super().__init__(message, retryable=True, detail=detail)


class PermanentAdapterError(AdapterError):
    """不可重试：参数错误、权限不足、资源不存在。"""

    def __init__(self, message: str, detail: Mapping[str, Any] | None = None):
        super().__init__(message, retryable=False, detail=detail)


class SafetyViolation(AdapterError):
    """试图执行只读白名单之外的命令。这是硬约束，任何情况都不能放行。"""


@dataclass(frozen=True)
class CommandResult:
    """一次外部命令调用的原始结果（用于录制/回放和排障）。"""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    cwd: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "cwd": self.cwd,
        }


@dataclass(frozen=True)
class ListPage:
    """一页目录清单。"""

    entries: tuple[Mapping[str, Any], ...]
    has_more: bool
    next_page_token: str | None
    folder_token: str
    page_index: int = 0
    raw: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "folder_token": self.folder_token,
            "page_index": self.page_index,
            "has_more": self.has_more,
            "next_page_token": self.next_page_token,
            "entries": [dict(e) for e in self.entries],
        }


@runtime_checkable
class SourceAdapter(Protocol):
    """数据源适配器协议。

    所有方法都必须是只读的：列举、查元数据、下载、导出。
    任何写回云盘的能力都不在协议里，从接口层面杜绝误操作。
    """

    name: str

    def list_folder(self, folder_token: str, *, page_token: str | None = None,
                    page_size: int = 200) -> ListPage:
        """列举一层目录，返回一页。"""

    def inspect(self, token_or_url: str) -> dict[str, Any]:
        """解析 URL/token，返回类型与标题（用于写清源文件夹名称）。"""

    def download_file(self, item_token: str, dest_rel_path: str, *,
                      workdir: str, overwrite: bool = False) -> dict[str, Any]:
        """下载普通文件到 workdir 下的相对路径，返回 {saved_path, size_bytes}。"""

    def export_online(self, item_token: str, item_type: str, dest_rel_dir: str, *,
                      workdir: str, extension: str | None = None,
                      overwrite: bool = False) -> dict[str, Any]:
        """导出在线资源（docx/sheet/slides/bitable/wiki）到本地。"""
