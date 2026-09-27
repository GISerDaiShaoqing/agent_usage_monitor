"""本 App 唯一可写数据库：额度快照（历史页 sparkline 数据源），按套餐键控。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import STORE_PATH
from .zen_client import QuotaSnapshot


def _connect() -> sqlite3.Connection:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STORE_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS quota_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            window TEXT NOT NULL,
            used_percent REAL NOT NULL,
            remaining_percent REAL NOT NULL,
            status TEXT NOT NULL
        )
    """)
    # 迁移：老库补 plan 列
    cols = {r[1] for r in conn.execute("PRAGMA table_info(quota_snapshot)")}
    if "plan" not in cols:
        conn.execute(
            "ALTER TABLE quota_snapshot ADD COLUMN plan TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshot_time ON quota_snapshot (window, fetched_at)"
    )
    return conn


def save_snapshot(snap: QuotaSnapshot, plan_id: str = "") -> None:
    if not snap.ok:
        return
    with _connect() as conn:
        now = snap.fetched_at.isoformat(timespec="seconds")
        for w in snap.windows.values():
            conn.execute(
                "INSERT INTO quota_snapshot (fetched_at, window, used_percent,"
                " remaining_percent, status, plan) VALUES (?,?,?,?,?,?)",
                (now, w.name, w.used_percent, w.remaining_percent, w.status, plan_id),
            )


def load_history(window: str, hours: float = 24.0,
                 plan_id: str = "") -> list[tuple[str, float]]:
    """返回 [(iso_time, remaining_percent)]，按时间升序；plan_id 空则不过滤。"""
    cutoff = (datetime.now(timezone.utc)
              .timestamp() - hours * 3600)
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    sql = ("SELECT fetched_at, remaining_percent FROM quota_snapshot"
           " WHERE window = ? AND fetched_at >= ?"
           + (" AND plan = ?" if plan_id else "")
           + " ORDER BY fetched_at")
    params: list = [window, cutoff_iso] + ([plan_id] if plan_id else [])
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [(r[0], r[1]) for r in rows]


def history_windows(plan_id: str = "") -> list[str]:
    """返回有快照记录的窗口名（按展示顺序排列）；plan_id 空则不过滤。"""
    order = ["grant", "rolling", "weekly", "monthly"]
    sql = "SELECT DISTINCT window FROM quota_snapshot" + (
        " WHERE plan = ?" if plan_id else "")
    with _connect() as conn:
        rows = conn.execute(sql, (plan_id,) if plan_id else ()).fetchall()
    names = [r[0] for r in rows]
    return sorted(names, key=lambda n: order.index(n) if n in order else 99)


def prune(keep_days: int = 30) -> None:
    """只保留最近 keep_days 天快照。"""
    cutoff = (datetime.now(timezone.utc).timestamp() - keep_days * 86400)
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute("DELETE FROM quota_snapshot WHERE fetched_at < ?", (cutoff_iso,))
