"""SQLite 读写：批次快照、运行清单、隔离行、键值状态。

只放数据访问，不放判定；调用方拿到的是普通字典。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from app import config

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    schema_id      TEXT PRIMARY KEY,
    version_label  TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    source_sha256  TEXT NOT NULL,
    encoding       TEXT NOT NULL,
    bom            INTEGER NOT NULL,
    field_count    INTEGER NOT NULL,
    blocked_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    batch_key      TEXT NOT NULL UNIQUE,
    old_schema_id  TEXT NOT NULL,
    new_schema_id  TEXT NOT NULL,
    version_seq    INTEGER NOT NULL,
    input_sha256   TEXT NOT NULL,
    ruleset        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    summary_json   TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine (
    schema_id     TEXT NOT NULL,
    line          INTEGER,
    table_name    TEXT,
    column_name   TEXT,
    reason        TEXT NOT NULL,
    blocking      INTEGER NOT NULL,
    detail        TEXT,
    raw           TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recovery_points (
    point_id        TEXT PRIMARY KEY,
    release_seq     INTEGER NOT NULL UNIQUE,
    run_id          TEXT NOT NULL,
    schema_id       TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    frozen          INTEGER NOT NULL,
    frozen_at       TEXT,
    created_at      TEXT NOT NULL,
    consumers_json  TEXT NOT NULL,
    payload_json    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rollback_orders (
    order_id        TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    point_id        TEXT,
    release_seq     INTEGER NOT NULL,
    fingerprint     TEXT,
    status          TEXT NOT NULL,
    requested_at    TEXT NOT NULL,
    finished_at     TEXT,
    attempts        INTEGER NOT NULL,
    blockers_json   TEXT NOT NULL,
    payload_json    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rollback_events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   TEXT NOT NULL,
    event      TEXT NOT NULL,
    reason     TEXT,
    at         TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quarantine_schema ON quarantine(schema_id);
CREATE INDEX IF NOT EXISTS idx_recovery_seq ON recovery_points(release_seq);
CREATE INDEX IF NOT EXISTS idx_rollback_point ON rollback_orders(point_id);
CREATE INDEX IF NOT EXISTS idx_events_order ON rollback_events(order_id, seq);
"""


def db_path() -> Path:
    raw = os.environ.get("FIELDMAP_DB", "").strip()
    path = Path(raw) if raw else config.DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)


def reset() -> None:
    """测试与重建用：清空全部业务表。"""
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)
        for table in ("snapshots", "runs", "quarantine", "kv",
                      "recovery_points", "rollback_orders", "rollback_events"):
            conn.execute(f"DELETE FROM {table}")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value):
    if not value:
        return None
    return json.loads(value)


# ---------- 快照 ----------

def save_snapshot(snapshot: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO snapshots (schema_id, version_label, source_path, source_sha256,
                   encoding, bom, field_count, blocked_count, created_at, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(schema_id) DO UPDATE SET
                   version_label=excluded.version_label,
                   source_path=excluded.source_path,
                   source_sha256=excluded.source_sha256,
                   encoding=excluded.encoding,
                   bom=excluded.bom,
                   field_count=excluded.field_count,
                   blocked_count=excluded.blocked_count,
                   created_at=excluded.created_at,
                   payload_json=excluded.payload_json""",
            (
                snapshot["schema_id"], snapshot["version_label"], snapshot["source_path"],
                snapshot["source_sha256"], snapshot["encoding"], int(bool(snapshot["bom"])),
                int(snapshot["field_count"]), int(snapshot["blocked_count"]),
                snapshot["created_at"], _dumps(snapshot),
            ),
        )


def load_snapshot(schema_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE schema_id=?", (schema_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_snapshots(limit: int = 20) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT schema_id, version_label, source_path, source_sha256, encoding, bom,
                      field_count, blocked_count, created_at
               FROM snapshots ORDER BY created_at DESC, schema_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_quarantine(schema_id: str, rows: list[dict]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM quarantine WHERE schema_id=?", (schema_id,))
        conn.executemany(
            """INSERT INTO quarantine (schema_id, line, table_name, column_name, reason,
                   blocking, detail, raw) VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    schema_id, int(row.get("line") or 0), row.get("table") or "",
                    row.get("column") or "", row["reason"], int(bool(row.get("blocking"))),
                    row.get("detail") or "", row.get("raw") or "",
                )
                for row in rows
            ],
        )


def list_quarantine(schema_id: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT line, table_name, column_name, reason, blocking, detail, raw
               FROM quarantine WHERE schema_id=? ORDER BY line, reason""",
            (schema_id,),
        ).fetchall()
    return [
        {
            "line": row["line"],
            "table": row["table_name"],
            "column": row["column_name"],
            "reason": row["reason"],
            "blocking": bool(row["blocking"]),
            "detail": row["detail"],
            "raw": row["raw"],
        }
        for row in rows
    ]


# ---------- 运行清单 ----------

def save_run(run: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO runs (run_id, batch_key, old_schema_id, new_schema_id, version_seq,
                   input_sha256, ruleset, created_at, effective_from, effective_to,
                   summary_json, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run["run_id"], run["batch_key"], run["old_schema_id"], run["new_schema_id"],
                int(run["version_seq"]), run["input_sha256"], run["ruleset"],
                run["created_at"], run["effective_from"], run["effective_to"],
                _dumps(run["summary"]), _dumps(run),
            ),
        )


def find_run_by_key(batch_key: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE batch_key=?", (batch_key,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def run_by_id(run_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def latest_run() -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs ORDER BY version_seq DESC LIMIT 1"
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_runs(limit: int = 30) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT summary_json FROM runs ORDER BY version_seq DESC, run_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_loads(row["summary_json"]) for row in rows]


def count_runs() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    return int(row["n"])


def next_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"]) + 1


def current_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"])


def list_windows() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, effective_from, effective_to FROM runs ORDER BY version_seq"
        ).fetchall()
    return [dict(row) for row in rows]


# ---------- 键值 ----------

def set_kv(key: str, value) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, _dumps(value)),
        )


def get_kv(key: str, default=None):
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return _loads(row["v"]) if row else default


# ---------- 恢复点（M8） ----------

def save_recovery_point(point: dict) -> None:
    """按发布序号落恢复点；已存在则不覆盖冻结态，只在未冻结时刷新。"""
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO recovery_points (point_id, release_seq, run_id, schema_id,
                   fingerprint, frozen, frozen_at, created_at, consumers_json, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(release_seq) DO UPDATE SET
                   run_id=excluded.run_id,
                   schema_id=excluded.schema_id,
                   fingerprint=excluded.fingerprint,
                   frozen=recovery_points.frozen,
                   frozen_at=recovery_points.frozen_at,
                   consumers_json=excluded.consumers_json,
                   payload_json=excluded.payload_json""",
            (
                point["point_id"], int(point["release_seq"]), point["run_id"],
                point["schema_id"], point["fingerprint"], int(bool(point["frozen"])),
                point.get("frozen_at"), point["created_at"],
                _dumps(point.get("consumers") or []), _dumps(point),
            ),
        )


def mark_recovery_frozen(point_id: str, frozen_at: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE recovery_points SET frozen=1, frozen_at=? WHERE point_id=?",
            (frozen_at, point_id),
        )


def _recovery_from_row(row) -> dict:
    """冻结态以表列为唯一事实源，回写进 payload，避免重登记把旧冻结态带回。"""
    point = _loads(row["payload_json"])
    point["frozen"] = bool(row["frozen"])
    point["frozen_at"] = row["frozen_at"]
    return point


def recovery_point_by_id(point_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json, frozen, frozen_at FROM recovery_points WHERE point_id=?",
            (point_id,),
        ).fetchone()
    return _recovery_from_row(row) if row else None


def recovery_point_by_seq(release_seq: int) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT payload_json, frozen, frozen_at FROM recovery_points
               WHERE release_seq=?""",
            (int(release_seq),),
        ).fetchone()
    return _recovery_from_row(row) if row else None


def list_recovery_points() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT point_id, release_seq, run_id, schema_id, fingerprint, frozen,
                      frozen_at, created_at, consumers_json
               FROM recovery_points ORDER BY release_seq""",
        ).fetchall()
    out = []
    for row in rows:
        item = {key: row[key] for key in
                ("point_id", "release_seq", "run_id", "schema_id", "fingerprint",
                 "frozen", "frozen_at", "created_at")}
        item["frozen"] = bool(item["frozen"])
        item["consumer_count"] = len(_loads(row["consumers_json"]) or [])
        out.append(item)
    return out


def latest_frozen_point_seq() -> int | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT MAX(release_seq) AS n FROM recovery_points WHERE frozen=1"
        ).fetchone()
    value = row["n"]
    return int(value) if value is not None else None


# ---------- 回滚单与事件流（M8） ----------

def save_rollback_order(order: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO rollback_orders (order_id, idempotency_key, point_id, release_seq,
                   fingerprint, status, requested_at, finished_at, attempts,
                   blockers_json, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(idempotency_key) DO UPDATE SET
                   status=excluded.status,
                   finished_at=excluded.finished_at,
                   attempts=excluded.attempts,
                   blockers_json=excluded.blockers_json,
                   payload_json=excluded.payload_json""",
            (
                order["order_id"], order["idempotency_key"], order["point_id"],
                int(order["release_seq"]), order["fingerprint"], order["status"],
                order["requested_at"], order.get("finished_at"),
                int(order.get("attempts", 1)), _dumps(order.get("blockers") or []),
                _dumps(order),
            ),
        )


def rollback_order_by_id(order_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM rollback_orders WHERE order_id=?", (order_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def rollback_order_by_key(idempotency_key: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM rollback_orders WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_rollback_orders() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM rollback_orders ORDER BY requested_at, order_id"
        ).fetchall()
    return [_loads(row["payload_json"]) for row in rows]


def append_rollback_event(order_id: str, event: str, reason: str | None,
                          at: str, detail: dict | None = None) -> None:
    """事件只追加不改写：中断后靠这条流恢复，任何记录都不丢。"""
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO rollback_events (order_id, event, reason, at, detail_json)
               VALUES (?,?,?,?,?)""",
            (order_id, event, reason, at, _dumps(detail or {})),
        )


def list_rollback_events(order_id: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT seq, order_id, event, reason, at, detail_json
               FROM rollback_events WHERE order_id=? ORDER BY seq""",
            (order_id,),
        ).fetchall()
    out = []
    for row in rows:
        item = {key: row[key] for key in ("seq", "order_id", "event", "reason", "at")}
        item["detail"] = _loads(row["detail_json"])
        out.append(item)
    return out
