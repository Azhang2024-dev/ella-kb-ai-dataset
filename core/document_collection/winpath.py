"""Windows 长路径处理。

为什么需要这个模块：
Windows 传统 API 有 MAX_PATH=260 的限制。本工具的产物路径是

    <repo>\\output\\document_collection\\<run_id>\\artifacts\\<sha256[:2]>\\<sha256>.<ext>

光是文件名就占 64+1+3 ≈ 68 个字符，仓库路径稍深一点（或者 pytest 的
临时目录名很长）就会突破 260，报 WinError 3「系统找不到指定的路径」。
这个坑在本项目已经出现过两次（item_id 派生名、artifact 落盘名）。

解决办法是给绝对路径加 `\\\\?\\` 前缀，走 Windows 的扩展长度路径 API，
上限提升到约 32767。只在真正调用 OS 的地方加前缀，
写进数据库和报告的路径仍然是普通可读形式。
"""

from __future__ import annotations

import os

_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"


def long_path(path: str | os.PathLike[str]) -> str:
    """返回可以直接传给 OS 的路径：非 Windows 原样返回，Windows 加扩展长度前缀。"""
    text = os.fspath(path)
    if not text:
        return text
    if os.name != "nt":
        return text
    absolute = os.path.abspath(text)
    if absolute.startswith(_PREFIX):
        return absolute
    if absolute.startswith("\\\\"):
        # UNC 路径：\\server\share -> \\?\UNC\server\share
        return _UNC_PREFIX + absolute[2:]
    return _PREFIX + absolute


def makedirs(path: str | os.PathLike[str], exist_ok: bool = True) -> None:
    os.makedirs(long_path(path), exist_ok=exist_ok)


def exists(path: str | os.PathLike[str]) -> bool:
    return os.path.exists(long_path(path))


def isfile(path: str | os.PathLike[str]) -> bool:
    return os.path.isfile(long_path(path))


def getsize(path: str | os.PathLike[str]) -> int:
    return os.path.getsize(long_path(path))


def remove(path: str | os.PathLike[str]) -> None:
    os.remove(long_path(path))


def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    os.replace(long_path(src), long_path(dst))


def move(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """跨目录移动。先尝试 os.replace（同卷时是原子操作），失败再退回 copy+remove。"""
    src_long, dst_long = long_path(src), long_path(dst)
    try:
        os.replace(src_long, dst_long)
        return
    except OSError:
        pass
    import shutil

    shutil.copy2(src_long, dst_long)
    os.remove(src_long)


def isdir(path: str | os.PathLike[str]) -> bool:
    return os.path.isdir(long_path(path))


def listdir(path: str | os.PathLike[str]) -> list[str]:
    return os.listdir(long_path(path))


def open_read(path: str | os.PathLike[str], mode: str = "r", **kwargs):
    return open(long_path(path), mode, **kwargs)


def open_write(path: str | os.PathLike[str], mode: str = "w", **kwargs):
    return open(long_path(path), mode, **kwargs)
