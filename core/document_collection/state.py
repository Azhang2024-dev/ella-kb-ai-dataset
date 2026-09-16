"""SQLite 状态存储：断点续跑与可复核的唯一事实来源。

为什么用 SQLite 而不是纯 JSONL：
1. 采集中断后必须能"只补没做完的"，需要按 status 查询；
2. 同一 item_token 在两个源文件夹都出现时，必须能靠唯一索引强制合并去重；
3. 报告阶段要做多维统计，SQL 比反复读 JSONL 稳。

JSONL/CSV 仍然保留，但它们是"导出产物"，不是状态源。
所有写入都是 upsert，重复执行同一 run 不会翻倍。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Iterable, Mapping, Sequence

from .contracts import safe_item_id

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    workspace_dir   TEXT,
    config_json     TEXT,
    policy_digest   TEXT,
    stage_state_json TEXT
);

CREATE TABLE IF NOT EXISTS items (
    item_id             TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL,
    source_name         TEXT NOT NULL,
    source_folder_path  TEXT NOT NULL,
    item_token          TEXT NOT NULL,
    name                TEXT NOT NULL,
    item_type           TEXT NOT NULL,
    extension           TEXT,
    mime_type           TEXT,
    remote_size         INTEGER,
    remote_modified_time TEXT,
    url                 TEXT,
    relative_path       TEXT,
    status              TEXT NOT NULL DEFAULT 'discovered',
    stage               TEXT NOT NULL DEFAULT 'inventory',
    updated_at          TEXT NOT NULL,
    metadata_json       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_items_token ON items(item_token);
CREATE INDEX IF NOT EXISTS ix_items_status ON items(status);
CREATE INDEX IF NOT EXISTS ix_items_source ON items(source_name);
CREATE INDEX IF NOT EXISTS ix_items_ext ON items(extension);

CREATE TABLE IF NOT EXISTS item_sources (
    item_token          TEXT NOT NULL,
    source_name         TEXT NOT NULL,
    source_folder_path  TEXT NOT NULL,
    relative_path       TEXT,
    url                 TEXT,
    PRIMARY KEY (item_token, source_name, source_folder_path)
);

CREATE TABLE IF NOT EXISTS artifacts (
    item_id     TEXT PRIMARY KEY,
    sha256      TEXT,
    path        TEXT,
    size_bytes  INTEGER,
    media_type  TEXT,
    acquire_mode TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',
    error       TEXT,
    acquired_at TEXT
);

CREATE TABLE IF NOT EXISTS extractions (
    item_id         TEXT PRIMARY KEY,
    extractor       TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    char_count      INTEGER,
    word_count      INTEGER,
    cjk_char_count  INTEGER,
    page_count      INTEGER,
    text_ratio      REAL,
    language        TEXT,
    language_confidence REAL,
    is_long_document INTEGER NOT NULL DEFAULT 0,
    segments_path   TEXT,
    text_path       TEXT,
    error           TEXT,
    extracted_at    TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     TEXT NOT NULL,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    evidence_json TEXT,
    decided_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_item ON decisions(item_id);
CREATE INDEX IF NOT EXISTS ix_decisions_stage ON decisions(stage);

CREATE TABLE IF NOT EXISTS dedup_groups (
    group_key       TEXT NOT NULL,
    kind            TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    is_representative INTEGER NOT NULL DEFAULT 0,
    reason          TEXT,
    PRIMARY KEY (group_key, kind, item_id)
);

CREATE TABLE IF NOT EXISTS queries (
    item_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    type_key    TEXT NOT NULL,
    question_count INTEGER NOT NULL,
    language    TEXT,
    compatibility_only INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    validation_json TEXT,
    status      TEXT NOT NULL DEFAULT 'generated',
    created_at  TEXT NOT NULL,
    PRIMARY KEY (item_id, seq)
);

CREATE TABLE IF NOT EXISTS questions (
    item_id     TEXT NOT NULL,
    query_seq   INTEGER NOT NULL,
    language    TEXT,
    payload_json TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'generated',
    created_at  TEXT NOT NULL,
    PRIMARY KEY (item_id, query_seq)
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     TEXT,
    scene       TEXT,
    model       TEXT,
    ok          INTEGER NOT NULL,
    prompt_chars INTEGER,
    output_chars INTEGER,
    latency_ms  INTEGER,
    error       TEXT,
    called_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime("%Y-%m-%dT%H:%M:%S")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


class StateStore:
    """对 SQLite 的薄封装。

    线程模型：阶段主循环在**主线程**串行写库；并发只发生在纯计算/网络阶段
    （抽取、语义复核、生题），worker 线程不直接碰库。
    唯一的例外是 LLM 调用台账：模型适配器的 on_call 回调可能在 worker 线程里
    触发，因此连接以 check_same_thread=False 打开，并用一把可重入锁把
    log_llm_call / commit 串行化，避免同一连接被多线程交错写入。
    """

    def __init__(self, db_path: str | os.PathLike[str]):
        self.db_path = os.fspath(db_path)
        from .winpath import long_path

        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(long_path(parent), exist_ok=True)
        # sqlite3 在 Windows 上走 CreateFileW，接受 \\?\ 扩展长度前缀
        self._conn = sqlite3.connect(long_path(self.db_path), check_same_thread=False)
        self._lock = threading.RLock()
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_DDL)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES(?,?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        self._conn.commit()

    # -- 生命周期 ---------------------------------------------------------
    def close(self) -> None:
        try:
            self._conn.commit()
        finally:
            self._conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    # -- run --------------------------------------------------------------
    def start_run(self, run_id: str, workspace_dir: str, config: Mapping[str, Any] | None = None,
                  policy_digest: str | None = None) -> None:
        now = _now()
        self._conn.execute(
            """INSERT INTO runs(run_id, started_at, updated_at, workspace_dir, config_json, policy_digest, stage_state_json)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET updated_at=excluded.updated_at""",
            (run_id, now, now, workspace_dir, _dumps(config or {}), policy_digest, _dumps({})),
        )
        self._conn.commit()

    def touch_run(self, run_id: str, stage_state: Mapping[str, Any] | None = None) -> None:
        """更新运行时间戳，并**合并**阶段摘要。

        必须是合并而不是覆盖：每条 CLI 命令都是独立进程，
        它内存里的 stage_summaries 只有自己这一个阶段。
        第一版实现直接覆盖，结果跑完 report 之后
        stage_state_json 里只剩 {"collection.report": ...}，
        inventory/acquire/extract 的摘要全丢了 ——
        cost_and_runtime.json 和 resume 的阶段视图因此只剩最后一步，
        audit 也读不到 screen 是否启用过语义复核。
        """
        if stage_state is None:
            self._conn.execute("UPDATE runs SET updated_at=? WHERE run_id=?", (_now(), run_id))
            self._conn.commit()
            return
        row = self._conn.execute(
            "SELECT stage_state_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        merged: dict[str, Any] = {}
        if row and row["stage_state_json"]:
            try:
                existing = json.loads(row["stage_state_json"])
                if isinstance(existing, dict):
                    merged.update(existing)
            except json.JSONDecodeError:
                pass
        merged.update(dict(stage_state))
        self._conn.execute(
            "UPDATE runs SET updated_at=?, stage_state_json=? WHERE run_id=?",
            (_now(), _dumps(merged), run_id),
        )
        self._conn.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM runs ORDER BY started_at DESC").fetchall()
        return [dict(r) for r in rows]

    # -- items ------------------------------------------------------------
    def upsert_item(self, item: Mapping[str, Any]) -> str:
        """写入或更新一条资源记录，返回 item_id。

        item_id 由 item_token 归一而来（contracts.safe_item_id）：
        - 跨源同 token 自动合并（计划硬约束 2）；
        - item_id 会被拿去派生文件名，所以必须保证文件系统安全
          （本地目录适配器用绝对路径当 token，直接用会超 Windows MAX_PATH）。
        """
        token = str(item["item_token"])
        normalized_id = safe_item_id(token)
        metadata = dict(item.get("metadata") or {})
        rel = item.get("relative_path") or metadata.get("relative_path") or item.get("name")
        now = _now()
        existing = self._conn.execute("SELECT item_id, status, stage FROM items WHERE item_token=?", (token,)).fetchone()
        if existing:
            item_id = existing["item_id"]
            self._conn.execute(
                """UPDATE items SET name=?, item_type=?, extension=?, mime_type=?,
                          remote_size=?, remote_modified_time=?, url=?, relative_path=?,
                          metadata_json=?, updated_at=?
                   WHERE item_id=?""",
                (
                    item.get("name"), item.get("item_type"), item.get("extension"),
                    item.get("mime_type"), item.get("remote_size"), item.get("remote_modified_time"),
                    item.get("url"), rel, _dumps(metadata), now, item_id,
                ),
            )
        else:
            item_id = normalized_id
            self._conn.execute(
                """INSERT INTO items(item_id, run_id, source_name, source_folder_path, item_token, name,
                          item_type, extension, mime_type, remote_size, remote_modified_time, url,
                          relative_path, status, stage, updated_at, metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item_id, item.get("run_id", ""), item.get("source_name"), item.get("source_folder_path"),
                    token, item.get("name"), item.get("item_type"), item.get("extension"),
                    item.get("mime_type"), item.get("remote_size"), item.get("remote_modified_time"),
                    item.get("url"), rel, item.get("status", "discovered"),
                    item.get("stage", "inventory"), now, _dumps(metadata),
                ),
            )
        # 记录全部来源路径（去重要求：保留所有来源）
        self._conn.execute(
            """INSERT OR IGNORE INTO item_sources(item_token, source_name, source_folder_path, relative_path, url)
               VALUES(?,?,?,?,?)""",
            (token, item.get("source_name"), item.get("source_folder_path"), rel, item.get("url")),
        )
        return item_id

    def upsert_items(self, items: Iterable[Mapping[str, Any]]) -> int:
        n = 0
        for it in items:
            self.upsert_item(it)
            n += 1
        self._conn.commit()
        return n

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
        return self._hydrate_item(row) if row else None

    def _hydrate_item(self, row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        try:
            d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            d["metadata"] = {}
        return d

    def list_items(self, *, statuses: Sequence[str] | None = None,
                   item_types: Sequence[str] | None = None,
                   extensions: Sequence[str] | None = None,
                   source_names: Sequence[str] | None = None,
                   limit: int | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM items"
        where: list[str] = []
        args: list[Any] = []
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args.extend(statuses)
        if item_types:
            where.append(f"item_type IN ({','.join('?' * len(item_types))})")
            args.extend(item_types)
        if extensions:
            where.append(f"extension IN ({','.join('?' * len(extensions))})")
            args.extend(extensions)
        if source_names:
            where.append(f"source_name IN ({','.join('?' * len(source_names))})")
            args.extend(source_names)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY source_name, relative_path"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._hydrate_item(r) for r in self._conn.execute(sql, args).fetchall()]

    def count_items(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self._conn.execute("SELECT status, COUNT(*) AS c FROM items GROUP BY status"):
            out[row["status"]] = int(row["c"])
        return out

    def item_sources(self, item_token: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM item_sources WHERE item_token=? ORDER BY source_name, relative_path",
            (item_token,),
        ).fetchall()
        return [dict(r) for r in rows]

    def all_item_sources(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for r in self._conn.execute("SELECT * FROM item_sources ORDER BY source_name, relative_path"):
            out.setdefault(r["item_token"], []).append(dict(r))
        return out

    # -- 阶段状态 ---------------------------------------------------------
    def set_item_stage(self, item_id: str, status: str, stage: str) -> None:
        self._conn.execute(
            "UPDATE items SET status=?, stage=?, updated_at=? WHERE item_id=?",
            (status, stage, _now(), item_id),
        )

    def set_item_stages(self, pairs: Iterable[tuple[str, str, str]]) -> None:
        now = _now()
        self._conn.executemany(
            "UPDATE items SET status=?, stage=?, updated_at=? WHERE item_id=?",
            [(s, st, now, i) for (i, s, st) in pairs],
        )
        self._conn.commit()

    # -- artifacts --------------------------------------------------------
    def upsert_artifact(self, item_id: str, **fields: Any) -> None:
        cols = ["item_id"] + [k for k in fields]
        vals = [item_id] + [
            _dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()
        ]
        placeholders = ",".join("?" * len(cols))
        updates = ",".join(f"{k}=excluded.{k}" for k in fields)
        self._conn.execute(
            f"INSERT INTO artifacts({','.join(cols)}) VALUES({placeholders}) "
            f"ON CONFLICT(item_id) DO UPDATE SET {updates}",
            vals,
        )
        self._conn.commit()

    def get_artifact(self, item_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM artifacts WHERE item_id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def list_artifacts(self, statuses: Sequence[str] | None = None) -> list[dict[str, Any]]:
        if statuses:
            q = f"SELECT * FROM artifacts WHERE status IN ({','.join('?' * len(statuses))})"
            rows = self._conn.execute(q, list(statuses)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM artifacts").fetchall()
        return [dict(r) for r in rows]

    # -- extractions ------------------------------------------------------
    def upsert_extraction(self, item_id: str, **fields: Any) -> None:
        cols = ["item_id"] + [k for k in fields]
        vals = [item_id] + list(fields.values())
        placeholders = ",".join("?" * len(cols))
        updates = ",".join(f"{k}=excluded.{k}" for k in fields)
        self._conn.execute(
            f"INSERT INTO extractions({','.join(cols)}) VALUES({placeholders}) "
            f"ON CONFLICT(item_id) DO UPDATE SET {updates}",
            vals,
        )
        self._conn.commit()

    def get_extraction(self, item_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM extractions WHERE item_id=?", (item_id,)).fetchone()
        return dict(row) if row else None

    def list_extractions(self, statuses: Sequence[str] | None = None) -> list[dict[str, Any]]:
        if statuses:
            q = f"SELECT * FROM extractions WHERE status IN ({','.join('?' * len(statuses))})"
            rows = self._conn.execute(q, list(statuses)).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM extractions").fetchall()
        return [dict(r) for r in rows]

    # -- decisions --------------------------------------------------------
    def add_decision(self, item_id: str, stage: str, status: str,
                     reason_codes: Sequence[str], evidence: Mapping[str, Any] | None = None) -> None:
        self._conn.execute(
            """INSERT INTO decisions(item_id, stage, status, reason_codes_json, evidence_json, decided_at)
               VALUES(?,?,?,?,?,?)""",
            (item_id, stage, status, _dumps(list(reason_codes)), _dumps(dict(evidence or {})), _now()),
        )

    def decisions_for(self, item_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM decisions WHERE item_id=? ORDER BY id", (item_id,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["reason_codes"] = json.loads(d.pop("reason_codes_json") or "[]")
            try:
                d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
            except json.JSONDecodeError:
                d["evidence"] = {}
            out.append(d)
        return out

    def latest_decisions(self, stage: str) -> dict[str, dict[str, Any]]:
        """每个 item 在指定 stage 的最后一次决定。"""
        rows = self._conn.execute(
            """SELECT d.* FROM decisions d
               JOIN (SELECT item_id, MAX(id) AS mid FROM decisions WHERE stage=? GROUP BY item_id) m
                 ON d.id = m.mid""",
            (stage,),
        ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = dict(r)
            d["reason_codes"] = json.loads(d.pop("reason_codes_json") or "[]")
            try:
                d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
            except json.JSONDecodeError:
                d["evidence"] = {}
            out[d["item_id"]] = d
        return out

    # -- dedup ------------------------------------------------------------
    def set_dedup(self, group_key: str, kind: str, item_id: str,
                  is_representative: bool, reason: str = "") -> None:
        self._conn.execute(
            """INSERT INTO dedup_groups(group_key, kind, item_id, is_representative, reason)
               VALUES(?,?,?,?,?)
               ON CONFLICT(group_key, kind, item_id) DO UPDATE SET
                 is_representative=excluded.is_representative, reason=excluded.reason""",
            (group_key, kind, item_id, 1 if is_representative else 0, reason),
        )

    def dedup_groups(self, kind: str | None = None) -> dict[str, list[dict[str, Any]]]:
        if kind:
            rows = self._conn.execute(
                "SELECT * FROM dedup_groups WHERE kind=? ORDER BY group_key, item_id", (kind,)
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM dedup_groups ORDER BY kind, group_key, item_id").fetchall()
        out: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            d = dict(r)
            out.setdefault(f"{d['kind']}:{d['group_key']}", []).append(d)
        return out

    # -- queries ----------------------------------------------------------
    def upsert_query(self, item_id: str, seq: int, **fields: Any) -> None:
        payload = _dumps(fields.pop("payload", {}))
        validation = _dumps(fields.pop("validation", {}))
        cols = ["item_id", "seq", "payload_json", "validation_json"] + list(fields)
        vals = [item_id, seq, payload, validation] + list(fields.values())
        placeholders = ",".join("?" * len(cols))
        updates = ",".join(f"{k}=excluded.{k}" for k in cols if k not in ("item_id", "seq"))
        self._conn.execute(
            f"INSERT INTO queries({','.join(cols)}) VALUES({placeholders}) "
            f"ON CONFLICT(item_id, seq) DO UPDATE SET {updates}",
            vals,
        )
        self._conn.commit()

    def queries_for(self, item_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM queries WHERE item_id=? ORDER BY seq", (item_id,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
            try:
                d["validation"] = json.loads(d.pop("validation_json") or "{}")
            except json.JSONDecodeError:
                d["validation"] = {}
            out.append(d)
        return out

    def all_queries(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM queries ORDER BY item_id, seq").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
            try:
                d["validation"] = json.loads(d.pop("validation_json") or "{}")
            except json.JSONDecodeError:
                d["validation"] = {}
            out.append(d)
        return out

    # -- llm 调用台账 -----------------------------------------------------
    def log_llm_call(self, *, item_id: str | None, scene: str, model: str, ok: bool,
                     prompt_chars: int = 0, output_chars: int = 0,
                     latency_ms: int = 0, error: str | None = None) -> None:
        # 可能从 worker 线程调用，必须与主线程的写库串行化
        with self._lock:
            self._conn.execute(
                """INSERT INTO llm_calls(item_id, scene, model, ok, prompt_chars, output_chars, latency_ms, error, called_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (item_id, scene, model, 1 if ok else 0, prompt_chars, output_chars, latency_ms, error, _now()),
            )
            self._conn.commit()

    def llm_call_stats(self) -> dict[str, Any]:
        row = self._conn.execute(
            """SELECT COUNT(*) AS calls, SUM(ok) AS ok_calls, SUM(prompt_chars) AS prompt_chars,
                      SUM(output_chars) AS output_chars, SUM(latency_ms) AS latency_ms
               FROM llm_calls"""
        ).fetchone()
        return {k: (int(row[k]) if row[k] is not None else 0) for k in row.keys()}

    # -- questions（参考题目）---------------------------------------------
    def upsert_question(self, item_id: str, query_seq: int, **fields: Any) -> None:
        """写入某份文档某条 query 的参考题目（题干/选项/答案/解析）。

        与 upsert_query 同款：payload 走 JSON 列，其余字段按列名写入，
        ON CONFLICT 覆盖。P4 要求"jsonl 与状态库同写"，这里是把出题也纳入
        状态库的那一半——只写 jsonl 不写库会重演上一批 D9 的不一致。
        """
        payload = _dumps(fields.pop("payload", {}))
        cols = ["item_id", "query_seq", "payload_json"] + list(fields)
        vals = [item_id, query_seq, payload] + list(fields.values())
        placeholders = ",".join("?" * len(cols))
        updates = ",".join(f"{k}=excluded.{k}" for k in cols if k not in ("item_id", "query_seq"))
        self._conn.execute(
            f"INSERT INTO questions({','.join(cols)}) VALUES({placeholders}) "
            f"ON CONFLICT(item_id, query_seq) DO UPDATE SET {updates}",
            vals,
        )
        self._conn.commit()

    def questions_for(self, item_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM questions WHERE item_id=? ORDER BY query_seq", (item_id,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
            out.append(d)
        return out

    def all_questions(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM questions ORDER BY item_id, query_seq").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
            out.append(d)
        return out

    # -- 一致性修复 -------------------------------------------------------
    def reconcile_queries_from_jsonl(self, path: str,
                                     logger: Any | None = None) -> dict[str, int]:
        """从 query_records.jsonl 重建 queries 表（P4 兜底）。

        为什么需要：定向"只补失败项"时若绕开 CLI 自写脚本，很容易漏写 upsert_query，
        导致状态库落后于 jsonl（上一批就是这样，172 vs 178，手工 reconcile 才补上）。
        这个命令把 jsonl 作为**导出产物**反灌回状态库，幂等、可反复执行。
        """
        import os as _os

        stats = {"records": 0, "queries": 0, "decisions": 0, "skipped": 0}
        if not _os.path.isfile(path):
            raise FileNotFoundError(path)
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    stats["skipped"] += 1
                    continue
                item_id = str(record.get("item_id") or "")
                if not item_id:
                    stats["skipped"] += 1
                    continue
                status = str(record.get("status") or "needs_review")
                generated_at = str(record.get("generated_at") or _now())
                queries = record.get("queries") or []
                if not queries:
                    stats["skipped"] += 1
                    continue
                stats["records"] += 1
                validation = record.get("validation") or {}
                for query in queries:
                    self.upsert_query(
                        item_id, int(query.get("seq") or 0),
                        type_key=str(query.get("type_key") or ""),
                        question_count=int(query.get("question_count") or 0),
                        language=query.get("language"),
                        compatibility_only=1 if query.get("compatibility_only") else 0,
                        payload=query,
                        validation=validation,
                        status=status,
                        created_at=generated_at,
                    )
                    stats["queries"] += 1
                self.add_decision(item_id, "query", status,
                                  record.get("reason_codes") or ["ok"],
                                  {"query_count": len(queries),
                                   "validation_ok": bool(validation.get("ok")),
                                   "reconciled_from_jsonl": True})
                stats["decisions"] += 1
        self.commit()
        if logger:
            logger(f"[reconcile] 从 {path} 回灌：{stats}")
        return stats

    # -- meta -------------------------------------------------------------
    def set_meta(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, _dumps(value) if isinstance(value, (dict, list)) else str(value)),
        )
        self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
