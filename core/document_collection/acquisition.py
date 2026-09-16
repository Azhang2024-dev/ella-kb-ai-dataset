"""Task 3：只读采集（acquire）。

职责：把盘点到的资源取到本地，并留下可复核的证据。

关键设计决定
------------
1. **内容寻址存储**：最终文件落在 `artifacts/<sha256[:2]>/<sha256>.<ext>`。
   这样"两个源文件夹里同一份文件"天然只占一份磁盘，也为后续正文去重打底。
2. **临时文件 + 原子改名**：先写 `tmp/<token>.part`，校验成功后 `os.replace`。
   中断不会留下半成品被误当成完整文件（计划 Task 3 的禁止项）。
3. **大小在下载后判定**：飞书 list/metas 都不返回 size（Task 0 实测），
   所以 remote_size 为 None 时用本地字节数判门槛，超限直接 reject 并删除 blob，
   不通过压缩/拆分/截取规避（需求 2.1）。
4. **在线资源导出**：docx/sheet/slides/bitable 走 `drive +export`；
   导出失败标 `pending_export`，不伪装成功。
5. **旧 Office 格式**（doc/ppt/xls）本期无批准转换器 -> `conversion_required`。
6. 只读：适配器层白名单已经拦截写操作，本模块不做任何云盘写动作。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .config import CollectionPolicy
from .contracts import (
    ITEM_ACQUIRED,
    ITEM_ACQUIRE_FAILED,
    ITEM_CONVERSION_REQUIRED,
    ITEM_NEEDS_REVIEW,
    ITEM_PENDING_EXPORT,
    ITEM_REJECTED,
    RC_ACQUIRE_FAILED,
    RC_CONVERSION_REQUIRED,
    RC_EXT_SUPPORTED,
    RC_EXT_UNSUPPORTED,
    RC_PENDING_EXPORT,
    RC_SIZE_OVER_LIMIT,
    RC_SIZE_UNKNOWN_REMOTE,
    RC_SIZE_WITHIN_LIMIT,
)
from .inventory import ONLINE_DOC, ONLINE_SHEET, ONLINE_SLIDES, SHORTCUT, UNKNOWN
from .state import StateStore
from .textstats import sha256_file
from . import winpath

LogFn = Callable[[str], None]


def _noop(msg: str) -> None:  # pragma: no cover
    pass


# 可用的分层维度。Pilot 阶段用它保证样本覆盖不同来源、目录、格式，
# 而不是按路径字典序一头扎进同一个子文件夹。
SAMPLE_STRATA = ("source_name", "top_folder", "extension", "item_type")
DEFAULT_SAMPLE_STRATA = ("source_name", "top_folder", "extension")
DEFAULT_SAMPLE_SEED = 20260909


@dataclass
class AcquireOptions:
    limit: int | None = None
    extensions: tuple[str, ...] = ()
    item_types: tuple[str, ...] = ()
    source_names: tuple[str, ...] = ()
    # 定向过滤：只看这些顶层目录 / 这些语种对应的目录（下载前只能靠目录名判语种）。
    # 没有它就只能"先 inventory --include-subfolder 收窄、再整批 acquire"两步走，
    # 收窄不彻底还会把非目标语种一起下载。
    top_folders: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    include_statuses: tuple[str, ...] = ("discovered", "acquire_failed", "pending_export")
    skip_existing: bool = True
    dry_run: bool = False
    keep_oversized_artifact: bool = False
    export_extension_override: dict[str, str] | None = None
    max_retries: int = 3
    export_timeout_sec: int = 300
    # 分层抽样（Pilot 用）：limit 是"取前 N 条"，sample 是"跨层均匀取 N 条"
    sample: int | None = None
    sample_strata: tuple[str, ...] = DEFAULT_SAMPLE_STRATA
    sample_seed: int = DEFAULT_SAMPLE_SEED


def _safe_ext(ext: str | None) -> str:
    if not ext:
        return ""
    e = str(ext).lower().lstrip(".")
    return e if e.isalnum() and len(e) <= 8 else ""


def artifact_path_for(workspace_dir: str, sha256: str, ext: str | None) -> str:
    suffix = f".{_safe_ext(ext)}" if _safe_ext(ext) else ""
    return os.path.join(workspace_dir, "artifacts", sha256[:2], f"{sha256}{suffix}")


def _stable_hash(text: str) -> int:
    """跨进程稳定的短哈希（Python 内置 hash 每进程随机化，不能用于可复现抽样）。"""
    import hashlib

    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def _dimension_value(item: Mapping[str, Any], dimension: str) -> str:
    meta = item.get("metadata") or {}
    rel = str(meta.get("relative_path") or item.get("relative_path") or item.get("name") or "")
    if dimension == "source_name":
        return str(item.get("source_name") or "?")
    if dimension == "top_folder":
        return rel.split("/")[0] if "/" in rel else "<root>"
    if dimension == "extension":
        return str(item.get("extension") or "<none>").lower()
    if dimension == "item_type":
        return str(item.get("item_type") or "?")
    return "?"


def stratum_key(item: Mapping[str, Any], strata: Sequence[str]) -> tuple[str, ...]:
    """按给定维度算出条目所属的层。"""
    return tuple(_dimension_value(item, dimension) for dimension in strata)


def _allocate_quota(total: int, group_sizes: Mapping[str, int]) -> dict[str, int]:
    """把名额在各组之间轮转均分；组内不够时自动把缺口补给其他组。

    轮转而不是按比例：真实目录大小极度不均（知识库_2026 有 3818 条，
    docQA.llm 只有 1074 条），按比例分会让样本一边倒，
    而计划明确要求 Pilot「两个源各取 30 份」。
    """
    names = sorted(group_sizes)
    alloc = {n: 0 for n in names}
    remaining = {n: int(group_sizes[n]) for n in names}
    left = int(total)
    while left > 0:
        progressed = False
        for name in names:
            if left <= 0:
                break
            if remaining[name] > 0:
                alloc[name] += 1
                remaining[name] -= 1
                left -= 1
                progressed = True
        if not progressed:
            break
    return alloc


def _coverage_sample(items: Sequence[Mapping[str, Any]], total: int,
                     strata: Sequence[str], seed: int) -> list[dict[str, Any]]:
    """在一组内部做「贪心覆盖 + 随机补齐」。

    贪心阶段：每次挑一条能覆盖最多"尚未出现过的层取值"的条目，
    这样稀有格式、稀有目录会在前几条就被抽到，不会等到名额用完。
    补齐阶段：所有取值都覆盖过之后，按带种子的随机顺序补足名额。

    两个阶段都只依赖 (排序后的候选 + 种子)，所以同一批输入结果完全一致。
    """
    import random

    if total <= 0 or not items:
        return []
    pool = sorted((dict(i) for i in items), key=lambda r: str(r.get("item_id")))
    rng = random.Random(seed)
    order = list(range(len(pool)))
    rng.shuffle(order)

    if not strata:
        return [pool[i] for i in order[:total]]

    keys = [stratum_key(item, strata) for item in pool]
    covered: list[set] = [set() for _ in strata]
    covered_combos: set[tuple[str, ...]] = set()
    picked: list[dict[str, Any]] = []
    picked_positions: set[int] = set()
    # 组合新颖度权重必须大于"最多能新增的维度取值数"，
    # 否则所有维度取值都被覆盖过之后，贪心会提前停止，组合覆盖不全。
    combo_weight = len(strata) + 1
    max_gain = combo_weight + len(strata)

    def gain_of(position: int) -> int:
        key = keys[position]
        gain = combo_weight if key not in covered_combos else 0
        gain += sum(1 for dim, value in enumerate(key) if value not in covered[dim])
        return gain

    while len(picked) < total:
        best: int | None = None
        best_gain = 0
        for position in order:
            if position in picked_positions:
                continue
            gain = gain_of(position)
            if gain > best_gain:
                best, best_gain = position, gain
                if gain >= max_gain:
                    break        # 不可能更高了，提前退出省时间
        if best is None or best_gain == 0:
            break
        picked.append(pool[best])
        picked_positions.add(best)
        covered_combos.add(keys[best])
        for dim, value in enumerate(keys[best]):
            covered[dim].add(value)

    if len(picked) < total:
        for position in order:
            if len(picked) >= total:
                break
            if position not in picked_positions:
                picked.append(pool[position])
                picked_positions.add(position)
    return picked[:total]


def stratified_sample(items: Sequence[Mapping[str, Any]], total: int,
                      strata: Sequence[str] = DEFAULT_SAMPLE_STRATA,
                      seed: int = DEFAULT_SAMPLE_SEED) -> list[dict[str, Any]]:
    """两级分层抽样：**组间均衡 + 组内最大覆盖**，结果完全可复现。

    为什么是两级：
    - 第一维（默认 `source_name`）要求**均衡**。计划明确写了 Pilot
      「知识库_2026 取 30 份、docQA.llm 取 30 份」，不能一边倒。
    - 其余维度（`top_folder` / `extension`）要求**覆盖**，
      稀有格式和稀有目录必须被抽到，否则 Pilot 测不出这些路径的问题。

    实测踩过的两个坑（都有回归用例）：
    1. 最初按完整层名排序后轮转，`docQA.llm`（ASCII）的所有层都排在
       `知识库_2026`（中文）之前，抽 80 条结果一条都没抽到第一个来源；
    2. 只按目录轮转时，稀有扩展名（csv/pptx）排不上号，样本全是 pdf。

    递归实现：第一维均分名额，每个组内用剩余维度递归做覆盖抽样。
    """
    if total <= 0 or not items:
        return []
    if not strata:
        return _coverage_sample(items, total, (), seed)

    head, rest = strata[0], tuple(strata[1:])
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(_dimension_value(item, head), []).append(dict(item))

    quota = _allocate_quota(total, {name: len(members) for name, members in groups.items()})
    per_group = [
        _coverage_sample(groups[name], quota[name], rest,
                         seed=(seed + _stable_hash(name)) & 0x7FFFFFFF)
        for name in sorted(groups)
    ]
    # 交错合并：如果直接首尾相接，`--sample 80 --limit 10` 会只拿到第一个来源，
    # 均衡就在后续 limit 里被破坏掉了。
    picked: list[dict[str, Any]] = []
    cursors = [0] * len(per_group)
    while len(picked) < total:
        progressed = False
        for index, bucket in enumerate(per_group):
            if len(picked) >= total:
                break
            if cursors[index] < len(bucket):
                picked.append(bucket[cursors[index]])
                cursors[index] += 1
                progressed = True
        if not progressed:
            break
    return picked[:total]


def _top_folder_of(item: Mapping[str, Any]) -> str:
    meta = item.get("metadata") or {}
    rel = str(meta.get("relative_path") or item.get("relative_path") or item.get("name") or "")
    return rel.split("/")[0] if "/" in rel else "<root>"


def language_folder_names(policy: CollectionPolicy, language: str) -> set[str]:
    """某个 P0 语种对应的顶层目录名集合（大小写不敏感），来自策略 folder_aliases。"""
    aliases = (policy.languages.get("folder_aliases") or {})
    names = aliases.get(str(language).lower()) or []
    return {str(n).lower() for n in names}


def select_items(store: StateStore, opts: AcquireOptions,
                 policy: CollectionPolicy | None = None) -> list[dict[str, Any]]:
    items = store.list_items(
        statuses=list(opts.include_statuses) or None,
        item_types=list(opts.item_types) or None,
        source_names=list(opts.source_names) or None,
    )
    if opts.extensions:
        wanted = {e.lower().lstrip(".") for e in opts.extensions}
        items = [i for i in items if (i.get("extension") or "").lower() in wanted]
    if opts.top_folders:
        wanted = {str(f).lower() for f in opts.top_folders}
        items = [i for i in items if _top_folder_of(i).lower() in wanted]
    if opts.languages:
        if policy is None:
            raise ValueError("按语种过滤需要传入 policy（目录别名来自 languages.folder_aliases）")
        allowed: set[str] = set()
        for lang in opts.languages:
            allowed |= language_folder_names(policy, lang)
        items = [i for i in items if _top_folder_of(i).lower() in allowed]
    if opts.sample:
        items = stratified_sample(items, int(opts.sample), opts.sample_strata, opts.sample_seed)
    if opts.limit:
        items = items[: int(opts.limit)]
    return items


def _extension_gate(item: Mapping[str, Any], policy: CollectionPolicy) -> tuple[str, list[str]]:
    """下载前的格式闸门：不支持的格式不浪费带宽。"""
    itype = item.get("item_type")
    ext = (item.get("extension") or "").lower()
    allowed = set(policy.allowed_extensions)
    conversion = set(policy.conversion_required_extensions)
    excluded = set(policy.excluded_extensions)

    if itype == SHORTCUT:
        return ITEM_REJECTED, ["shortcut_not_acquirable"]
    if itype == UNKNOWN:
        return ITEM_NEEDS_REVIEW, ["unknown_item_type"]
    if not ext:
        return ITEM_NEEDS_REVIEW, ["extension_missing"]
    if ext in conversion:
        return ITEM_CONVERSION_REQUIRED, [RC_CONVERSION_REQUIRED]
    if ext in excluded:
        return ITEM_REJECTED, [RC_EXT_UNSUPPORTED]
    if ext not in allowed:
        return ITEM_REJECTED, [RC_EXT_UNSUPPORTED]
    return "", [RC_EXT_SUPPORTED]


def _size_gate(size_bytes: int | None, ext: str | None, policy: CollectionPolicy,
               online: bool) -> tuple[bool, list[str]]:
    """大小门槛。返回 (是否通过, reason_codes)。"""
    reasons: list[str] = []
    limit_mb = policy.size_limit_mb(ext)
    if limit_mb is None:
        reasons.append("size_limit_not_configured")
        return False, reasons
    if size_bytes is None:
        reasons.append(RC_SIZE_UNKNOWN_REMOTE)
        # 在线资源在导出前无法知道大小 -> 待核验，不是拒绝
        return online, reasons
    if size_bytes > limit_mb * 1024 * 1024:
        reasons.append(RC_SIZE_OVER_LIMIT)
        return False, reasons
    reasons.append(RC_SIZE_WITHIN_LIMIT)
    return True, reasons


def acquire_one(*, adapter: Any, item: Mapping[str, Any], policy: CollectionPolicy,
                workspace_dir: str, opts: AcquireOptions, log: LogFn = _noop) -> dict[str, Any]:
    """采集单个资源，返回决定记录。"""
    item_id = str(item.get("item_id") or item.get("item_token"))
    token = str(item["item_token"])
    itype = str(item.get("item_type") or "")
    ext = (item.get("extension") or "").lower()
    name = str(item.get("name") or token)
    rel = str((item.get("metadata") or {}).get("relative_path") or item.get("relative_path") or name)
    online = itype in (ONLINE_DOC, ONLINE_SHEET, ONLINE_SLIDES)

    result: dict[str, Any] = {
        "item_id": item_id, "item_token": token, "name": name,
        "relative_path": rel, "item_type": itype, "extension": ext,
        "online": online, "status": ITEM_ACQUIRED, "reason_codes": [],
        "artifact_path": None, "sha256": None, "size_bytes": None,
        "media_type": None, "acquire_mode": None, "attempts": 0,
        "error": None, "error_code": None, "error_type": None,
        "error_log_id": None, "retryable": False,
        "declared_size_verified": False,
        "source_name": item.get("source_name"),
    }

    # 1) 格式闸门
    gate_status, gate_reasons = _extension_gate(item, policy)
    result["reason_codes"].extend(gate_reasons)
    if gate_status:
        result["status"] = gate_status
        return result

    # 2) 下载前的大小预检：飞书不返回 size，只有本地适配器或未来数据源会有 remote_size
    remote_size = item.get("remote_size")
    if remote_size is not None:
        ok, size_reasons = _size_gate(remote_size, ext, policy, online=False)
        result["reason_codes"].extend(size_reasons)
        if not ok:
            result["status"] = ITEM_REJECTED
            result["size_bytes"] = remote_size
            return result
    else:
        result["reason_codes"].append(RC_SIZE_UNKNOWN_REMOTE)

    if opts.dry_run:
        result["status"] = ITEM_NEEDS_REVIEW
        result["reason_codes"].append("dry_run_not_acquired")
        result["acquire_mode"] = "dry_run"
        return result

    # 3) 已存在则跳过（续跑）
    existing = None
    if opts.skip_existing:
        existing = _existing_artifact(workspace_dir, item_id)
    if existing:
        result.update(existing)
        result["status"] = ITEM_ACQUIRED
        result["reason_codes"].append("resumed_existing_artifact")
        return result

    tmp_dir = os.path.join(workspace_dir, "tmp")
    winpath.makedirs(tmp_dir)

    # 4) 真实取文件
    mode = "export" if online else "download"
    try:
        if online:
            export_map = dict(policy.online_type_export)
            if opts.export_extension_override:
                export_map.update({k.lower(): v.lower() for k, v in opts.export_extension_override.items()})
            subtype = str((item.get("metadata") or {}).get("online_subtype") or "").lower()
            target_ext = export_map.get(subtype) or export_map.get(itype) or ext or "pdf"
            rel_dir = os.path.join("tmp", "export", _safe_ext(target_ext) or "bin")
            payload = adapter.export_online(
                token, subtype or _lark_doc_type(itype), rel_dir,
                workdir=workspace_dir, extension=target_ext,
                overwrite=True, timeout=opts.export_timeout_sec,
            )
            saved = payload.get("saved_path")
            if not saved or not winpath.isfile(saved):
                # 导出接口成功但没落盘 -> 待导出，不算失败也不算成功
                result["status"] = ITEM_PENDING_EXPORT
                result["reason_codes"] = [RC_PENDING_EXPORT, "export_reported_ok_but_no_file"]
                result["acquire_mode"] = mode
                result["error"] = json.dumps(payload.get("raw") or {}, ensure_ascii=False)[:500]
                return result
            result["media_type"] = (payload.get("raw") or {}).get("content_type")
            ext = _safe_ext(target_ext) or ext
            result["extension"] = ext
        else:
            tmp_rel = os.path.join("tmp", f"{_safe_token(token)}.{_safe_ext(ext) or 'bin'}.part")
            payload = adapter.download_file(token, tmp_rel, workdir=workspace_dir, overwrite=True)
            saved = payload.get("saved_path")
            if not saved or not winpath.isfile(saved):
                raise FileNotFoundError(f"下载声称成功但文件不存在：{saved}")
        result["acquire_mode"] = mode
        result["attempts"] = 1
    except Exception as exc:  # noqa: BLE001 - 采集必须逐条容错
        retryable = bool(getattr(exc, "retryable", False))
        detail = dict(getattr(exc, "detail", {}) or {})
        if online:
            result["status"] = ITEM_PENDING_EXPORT
            result["reason_codes"] = [RC_PENDING_EXPORT]
        else:
            result["status"] = ITEM_ACQUIRE_FAILED
            result["reason_codes"] = [RC_ACQUIRE_FAILED]
        # 存结构化的错误码与消息，而不是整段 JSON：
        # 报告要能一眼看出"是权限问题还是服务端抖动"，这决定了要不要重试。
        result["error"] = (detail.get("message") or f"{type(exc).__name__}: {exc}")[:400]
        result["error_code"] = detail.get("code")
        result["error_type"] = detail.get("type")
        result["error_log_id"] = detail.get("log_id")
        result["retryable"] = retryable
        if retryable:
            result["reason_codes"].append("retryable_error_exhausted")
        log(f"  [WARN] {mode} 失败 {rel}: code={result['error_code']} {result['error'][:110]}")
        return result

    # 5) 完整性校验 + 计算哈希 + 大小门槛复核
    try:
        size_bytes = winpath.getsize(saved)
    except OSError as exc:
        result["status"] = ITEM_ACQUIRE_FAILED
        result["reason_codes"] = ["stat_failed_after_transfer"]
        result["error"] = f"{type(exc).__name__}: {exc}"[:400]
        return result

    # 5a) 服务端声明的字节数与落盘字节数必须一致，否则说明传输被截断。
    #     飞书的普通文件下载不返回 size（清单接口也没有），只有 export 会返回，
    #     所以这里只在拿得到声明值时才校验；拿不到就如实标注，不假装校验过。
    declared = payload.get("size_bytes")
    declared_int = None
    if declared is not None:
        try:
            declared_int = int(declared)
        except (TypeError, ValueError):
            declared_int = None
    if declared_int is not None and declared_int != size_bytes:
        result["status"] = ITEM_ACQUIRE_FAILED
        result["reason_codes"] = ["truncated_transfer"]
        result["size_bytes"] = size_bytes
        result["error"] = f"服务端声明 {declared_int} 字节，落盘只有 {size_bytes} 字节"
        _silent_remove(saved)
        log(f"  [WARN] {mode} 传输截断 {rel}: {result['error']}")
        return result
    result["declared_size_verified"] = declared_int is not None
    if declared_int is None:
        result["reason_codes"].append("declared_size_unavailable")

    digest = sha256_file(saved)
    result["size_bytes"] = size_bytes
    result["sha256"] = digest

    ok, size_reasons = _size_gate(size_bytes, ext, policy, online=False)
    result["reason_codes"] = [r for r in result["reason_codes"] if r != RC_SIZE_UNKNOWN_REMOTE]
    result["reason_codes"].extend(size_reasons)
    if not ok:
        result["status"] = ITEM_REJECTED
        if not opts.keep_oversized_artifact:
            _silent_remove(saved)
            result["artifact_path"] = None
            result["reason_codes"].append("oversized_blob_deleted")
        else:
            result["artifact_path"] = saved
        return result

    # 6) 原子落盘到内容寻址位置
    final_path = artifact_path_for(workspace_dir, digest, ext)
    winpath.makedirs(os.path.dirname(final_path))
    if os.path.abspath(saved) != os.path.abspath(final_path):
        if winpath.exists(final_path):
            _silent_remove(saved)
            result["reason_codes"].append("content_already_stored")
        else:
            # 先移到 .part 再原子改名：中断不会留下被误当成完整文件的半成品
            tmp_final = final_path + ".part"
            winpath.move(saved, tmp_final)
            winpath.replace(tmp_final, final_path)
    # 5b) 移动完成后重算一次哈希。移动过程本身也可能出错（磁盘满、文件被占用、
    #     杀毒软件拦截），只在临时目录算过哈希不足以保证最终产物是对的。
    try:
        verify = sha256_file(final_path)
    except OSError as exc:
        result["status"] = ITEM_ACQUIRE_FAILED
        result["reason_codes"] = ["artifact_unreadable_after_store"]
        result["error"] = f"{type(exc).__name__}: {exc}"[:400]
        return result
    if verify != digest:
        result["status"] = ITEM_ACQUIRE_FAILED
        result["reason_codes"] = ["checksum_mismatch_after_store"]
        result["error"] = f"落盘后哈希不一致：{digest[:16]} -> {verify[:16]}"
        _silent_remove(final_path)
        return result

    result["artifact_path"] = final_path
    result["status"] = ITEM_ACQUIRED
    return result


def _safe_token(token: str) -> str:
    """派生临时文件/sidecar 名。上限 64，理由同 extraction._safe_name。"""
    return "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in token)[:64]


def _lark_doc_type(item_type: str) -> str:
    return {ONLINE_DOC: "docx", ONLINE_SHEET: "sheet", ONLINE_SLIDES: "slides"}.get(item_type, "docx")


def _silent_remove(path: str) -> None:
    try:
        if path and winpath.isfile(path):
            winpath.remove(path)
    except OSError:
        pass


def _existing_artifact(workspace_dir: str, item_id: str) -> dict[str, Any] | None:
    """续跑：从 sidecar 元数据里找回已采集结果。"""
    meta_path = os.path.join(workspace_dir, "artifacts_meta", f"{_safe_token(item_id)}.json")
    if not winpath.isfile(meta_path):
        return None
    try:
        with winpath.open_read(meta_path, encoding="utf-8-sig") as fh:
            meta = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    path = meta.get("artifact_path")
    if not path or not winpath.isfile(path):
        return None
    return {
        "artifact_path": path,
        "sha256": meta.get("sha256"),
        "size_bytes": meta.get("size_bytes"),
        "media_type": meta.get("media_type"),
        "acquire_mode": meta.get("acquire_mode"),
        "attempts": int(meta.get("attempts", 1)),
    }


def _write_artifact_meta(workspace_dir: str, item_id: str, rec: Mapping[str, Any]) -> str:
    meta_dir = os.path.join(workspace_dir, "artifacts_meta")
    winpath.makedirs(meta_dir)
    meta_path = os.path.join(meta_dir, f"{_safe_token(item_id)}.json")
    payload = {
        "item_id": item_id,
        "item_token": rec.get("item_token"),
        "sha256": rec.get("sha256"),
        "artifact_path": rec.get("artifact_path"),
        "size_bytes": rec.get("size_bytes"),
        "media_type": rec.get("media_type"),
        "acquire_mode": rec.get("acquire_mode"),
        "attempts": rec.get("attempts"),
        "extension": rec.get("extension"),
        "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with winpath.open_write(meta_path, encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return meta_path


def run_acquire(*, adapter: Any, policy: CollectionPolicy, store: StateStore,
                workspace_dir: str, run_id: str, options: AcquireOptions | None = None,
                log: LogFn = _noop) -> dict[str, Any]:
    """批量采集，逐条落库，返回汇总。"""
    opts = options or AcquireOptions()
    winpath.makedirs(workspace_dir)
    items = select_items(store, opts, policy)
    log(f"[acquire] 待处理 {len(items)} 条（dry_run={opts.dry_run}）")

    report_path = os.path.join(workspace_dir, "acquisition_report.jsonl")
    counts: dict[str, int] = {}
    records: list[dict[str, Any]] = []
    started = time.time()

    with winpath.open_write(report_path, "a", encoding="utf-8") as fh:
        for idx, item in enumerate(items, start=1):
            rec = acquire_one(adapter=adapter, item=item, policy=policy,
                              workspace_dir=workspace_dir, opts=opts, log=log)
            rec["run_id"] = run_id
            rec["acquired_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            records.append(rec)
            counts[rec["status"]] = counts.get(rec["status"], 0) + 1

            store.upsert_artifact(
                rec["item_id"],
                sha256=rec.get("sha256"), path=rec.get("artifact_path"),
                size_bytes=rec.get("size_bytes"), media_type=rec.get("media_type"),
                acquire_mode=rec.get("acquire_mode"), attempts=rec.get("attempts") or 0,
                status=rec["status"], error=rec.get("error"),
                acquired_at=rec["acquired_at"],
            )
            store.add_decision(rec["item_id"], "acquire", rec["status"],
                               rec["reason_codes"], {
                                   "size_bytes": rec.get("size_bytes"),
                                   "sha256": rec.get("sha256"),
                                   "artifact_path": rec.get("artifact_path"),
                                   "acquire_mode": rec.get("acquire_mode"),
                                   "extension": rec.get("extension"),
                               })
            store.set_item_stage(rec["item_id"], rec["status"], "acquire")
            if rec.get("artifact_path"):
                _write_artifact_meta(workspace_dir, rec["item_id"], rec)
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            if idx % 20 == 0 or idx == len(items):
                store.commit()
                log(f"[acquire] {idx}/{len(items)} 已处理，状态分布 {counts}")

    store.commit()
    summary = {
        "total_selected": len(items),
        "selection": {
            "sample": opts.sample,
            "sample_strata": list(opts.sample_strata),
            "sample_seed": opts.sample_seed,
            "limit": opts.limit,
            "extensions": list(opts.extensions),
            "item_types": list(opts.item_types),
            "source_names": list(opts.source_names),
            "include_statuses": list(opts.include_statuses),
            "strata_covered": len({stratum_key(i, opts.sample_strata) for i in items}),
        },
        "status_counts": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "bytes_acquired": sum(int(r.get("size_bytes") or 0) for r in records
                              if r.get("artifact_path")),
        "elapsed_sec": round(time.time() - started, 2),
        "report_path": report_path,
        "dry_run": opts.dry_run,
    }
    store.touch_run(run_id, {"collection.acquire": summary})
    log(f"[acquire] 完成：{summary['status_counts']}")
    return summary
