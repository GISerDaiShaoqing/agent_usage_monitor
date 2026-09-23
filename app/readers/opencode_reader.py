"""OpenCode 用量数据源（次要）：只读解析 ~/.local/share/opencode/opencode.db。

schema（已实测，opencode 2.x）：
- session_message(id, session_id, type, seq, time_created, time_updated, data JSON)
  assistant 行的 data: model{id, providerID}, tokens{input, output, reasoning,
  cache{read, write}}, time{created, completed}, cost
- session_v2 含会话级聚合列，但按模型统计走 session_message。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from ..config import OPENCODE_DB_CANDIDATES
from ..pricing import normalize_model
from .zcode_reader import ModelUsageRow, UsageReport


def _open_db(db_path: Path, warnings: list[str]) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(
            f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5.0)
        conn.execute("SELECT 1 FROM session_message LIMIT 1").fetchone()
        return conn
    except sqlite3.Error as exc:
        warnings.append(f"opencode.db 只读打开失败（{exc}），改用临时副本")
        tmp_dir = Path(tempfile.mkdtemp(prefix="ocgo_oc_"))
        dst = tmp_dir / db_path.name
        shutil.copy2(db_path, dst)
        for suffix in ("-wal", "-shm"):
            side = db_path.with_name(db_path.name + suffix)
            if side.exists():
                shutil.copy2(side, dst.with_name(dst.name + suffix))
        return sqlite3.connect(dst)


def read_opencode_usage(
    since: datetime | None = None,
    pricing=None,
) -> UsageReport:
    """解析 assistant 消息的 tokens 字段，按模型聚合。"""
    report = UsageReport(since=since)
    db_path = next((p for p in OPENCODE_DB_CANDIDATES if p.exists()), None)
    if db_path is None:
        report.warnings.append("未找到 opencode.db，跳过 OpenCode 数据源")
        return report
    report.source = str(db_path)

    cutoff_ms: int | None = None
    if since is not None:
        cutoff_ms = int(since.timestamp() * 1000)

    conn = _open_db(db_path, report.warnings)
    try:
        agg: dict[tuple[str, str], ModelUsageRow] = {}
        cur = conn.execute(
            "SELECT session_id, time_created, data FROM session_message"
            " WHERE type = 'assistant'")
        for _sid, _tc, data_raw in cur:
            try:
                data = json.loads(data_raw or "{}")
            except json.JSONDecodeError:
                continue
            # 时间过滤：优先 data.time.completed/created (ms)
            ts = ((data.get("time", {}) or {}).get("completed")
                  or (data.get("time", {}) or {}).get("created"))
            if not isinstance(ts, (int, float)):
                continue
            if cutoff_ms is not None and ts < cutoff_ms:
                continue
            model = (data.get("model") or {})
            model_id = model.get("id") or "unknown"
            provider = model.get("providerID") or "opencode"
            tokens = (data.get("tokens") or {})
            cache = (tokens.get("cache") or {})
            cache_r = max(0, int(cache.get("read") or 0))
            cache_w = max(0, int(cache.get("write") or 0))
            inp = max(0, int(tokens.get("input") or 0))
            out = max(0, int(tokens.get("output") or 0))

            key_model = normalize_model(model_id)
            key = (str(provider), key_model)
            row = agg.get(key)
            if row is None:
                row = agg[key] = ModelUsageRow(
                    model=key_model, provider=str(provider))
            row.input_uncached += max(0, inp - cache_r - cache_w)
            row.cache_read += cache_r
            row.cache_write += cache_w
            row.output += out
            row.reasoning += max(0, int(tokens.get("reasoning") or 0))
            row.requests += 1

        report.rows = sorted(agg.values(),
                             key=lambda r: r.processed, reverse=True)
        report.total_requests = sum(r.requests for r in report.rows)
    finally:
        conn.close()
    return report
