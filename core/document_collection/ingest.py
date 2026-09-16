"""本地导入（ingest）：直接接受用户已经下载好的文档，跳过云盘盘点/采集。

设计动机
--------
原来的默认入口是 inventory（盘点云盘）+ acquire（只读下载/导出），
需要 lark-cli 全局命令、npm、云盘授权与 refresh token。对"我手上已经有一批
下载好的 PPT/DOCX/PDF，帮我筛一遍并生题"这种最常见的工作方式来说，
那两步既没必要，又把权限面放大了。本模块把入口缩短为：

    本地文件夹 -> ingest -> extract -> deduplicate -> screen -> query/report

ingest 的职责等价于 inventory + acquire 的本地版本：
1. 递归遍历本地文件夹，逐个文件分类（复用 inventory.classify_entry）；
2. 复用 acquire 的格式闸门与大小闸门，结论口径完全一致（不拍脑袋另定一套）；
3. 计算 SHA-256 做精确去重与内容寻址；
4. 登记 item + artifact + acquire 决定，把条目状态置为 acquired，
   让后面的 extract 阶段不需要任何改动就能接上。

只读纪律照旧：**默认不复制、不移动、不修改用户源文件**（artifact 只记录原路径），
需要让 run 目录自包含时才用 copy=True 复制到内容寻址的 artifacts/ 下。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .acquisition import _extension_gate, _size_gate, artifact_path_for
from .config import CollectionPolicy
from .contracts import (
    ITEM_ACQUIRED,
    ITEM_NEEDS_REVIEW,
    ITEM_REJECTED,
    RC_SIZE_UNKNOWN_REMOTE,
)
from .inventory import classify_entry
from .state import StateStore
from .textstats import sha256_file
from . import winpath

LogFn = Callable[[str], None]


def _noop(msg: str) -> None:  # pragma: no cover
    pass


@dataclass
class IngestOptions:
    limit: int | None = None
    extensions: tuple[str, ...] = ()
    copy: bool = False            # True = 复制到内容寻址 artifacts/，默认原地引用
    recursive: bool = True
    skip_existing: bool = True
    source_name: str | None = None  # 覆盖派生出来的来源名（默认取文件夹名）
    include_hidden: bool = False


@dataclass
class IngestReport:
    root: str
    source_name: str
    total_files: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    type_counts: dict[str, int] = field(default_factory=dict)
    bytes_total: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    records: list[dict[str, Any]] = field(default_factory=list)


def _is_hidden(name: str) -> bool:
    return name.startswith(".") or name.startswith("~$")


def discover_files(root: str, *, recursive: bool = True,
                   include_hidden: bool = False) -> list[tuple[str, str]]:
    """返回 [(绝对路径, 相对 root 的 POSIX 相对路径)]，按相对路径排序保证可复现。"""
    out: list[tuple[str, str]] = []
    root = os.path.abspath(root)
    if not winpath.isdir(root):
        raise FileNotFoundError(f"不是目录：{root}")
    # os.walk 走 long_path（突破 Windows MAX_PATH），但它会把 \\?\ 前缀带进
    # 返回的 dirpath，导致 relpath 报 "path is on mount '\\\\?\\D:'"。
    # 这里统一剥掉前缀再算相对路径，落库/报告仍是普通可读形式。
    prefix = "\\\\?\\"
    if recursive:
        for dirpath, dirnames, filenames in os.walk(winpath.long_path(root)):
            if dirpath.startswith(prefix):
                dirpath = dirpath[len(prefix):]
            if not include_hidden:
                dirnames[:] = [d for d in dirnames if not _is_hidden(d)]
            for name in filenames:
                if not include_hidden and _is_hidden(name):
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                out.append((full, rel))
    else:
        for name in winpath.listdir(root):
            if not include_hidden and _is_hidden(name):
                continue
            full = os.path.join(root, name)
            if winpath.isfile(full):
                out.append((full, name))
    out.sort(key=lambda pair: pair[1])
    return out


def ingest_one(*, path: str, rel_path: str, source_name: str, root: str,
               policy: CollectionPolicy, workspace_dir: str,
               opts: IngestOptions, log: LogFn = _noop) -> dict[str, Any]:
    """登记单个本地文件。结论口径与 acquire 完全一致。"""
    name = os.path.basename(path)
    # 复用盘点阶段的分类逻辑，保证 item_type / extension 口径一致
    cls = classify_entry({"name": name, "type": "file", "token": path}, policy)
    ext = (cls.get("extension") or "").lower()
    # token 用绝对路径：本地文件没有云盘 token，路径本身就是唯一标识
    token = os.path.abspath(path)
    from .contracts import safe_item_id
    item_id = safe_item_id(token)

    result: dict[str, Any] = {
        "item_id": item_id, "item_token": token, "name": name,
        "relative_path": rel_path, "source_name": source_name,
        "source_folder_path": os.path.dirname(rel_path) or "/",
        "item_type": cls["item_type"], "extension": ext,
        "status": ITEM_ACQUIRED, "reason_codes": [], "artifact_path": None,
        "sha256": None, "size_bytes": None, "media_type": None,
        "acquire_mode": "ingest_in_place" if not opts.copy else "ingest_copy",
        "error": None, "source_root": os.path.abspath(root),
    }

    # 1) 格式闸门（与 acquire 同一函数，不另写一套业务判断）
    gate_status, gate_reasons = _extension_gate(
        {"item_type": cls["item_type"], "extension": ext}, policy)
    result["reason_codes"].extend(gate_reasons)
    if gate_status:
        result["status"] = gate_status
        return result

    # 2) 大小闸门（本地文件一定有 size，直接判，不需要下载后复核）
    try:
        size_bytes = winpath.getsize(token)
    except OSError as exc:
        result["status"] = ITEM_NEEDS_REVIEW
        result["reason_codes"] = ["stat_failed", RC_SIZE_UNKNOWN_REMOTE]
        result["error"] = f"{type(exc).__name__}: {exc}"[:400]
        return result
    ok, size_reasons = _size_gate(size_bytes, ext, policy, online=False)
    result["reason_codes"].extend(size_reasons)
    result["size_bytes"] = size_bytes
    if not ok:
        result["status"] = ITEM_REJECTED
        return result

    # 3) 哈希（去重 + 内容寻址）
    result["sha256"] = sha256_file(token)

    # 4) 落定 artifact 路径：默认原地引用（源文件只读），copy=True 才复制
    if opts.copy:
        final_path = artifact_path_for(workspace_dir, result["sha256"], ext)
        if not winpath.isfile(final_path):
            winpath.makedirs(os.path.dirname(final_path))
            tmp = final_path + ".part"
            import shutil
            shutil.copy2(winpath.long_path(token), winpath.long_path(tmp))
            winpath.replace(tmp, final_path)
        result["artifact_path"] = final_path
        if os.path.abspath(final_path) != os.path.abspath(token):
            result["reason_codes"].append("copied_to_content_store")
    else:
        result["artifact_path"] = token
        result["reason_codes"].append("referenced_in_place")

    result["status"] = ITEM_ACQUIRED
    return result


def run_ingest(*, policy: CollectionPolicy, store: StateStore, workspace_dir: str,
               run_id: str, root: str, options: IngestOptions | None = None,
               log: LogFn = _noop) -> dict[str, Any]:
    """批量导入一个本地文件夹，写 ingest_report.jsonl 并落库。"""
    opts = options or IngestOptions()
    winpath.makedirs(workspace_dir)
    root = os.path.abspath(root)
    source_name = opts.source_name or os.path.basename(root.rstrip("\\/")) or root
    files = discover_files(root, recursive=opts.recursive, include_hidden=opts.include_hidden)
    if opts.extensions:
        wanted = {e.lower().lstrip(".") for e in opts.extensions}
        files = [(p, r) for (p, r) in files if os.path.splitext(r)[1].lower().lstrip(".") in wanted]
    report = IngestReport(root=root, source_name=source_name, total_files=len(files))
    if opts.limit:
        files = files[: int(opts.limit)]
    log(f"[ingest] 待导入 {len(files)} 个本地文件（源：{root}）")

    report_path = os.path.join(workspace_dir, "ingest_report.jsonl")
    counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    started = time.time()

    with winpath.open_write(report_path, "a", encoding="utf-8") as fh:
        for idx, (path, rel_path) in enumerate(files, start=1):
            try:
                rec = ingest_one(path=path, rel_path=rel_path, source_name=source_name,
                                 root=root, policy=policy, workspace_dir=workspace_dir,
                                 opts=opts, log=log)
            except Exception as exc:  # noqa: BLE001 - 单个文件出错不能拖垮整批
                rec = {"item_id": None, "name": os.path.basename(path),
                       "relative_path": rel_path, "source_name": source_name,
                       "status": "ingest_failed", "reason_codes": ["ingest_exception"],
                       "error": f"{type(exc).__name__}: {exc}"[:400]}
                report.errors.append({"path": path, "message": rec["error"]})
            rec["run_id"] = run_id
            rec["ingested_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            report.records.append(rec)
            counts[rec["status"]] = counts.get(rec["status"], 0) + 1
            type_counts[rec.get("extension") or "<none>"] = \
                type_counts.get(rec.get("extension") or "<none>", 0) + 1
            if rec.get("size_bytes"):
                report.bytes_total += int(rec["size_bytes"])

            if rec.get("item_id"):
                _persist(store, rec, source_name, root, run_id)
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            if idx % 50 == 0 or idx == len(files):
                store.commit()
                log(f"[ingest] {idx}/{len(files)} 状态分布 {counts}")

    store.commit()
    report.status_counts = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
    report.type_counts = dict(sorted(type_counts.items(), key=lambda kv: (-kv[1], kv[0])))
    summary = {
        "root": root,
        "source_name": source_name,
        "total_files": len(files),
        "status_counts": report.status_counts,
        "type_counts": report.type_counts,
        "bytes_total": report.bytes_total,
        "copy_mode": opts.copy,
        "error_count": len(report.errors),
        "elapsed_sec": round(time.time() - started, 2),
        "report_path": report_path,
    }
    store.touch_run(run_id, {"collection.ingest": summary})
    log(f"[ingest] 完成：{summary['status_counts']}")
    return summary


def _persist(store: StateStore, rec: Mapping[str, Any], source_name: str,
             root: str, run_id: str) -> None:
    """把一条 ingest 记录写进 items / artifacts / decisions。"""
    store.upsert_item({
        "item_id": rec["item_id"],
        "item_token": rec["item_token"],
        "name": rec["name"],
        "source_name": source_name,
        "source_folder_path": rec.get("source_folder_path") or "/",
        "item_type": rec.get("item_type") or "local_file",
        "extension": rec.get("extension"),
        "remote_size": rec.get("size_bytes"),
        "url": "file:///" + str(rec["item_token"]).replace(os.sep, "/"),
        "relative_path": rec.get("relative_path"),
        "status": rec["status"],
        "stage": "ingest",
        "metadata": {
            "relative_path": rec.get("relative_path"),
            "ingest_source_root": root,
            "ingest_mode": rec.get("acquire_mode"),
            "display_bucket": rec.get("extension"),
        },
        "run_id": run_id,
    })
    store.upsert_artifact(
        rec["item_id"], sha256=rec.get("sha256"), path=rec.get("artifact_path"),
        size_bytes=rec.get("size_bytes"), media_type=rec.get("media_type"),
        acquire_mode=rec.get("acquire_mode"), attempts=1, status=rec["status"],
        error=rec.get("error"), acquired_at=rec.get("ingested_at"),
    )
    store.add_decision(rec["item_id"], "acquire", rec["status"],
                       rec.get("reason_codes") or [], {
                           "size_bytes": rec.get("size_bytes"),
                           "sha256": rec.get("sha256"),
                           "artifact_path": rec.get("artifact_path"),
                           "acquire_mode": rec.get("acquire_mode"),
                           "extension": rec.get("extension"),
                           "ingest": True,
                       })
    store.set_item_stage(rec["item_id"], rec["status"], "ingest")


def discover_supported_extensions(files: Sequence[tuple[str, str]]) -> dict[str, int]:
    """小工具：给 CLI 打印一份"这批文件都长什么样"的类型分布。"""
    counts: dict[str, int] = {}
    for _path, rel in files:
        ext = (os.path.splitext(rel)[1].lower().lstrip(".")) or "<none>"
        counts[ext] = counts.get(ext, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
