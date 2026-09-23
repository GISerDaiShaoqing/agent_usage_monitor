"""ZCode 用量数据源：只读解析 ~/.zcode/cli/db/db.sqlite 的 model_usage 表。

零侵入原则：绝不写入该库。并发安全策略：先尝试只读连接（WAL 并发读，
原生 Windows 稳定）；失败则拷贝 db + wal 到临时目录再读。
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import ZCODE_DB_CANDIDATES
from ..pricing import normalize_model, Pricing

# 本工具关注的 provider 前缀（OpenCode Go 相关）。空集合 = 不过滤。
OPENCODE_GO_PREFIXES = ("opencode-go",)


@dataclass
class ModelUsageRow:
    model: str            # 归一化后的模型名
    provider: str
    requests: int = 0     # completed 请求数
    errors: int = 0
    input_uncached: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    reasoning: int = 0
    duration_ms: int = 0

    @property
    def processed(self) -> int:
        return (self.input_uncached + self.cache_read
                + self.cache_write + self.output)


@dataclass
class UsageReport:
    rows: list[ModelUsageRow] = field(default_factory=list)
    total_requests: int = 0
    since: datetime | None = None
    source: str = ""
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        n = len(self.rows)
        return (f"{self.total_requests} 次请求 / {n} 个模型"
                f"（来源: {Path(self.source).name if self.source else '无'}）")


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """只读连接；immutable 不可用（会漏 WAL 数据），常规 ro 即可。"""
    return sqlite3.connect(
        f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5.0
    )


def _copy_and_open(db_path: Path) -> sqlite3.Connection:
    """拷贝 db + -wal + -shm 到临时目录后打开（主库被锁时的兜底）。"""
    tmp_dir = Path(tempfile.mkdtemp(prefix="ocgo_"))
    dst = tmp_dir / db_path.name
    shutil.copy2(db_path, dst)
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        if side.exists():
            shutil.copy2(side, dst.with_name(dst.name + suffix))
    return sqlite3.connect(dst)


def _open_db(db_path: Path, warnings: list[str]) -> sqlite3.Connection:
    try:
        conn = _connect_readonly(db_path)
        conn.execute("SELECT 1 FROM model_usage LIMIT 1").fetchone()
        return conn
    except sqlite3.Error as exc:
        warnings.append(f"只读打开失败（{exc}），改用临时副本")
        return _copy_and_open(db_path)


def read_zcode_usage(
    since: datetime | None = None,
    provider_prefixes: tuple[str, ...] | None = OPENCODE_GO_PREFIXES,
    pricing: Pricing | None = None,
) -> UsageReport:
    """读取 model_usage 并按归一化模型名聚合。

    since: None = 全部；provider_prefixes: None = 全部 provider，
    空元组 = 跳过过滤，否则匹配 provider_id 前缀。
    """
    report = UsageReport(since=since)
    db_path = next((p for p in ZCODE_DB_CANDIDATES if p.exists()), None)
    if db_path is None:
        report.warnings.append("未找到 ZCode 数据库（~/.zcode/cli/db/db.sqlite）")
        return report
    report.source = str(db_path)

    cutoff_ms: int | None = None
    if since is not None:
        cutoff_ms = int(since.timestamp() * 1000)

    conn = _open_db(db_path, report.warnings)
    try:
        query = """
            SELECT provider_id, model_id, status,
                   input_tokens, output_tokens, reasoning_tokens,
                   cache_creation_input_tokens, cache_read_input_tokens,
                   COALESCE(duration_ms, 0)
            FROM model_usage
            WHERE COALESCE(completed_at, started_at) >= COALESCE(?, 0)
        """
        params: list = [cutoff_ms]
        agg: dict[tuple[str, str], ModelUsageRow] = {}

        cur = conn.execute(query, params)
        for (provider, model_id, status, inp, out, reason,
             cache_w, cache_r, dur_ms) in cur:
            if provider_prefixes is not None and provider_prefixes:
                if not any(str(provider or "").startswith(p)
                           for p in provider_prefixes):
                    continue
            key_model = normalize_model(model_id or "unknown")
            key = (str(provider or "unknown"), key_model)
            row = agg.get(key)
            if row is None:
                row = agg[key] = ModelUsageRow(
                    model=key_model, provider=str(provider or "unknown"))
            # metrik 归一化：input_tokens 已含 cache read，拆回未缓存部分
            cache_r = max(0, int(cache_r or 0))
            cache_w = max(0, int(cache_w or 0))
            inp = max(0, int(inp or 0))
            out = max(0, int(out or 0))
            row.input_uncached += max(0, inp - cache_r - cache_w)
            row.cache_read += cache_r
            row.cache_write += cache_w
            row.output += out
            row.reasoning += max(0, int(reason or 0))
            row.duration_ms += int(dur_ms or 0)
            if status == "completed":
                row.requests += 1
            else:
                row.errors += 1

        report.rows = sorted(agg.values(),
                             key=lambda r: r.processed, reverse=True)
        report.total_requests = sum(r.requests for r in report.rows)
    finally:
        conn.close()
    return report
