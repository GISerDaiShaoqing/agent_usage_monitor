"""Codex CLI 用量数据源（可选）：只读解析 ~/.codex/sessions 的 rollout 会话记录。

Codex（Windows 原生安装）每个会话写一条
~/.codex/sessions/YYYY/MM/DD/rollout-<时间>-<uuid>.jsonl：
- token_usage_record（codex 0.16x+）每条对应一次请求，payload.usage 为
  该次 token 消耗，顶层 timestamp 用于按时间过滤；
- 老版本没有该记录，退回 event_msg/token_count：info.total_token_usage
  为会话累计，先把相邻事件做差还原每次请求的增量再统计；
- 模型名取该记录之前最近的 turn_context（payload.model），会话中途
  /model 切换据此逐段归属。
provider 记为 "codex"。responses API 的 input 含缓存，与 metrik 同口径：
input_uncached = input_tokens - cached_input_tokens - cache_write_input_tokens。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..config import CODEX_SESSION_CANDIDATES
from .zcode_reader import ModelUsageRow, UsageReport

_USAGE_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens",
)

# 文件级 mtime 过滤余量：长会话跨天写入，晚于会话开始
_MTIME_MARGIN = 48 * 3600


def _add_usage(row: ModelUsageRow, usage: dict) -> None:
    inp = max(0, int(usage.get("input_tokens") or 0))
    cache_r = max(0, int(usage.get("cached_input_tokens") or 0))
    cache_w = max(0, int(usage.get("cache_write_input_tokens") or 0))
    row.input_uncached += max(0, inp - cache_r - cache_w)
    row.cache_read += cache_r
    row.cache_write += cache_w
    row.output += max(0, int(usage.get("output_tokens") or 0))
    row.reasoning += max(0, int(usage.get("reasoning_output_tokens") or 0))


def _row_for(agg: dict, model: str) -> ModelUsageRow:
    key = ("codex", model)
    row = agg.get(key)
    if row is None:
        row = agg[key] = ModelUsageRow(model=model, provider="codex")
    return row


def _parse_ts(rec: dict) -> datetime | None:
    raw = rec.get("timestamp")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _parse_rollout(fp: Path, since: datetime | None,
                   agg: dict[tuple[str, str], ModelUsageRow],
                   warnings: list[str]) -> None:
    """单条 rollout：token_usage_record 优先，无则 token_count 做差。"""
    model = ""
    prev_total: dict | None = None
    saw_record = False
    try:
        fh = fp.open(encoding="utf-8", errors="replace")
    except OSError as exc:
        warnings.append(f"Codex 会话文件打开失败（{exc}）")
        return
    with fh:
        try:
            for line in fh:
                # 廉价预过滤：会话行包含 assistant 全文，避免整文件逐行 json 解析
                if ("token_usage_record" not in line
                        and '"token_count"' not in line
                        and '"turn_context"' not in line):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                rtype = rec.get("type")
                if rtype == "turn_context":
                    m = str((rec.get("payload") or {}).get("model") or "")
                    if m:
                        model = m
                elif rtype == "token_usage_record":
                    saw_record = True
                    ts = _parse_ts(rec)
                    usage = (rec.get("payload") or {}).get("usage") or {}
                    if not usage:
                        continue
                    if since is None or ts is None or ts >= since:
                        row = _row_for(agg, model or "unknown")
                        _add_usage(row, usage)
                        row.requests += 1
                elif rtype == "event_msg" and (rec.get("payload") or {}).get(
                        "type") == "token_count":
                    if saw_record:      # 新版本：token_usage_record 已逐请求计数
                        continue
                    info = (rec.get("payload") or {}).get("info") or {}
                    total = info.get("total_token_usage")
                    if not isinstance(total, dict):
                        continue
                    base = prev_total or {}
                    delta = {f: max(0, int(total.get(f) or 0) - int(base.get(f) or 0))
                             for f in _USAGE_FIELDS}
                    ts = _parse_ts(rec)
                    prev_total = total
                    if since is None or ts is None or ts >= since:
                        row = _row_for(agg, model or "unknown")
                        _add_usage(row, delta)
                        row.requests += 1
        except (json.JSONDecodeError, OSError) as exc:
            warnings.append(f"Codex 会话解析中断（{fp.name}: {exc}）")


def read_codex_usage(
    since: datetime | None = None,
    sessions_dir: Path | None = None,
) -> UsageReport:
    """解析 rollout 会话，按模型聚合。since=None = 全部历史。

    sessions_dir 参数供测试注入，缺省用 config 的候选目录。
    """
    report = UsageReport(since=since)
    if sessions_dir is None:
        sessions_dir = next(
            (p for p in CODEX_SESSION_CANDIDATES if p.is_dir()), None)
    if sessions_dir is None or not sessions_dir.is_dir():
        report.warnings.append(
            "未找到 Codex 会话目录（~/.codex/sessions），跳过 Codex 数据源")
        return report
    report.source = str(sessions_dir)

    cutoff_ts = since.timestamp() if since else None
    try:
        files = sorted(sessions_dir.rglob("rollout-*.jsonl"))
    except OSError as exc:
        report.warnings.append(f"Codex 会话目录不可读（{exc}）")
        return report

    agg: dict[tuple[str, str], ModelUsageRow] = {}
    for fp in files:
        try:
            mtime = fp.stat().st_mtime
        except OSError:
            continue
        if cutoff_ts is not None and mtime < cutoff_ts - _MTIME_MARGIN:
            continue
        _parse_rollout(fp, since, agg, report.warnings)

    report.rows = sorted(agg.values(),
                         key=lambda r: r.processed, reverse=True)
    report.total_requests = sum(r.requests for r in report.rows)
    return report
