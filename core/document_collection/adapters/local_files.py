"""本地目录适配器。

两个用途：
1. 单元测试 / 回放之外，提供一条完全不依赖飞书的端到端验证路径；
2. 未来若要采集本地资料库，直接换这个适配器即可，上层代码不变。

约定：folder_token 就是目录路径（绝对或相对 root）。
"""

from __future__ import annotations

import os
import shutil
from typing import Any

from .. import winpath
from .base import ListPage, PermanentAdapterError


class LocalFilesAdapter:
    """只读遍历本地目录。绝不修改、移动或删除源文件。"""

    name = "local_files"

    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = os.path.abspath(root) if root else os.getcwd()

    # -- 工具 -------------------------------------------------------------
    def _resolve(self, folder_token: str) -> str:
        path = folder_token if os.path.isabs(folder_token) else os.path.join(self.root, folder_token)
        path = os.path.abspath(path)
        if os.path.commonpath([path, self.root]) != self.root:
            raise PermanentAdapterError(f"目录越界：{path} 不在 root={self.root} 内")
        return path

    @staticmethod
    def _ext(name: str) -> str | None:
        base, ext = os.path.splitext(name)
        return ext.lower().lstrip(".") if ext else None

    # -- SourceAdapter 实现 ----------------------------------------------
    def list_folder(self, folder_token: str, *, page_token: str | None = None,
                    page_size: int = 200, capability: str = "collection.inventory") -> ListPage:
        path = self._resolve(folder_token)
        if not winpath.isdir(path):
            raise PermanentAdapterError(f"不是目录：{path}")
        entries: list[dict[str, Any]] = []
        try:
            names = sorted(winpath.listdir(path))
        except OSError as exc:
            raise PermanentAdapterError(f"无法列举目录 {path}: {exc}") from exc

        for name in names:
            full = os.path.join(path, name)
            try:
                st = os.stat(winpath.long_path(full))
            except OSError:
                continue
            is_dir = winpath.isdir(full)
            entries.append({
                "token": full,
                "name": name,
                "type": "folder" if is_dir else "file",
                "parent_token": path,
                "url": "file:///" + full.replace(os.sep, "/"),
                "created_time": str(int(st.st_ctime)),
                "modified_time": str(int(st.st_mtime)),
                "size": None if is_dir else int(st.st_size),
            })

        # 本地也实现分页，确保上层分页逻辑在两种适配器下都被真实覆盖
        start = int(page_token) if page_token and str(page_token).isdigit() else 0
        chunk = entries[start: start + max(1, int(page_size))]
        end = start + len(chunk)
        has_more = end < len(entries)
        return ListPage(
            entries=tuple(chunk),
            has_more=has_more,
            next_page_token=str(end) if has_more else None,
            folder_token=path,
            raw={"total_local_entries": len(entries)},
        )

    def inspect(self, token_or_url: str, capability: str = "collection.inventory") -> dict[str, Any]:
        path = token_or_url.replace("file:///", "") if token_or_url.startswith("file:///") else token_or_url
        path = os.path.abspath(path)
        return {
            "title": os.path.basename(path) or path,
            "token": path,
            "type": "folder" if winpath.isdir(path) else "file",
            "url": "file:///" + path.replace(os.sep, "/"),
        }

    def download_file(self, item_token: str, dest_rel_path: str, *, workdir: str | None = None,
                      overwrite: bool = True, timeout: int | None = None,
                      capability: str = "collection.acquire") -> dict[str, Any]:
        wd = os.path.abspath(workdir or os.getcwd())
        src = os.path.abspath(item_token)
        if not winpath.isfile(src):
            raise PermanentAdapterError(f"源文件不存在：{src}")
        dest = dest_rel_path if os.path.isabs(dest_rel_path) else os.path.join(wd, dest_rel_path)
        dest = os.path.abspath(dest)
        if os.path.commonpath([dest, wd]) != wd:
            raise PermanentAdapterError(f"目标越界：{dest} 不在 workdir={wd} 内")
        winpath.makedirs(os.path.dirname(dest))
        if winpath.exists(dest) and not overwrite:
            raise PermanentAdapterError(f"目标已存在且未允许覆盖：{dest}")
        # 先写临时文件再原子改名，和 lark 适配器保持一致的落盘语义
        tmp = dest + ".part"
        shutil.copy2(winpath.long_path(src), winpath.long_path(tmp))
        winpath.replace(tmp, dest)
        return {"saved_path": dest, "size_bytes": winpath.getsize(dest),
                "rel_path": os.path.relpath(dest, wd).replace(os.sep, "/")}

    def export_online(self, item_token: str, item_type: str, dest_rel_dir: str, *,
                      workdir: str | None = None, extension: str | None = None,
                      overwrite: bool = True, timeout: int | None = None,
                      capability: str = "collection.acquire") -> dict[str, Any]:
        raise PermanentAdapterError(
            f"本地适配器不支持在线资源导出（item_type={item_type}）",
            detail={"item_token": item_token, "item_type": item_type},
        )
