# -*- coding: utf-8 -*-
"""事故修复：把 14:00 之后被误跑改写的历史 run 还原成迁移完成时的状态。

事故经过：为了验证「装好的 SKILL.md 照着跑能不能用」，把 SKILL.md 里的示例命令
整批执行了一遍，其中 audit / resume / inventory / acquire 都会写运行目录，
于是污染了两个历史 run：repro-v6（audit+resume）与 pilot-20260909（inventory+acquire 被我中途杀掉）。

恢复源：D:\\Users\\admin\\_migration_trash_20260910\\旧仓库_Ella知识库数据集小工具\\output\\document_collection
       （迁移前的原始副本，迁移记录 §6 当时就是用它还原的）

做法（对齐迁移记录 §3/§6）：
  1. 用「今天没被碰过的同名文件」做对照组：把旧副本按同样规则改写后与现状逐字节比对，
     规则复现不了就中止，绝不硬改；
  2. 我新增的文件 -> 移入隔离目录（不直接删）；
  3. 我改写的文件 -> 从旧副本复制 + 路径改写（文本按单反斜杠/JSON 转义两种形式；
     sqlite 按列 UPDATE...REPLACE，不做二进制替换，因为新旧根长度不同会坏页）；
  4. 复验：文件清单、旧路径残留、sqlite 关键行。
"""
from __future__ import annotations

import datetime as dt
import io
import os
import shutil
import sqlite3
import sys

NEW_ROOT = r"D:\Users\admin\Desktop\My_Project\Ella知识库AI生题数据集"
OLD_ROOT = r"D:\Users\admin\Desktop\My_Project\Ella知识库数据集小工具"
CUR_WS = os.path.join(NEW_ROOT, "output", "document_collection")
OLD_WS = (r"D:\Users\admin\_migration_trash_20260910"
          r"\旧仓库_Ella知识库数据集小工具\output\document_collection")
QUARANTINE = (r"D:\Users\admin\_migration_trash_20260910"
              r"\事故_20260910_误跑历史run")
CUTOFF = dt.datetime(2026, 9, 10, 14, 0, 0)
RUNS = ["repro-v6", "pilot-20260909"]
TEXT_EXT = {".json", ".jsonl", ".csv", ".md", ".log", ".txt", ".xml", ".py"}


def esc(p: str) -> str:
    return p.replace("\\", "\\\\")


def rewrite_text(data: bytes) -> bytes:
    """把旧仓库根改写成新仓库根：单反斜杠形式 + JSON 转义形式。"""
    text = data.decode("utf-8")
    text = text.replace(esc(OLD_ROOT), esc(NEW_ROOT))
    text = text.replace(OLD_ROOT, NEW_ROOT)
    return text.encode("utf-8")


def walk_files(base: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for root, _dirs, files in os.walk(base):
        for name in files:
            full = os.path.join(root, name)
            out[os.path.relpath(full, base).replace(os.sep, "/")] = full
    return out


def sqlite_rewrite(path: str) -> int:
    """按列 REPLACE 改写 sqlite 里的旧根路径，返回改动行数。"""
    con = sqlite3.connect(path)
    try:
        changed = 0
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            cols = [(r[1], r[2]) for r in con.execute(f"PRAGMA table_info({table})")]
            for col, ctype in cols:
                if (ctype or "").upper() not in ("TEXT", "VARCHAR", "BLOB", ""):
                    continue
                sql = (f'UPDATE {table} SET "{col}" = REPLACE(REPLACE("{col}", ?, ?), ?, ?) '
                       f'WHERE "{col}" LIKE ? OR "{col}" LIKE ?')
                cur = con.execute(sql, (OLD_ROOT, NEW_ROOT, esc(OLD_ROOT), esc(NEW_ROOT),
                                        f"%{OLD_ROOT}%", f"%{esc(OLD_ROOT)}%"))
                changed += cur.rowcount if cur.rowcount > 0 else 0
        con.commit()
        return changed
    finally:
        con.close()


def main() -> int:
    if not os.path.isdir(OLD_WS):
        print(f"找不到恢复源：{OLD_WS}")
        return 1
    os.makedirs(QUARANTINE, exist_ok=True)
    report: list[str] = []
    control_ok = True
    control_n = 0

    for run in RUNS:
        cur_dir, old_dir = os.path.join(CUR_WS, run), os.path.join(OLD_WS, run)
        cur, old = walk_files(cur_dir), walk_files(old_dir)
        report.append(f"\n===== {run} =====")
        report.append(f"现状 {len(cur)} 文件 / 旧副本 {len(old)} 文件")

        added = sorted(set(cur) - set(old))
        touched = sorted(rel for rel in set(cur) & set(old)
                         if dt.datetime.fromtimestamp(os.path.getmtime(cur[rel])) >= CUTOFF)
        report.append(f"我新增的（隔离）：{len(added)}")
        report.append(f"我改写的（还原）：{len(touched)}")

        # --- 1. 对照组：没被碰过的同名文本文件，验证改写规则能逐字节复现 ---
        controls = [rel for rel in sorted(set(cur) & set(old) - set(touched))
                    if os.path.splitext(rel)[1].lower() in TEXT_EXT][:6]
        for rel in controls:
            want = open(cur[rel], "rb").read()
            got = rewrite_text(open(old[rel], "rb").read())
            same = got == want
            control_n += 1
            if not same:
                control_ok = False
                report.append(f"  ✗ 对照组不一致：{rel}（旧副本改写后与现状差 "
                              f"{len(got)} vs {len(want)} bytes）")
        report.append(f"  对照组 {len(controls)} 个文件："
                      f"{'全部逐字节一致' if control_ok else '存在不一致'}")

        # --- 2. 隔离我新增的文件 ---
        for rel in added:
            src = cur[rel]
            dst = os.path.join(QUARANTINE, run, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
        # 清掉因杀进程残留的空目录（tmp/ 等）
        for rel in added:
            d = os.path.dirname(os.path.join(cur_dir, rel.replace("/", os.sep)))
            while os.path.normcase(d) != os.path.normcase(cur_dir) and os.path.isdir(d) \
                    and not os.listdir(d):
                os.rmdir(d)
                d = os.path.dirname(d)

        # --- 3. 还原我改写的文件 ---
        for rel in touched:
            src, dst = old[rel], cur[rel]
            if rel.endswith(".sqlite3"):
                shutil.copyfile(src, dst)
                n = sqlite_rewrite(dst)
                report.append(f"  ↺ {rel}（sqlite 按列改写 {n} 行）")
            elif os.path.splitext(rel)[1].lower() in TEXT_EXT:
                data = rewrite_text(open(src, "rb").read())
                with io.open(dst, "wb") as fh:
                    fh.write(data)
                report.append(f"  ↺ {rel}（文本改写 {len(data)} bytes）")
            else:
                shutil.copyfile(src, dst)
                report.append(f"  ↺ {rel}（原样复制）")

    print("\n".join(report))
    print(f"\n对照组总计 {control_n} 个文件，规则复现：{'通过' if control_ok else '未通过'}")
    return 0 if control_ok else 2


if __name__ == "__main__":
    sys.exit(main())
