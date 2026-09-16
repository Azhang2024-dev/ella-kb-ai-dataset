# -*- coding: utf-8 -*-
"""还原后的独立复验：文件清单 / 旧路径残留 / sqlite 关键行 / 业务计数。"""
from __future__ import annotations
import json, os, sqlite3, sys

NEW_ROOT = r"D:\Users\admin\Desktop\My_Project\Ella知识库AI生题数据集"
OLD_ROOT = r"D:\Users\admin\Desktop\My_Project\Ella知识库数据集小工具"
CUR_WS = os.path.join(NEW_ROOT, "output", "document_collection")
OLD_WS = (r"D:\Users\admin\_migration_trash_20260910"
          r"\旧仓库_Ella知识库数据集小工具\output\document_collection")
TEXT_EXT = {".json", ".jsonl", ".csv", ".md", ".log", ".txt", ".xml", ".py"}
fails = []

def walk(base):
    out = {}
    for root, _d, files in os.walk(base):
        for n in files:
            full = os.path.join(root, n)
            out[os.path.relpath(full, base).replace(os.sep, "/")] = full
    return out

def esc(p): return p.replace("\\", "\\\\")

for run in ("repro-v6", "pilot-20260909"):
    cur, old = walk(os.path.join(CUR_WS, run)), walk(os.path.join(OLD_WS, run))
    extra = sorted(set(cur) - set(old))
    missing = sorted(set(old) - set(cur))
    print(f"\n===== {run} =====")
    print(f"现状 {len(cur)} / 旧副本 {len(old)}；多出 {len(extra)}；缺少 {len(missing)}")
    if extra:
        fails.append(f"{run}: 仍有旧副本里不存在的文件 {extra[:5]}")
    buckets = {}
    for rel in missing:
        buckets.setdefault(rel.split("/")[0], []).append(rel)
    for k, v in sorted(buckets.items()):
        print(f"  缺少（按目录）：{k}/ 共 {len(v)} 个")
    if run == "pilot-20260909" and missing:
        fails.append("pilot-20260909 是完整搬迁的 run，不该缺任何文件")
    if run == "repro-v6":
        allowed = {"artifacts", "extracted", "segments"}
        bad = [k for k in buckets if k not in allowed]
        if bad:
            fails.append(f"repro-v6 缺少的目录超出迁移记录允许范围：{bad}")
        else:
            print("  缺少的只有 artifacts/extracted/segments —— 与迁移记录 §3 一致（当时只搬报告与状态库）")

    # 旧根残留扫描
    residue = []
    for rel, path in cur.items():
        if os.path.splitext(rel)[1].lower() in TEXT_EXT:
            data = open(path, "rb").read()
            if esc(OLD_ROOT).encode() in data or OLD_ROOT.encode() in data:
                residue.append(rel)
    db = os.path.join(CUR_WS, run, "state.sqlite3")
    con = sqlite3.connect(db)
    db_residue = []
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        for r in con.execute(f"PRAGMA table_info({t})"):
            c = r[1]
            try:
                n = con.execute(f'SELECT COUNT(*) FROM {t} WHERE "{c}" LIKE ? OR "{c}" LIKE ?',
                                (f"%{OLD_ROOT}%", f"%{esc(OLD_ROOT)}%")).fetchone()[0]
            except sqlite3.Error:
                continue
            if n:
                db_residue.append(f"{t}.{c}={n}")
    print(f"  旧仓库根残留：文本 {len(residue)} 个文件；sqlite {db_residue or '0 处'}")
    if residue or db_residue:
        fails.append(f"{run}: 旧路径残留 {residue[:3]} {db_residue[:3]}")

    # sqlite 关键行
    row = con.execute("SELECT run_id, started_at, updated_at, stage_state_json FROM runs").fetchall()
    for rid, st, up, ssj in row:
        keys = list(json.loads(ssj).keys()) if ssj else []
        print(f"  runs: {rid} started={st} updated={up} stage_state_json={len(keys)} 键")
        if run == "repro-v6":
            if len(keys) != 12:
                fails.append(f"repro-v6 stage_state_json 应为 12 键（迁移记录 §7），实际 {len(keys)}")
            if up != "2026-09-09T22:16:46":
                fails.append(f"repro-v6 updated_at 应为 2026-09-09T22:16:46（迁移记录 §6），实际 {up}")
    for t in ("items", "artifacts", "decisions", "extractions"):
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  表 {t}: {n} 行")
        except sqlite3.Error:
            pass
    con.close()

# 业务计数：盘点必须是 4892 条
inv = os.path.join(CUR_WS, "pilot-20260909", "source_inventory.jsonl")
n = sum(1 for _ in open(inv, encoding="utf-8"))
print(f"\npilot-20260909/source_inventory.jsonl = {n} 行（期望 4892）")
if n != 4892:
    fails.append(f"盘点行数 {n} != 4892")
inv6 = os.path.join(CUR_WS, "repro-v6", "source_inventory.jsonl")
if os.path.isfile(inv6):
    n6 = sum(1 for _ in open(inv6, encoding="utf-8"))
    print(f"repro-v6/source_inventory.jsonl = {n6} 行（期望 4892）")
    if n6 != 4892:
        fails.append(f"repro-v6 盘点行数 {n6} != 4892")

print("\n结论：" + ("全部通过" if not fails else "有 %d 项未通过" % len(fails)))
for f in fails:
    print("  ✗ " + f)
sys.exit(0 if not fails else 1)
