"""daiRH 数据源（第 4）：只读解析 daiRH 的 token 用量账本 usage.jsonl。

daiRH（R 包直连客户端）在每次模型请求结束后（含工具循环 round-trip、
起标题、子代理、压缩总结器）向 R_user_dir("daiRH", "data")/usage/usage.jsonl
追加一行 jsonl，只记 token 数，不含对话内容。行 schema（daiRH 侧 R/usage.R）：

    {"ts": "2026-11-07 00:16:09.543",   # 本地时区、毫秒精度、无时区尾缀
     "pid": 27464,                      # daiRH 进程号，忽略
     "source": "daiRH",                 # daiRH 内部调用面标签，忽略
     "model": "glm-5.3-flash",          # 裸模型 id
     "ok": true,                        # 实际恒 true：ellmer 失败请求不触发
                                        # on_request_end（网关也不计费），取消
                                        # 请求 tokens 为 NA 被 daiRH 跳过；
                                        # 保留解析以备将来
     "input": 2176, "output": 7, "cache_read": 1088,
     ["cost": 0.001]}                   # 仅带定价表的端点才有；估算暂不用

口径对齐：cache_read 即缓存命中的输入部分；网关无独立 cache_write，
input_uncached = max(0, input - cache_read) 由本侧计算。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..config import DAIRH_USAGE_CANDIDATES
from ..pricing import normalize_model
from .zcode_reader import ModelUsageRow, UsageReport

DEFAULT_PROVIDER = "opencode-go"


def _ts_ms(value) -> int | None:
    """行内 ts → epoch 毫秒。数字视为 epoch（<1e11 当秒）；字符串走
    fromisoformat（兼容空格分隔与 ISO 的 T；naive 视为本地时区）；
    解析不了返回 None。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ms = float(value)
        if ms < 1e11:                       # epoch 秒
            ms *= 1000
        return int(ms)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.astimezone()            # daiRH 的 ts 无时区尾缀 → 本地时区
        return int(dt.timestamp() * 1000)
    return None


def _int_field(rec: dict, name: str) -> int:
    v = rec.get(name)
    if v is None:
        return 0
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


def read_dairh_usage(
    since: datetime | None = None,
    pricing=None,
) -> UsageReport:
    """逐行解析 daiRH 账本，按 (provider, 模型) 聚合 token 用量。"""
    report = UsageReport(since=since)
    path = next((p for p in DAIRH_USAGE_CANDIDATES if p.exists()), None)
    if path is None:
        report.warnings.append("未找到 daiRH usage.jsonl，跳过 daiRH 数据源")
        return report
    report.source = str(path)

    cutoff_ms: int | None = None
    if since is not None:
        cutoff_ms = int(since.timestamp() * 1000)

    bad = no_ts = 0
    agg: dict[tuple[str, str], ModelUsageRow] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    continue
                if not isinstance(rec, dict):
                    bad += 1
                    continue
                ts_ms = _ts_ms(rec.get("ts"))
                if ts_ms is None:
                    no_ts += 1
                    continue
                if cutoff_ms is not None and ts_ms < cutoff_ms:
                    continue
                model = normalize_model(str(rec.get("model") or "unknown"))
                provider = str(rec.get("provider") or DEFAULT_PROVIDER)
                row = agg.get((provider, model))
                if row is None:
                    row = agg[(provider, model)] = ModelUsageRow(
                        model=model, provider=provider, source="dairh")
                if rec.get("ok", True) is False:
                    row.errors += 1     # 失败请求不进 token，只计次数
                    continue
                inp = _int_field(rec, "input")
                cache_r = _int_field(rec, "cache_read")
                cache_w = _int_field(rec, "cache_write")
                row.input_uncached += max(0, inp - cache_r - cache_w)
                row.cache_read += cache_r
                row.cache_write += cache_w
                row.output += _int_field(rec, "output")
                row.reasoning += _int_field(rec, "reasoning_tokens")
                row.requests += 1
    except OSError as exc:
        report.warnings.append(f"daiRH usage.jsonl 读取失败（{exc}）")
        return report
    report.rows = sorted(agg.values(), key=lambda r: r.processed, reverse=True)
    report.total_requests = sum(r.requests for r in report.rows)
    if bad or no_ts:
        report.warnings.append(
            f"daiRH usage.jsonl：{bad} 行损坏、{no_ts} 行缺时间戳，已跳过")
    return report
