"""DSH（DeepSeek Harness）用量数据源（第 5）：只读解析其本地会话记录。

DSH 有两份记录，双路合账、按会话 id 防双计（同会话事件日志优先）：

1. **事件日志**（metrik 主路径，逐请求粒度）：
   `<DSH_HOME>/sessions/--<归一化 cwd>--/<session id>/session[.vN].jsonl[.zstd]`
   - 首行 header：`{"type":"session","id","createdAt","cwd","isSeeded",...}`，
     其后事件为 `{"type","seq","time(ms)","data"}`；
   - 计费事件 type ∈ `assistant/message` / `assistant/attempt` /
     `compaction/summary`：usage 取 `data.usage`，缺省取 `data.stream` 里
     最后一个 `{"chunk":{"type":"usage",...}}`；
   - 同一代只取版本最高的文件（session.jsonl=v0，session.vN.jsonl=数字代；
     tmp 碎片与非规范名忽略），防同一会话多代重放双计；
   - fork 前缀剔除：`session/end-seed {inherited:true}` 及 header 里遗留
     `seedLength` 之前的 seq 属于父会话，不计入本会话；
   - 模型归属：`data.message.source.provider/model`，被
     `source.replayState.response.responseModel` 覆盖，再回溯最近一次
     `request/header` 的 `route_provider`/`route_model`，最终兜底 unknown。
2. **投影**（DSH 桌面版全量覆盖、刷新及时；老会话可能只剩它）：
   `<DSH_HOME>/storages/session_projcache/sessions/session-<uuid>.json`
   - `record.rows.tokenUsage.val.totals` 为**会话 lifetime 累计**：
     {uncachedInputTokens, outputTokens, cacheReadTokens, cacheWriteTokens}
     （`sessionStats.decodeTokens == totals.outputTokens` 交叉验证过），
     reasoning 不另记，恒 0；
   - 模型归属取 `record.rows.modelSelection.val.lastUsed.provider/model`
     （此会话粗粒度：会话中切模型只记最后一次）；
   - 会话级时间取 `sessionListMetadata.lastPromptAt`（兜底
     `timeContext.lastMessageTime`，epoch ms）；跨界的会话按该时间整段计入。

只记 token 与模型，不落内容。DSH_HOME 环境变量可重定向（metrik 同款）。
zstd 解压可选：≥3.14 用标准库 compression.zstd，否则第三方 zstandard，
都没有则跳过 .zstd 文件并告警（投影路径不受影响）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ..config import dsh_home
from ..pricing import normalize_model
from .zcode_reader import ModelUsageRow, UsageReport

DEFAULT_PROVIDER = "deepseek-account"

# 计费事件类型（metrik src-tauri/src/adapters/dsh.rs 同款三类）
_BILL_TYPES = ("assistant/message", "assistant/attempt", "compaction/summary")
_REASONING_KEYS = ("reasoningOutputTokens", "reasoningTokens", "reasoning")

# 会话文件名规范：session.jsonl=v0；session.vN.jsonl=v1、v2…（前导零的
# v04 之类非规范名与 tmp 碎片一律忽略，metrik 同款防重放规则）
_FILENAME_RE = re.compile(
    r"^session(?:\.v([1-9]\d*))?\.jsonl(?:\.zstd)?$")
_TMP_NAME_RE = re.compile(
    r"^session[.\w]*\.jsonl(?:\.zstd)?\.(tmp|tmp\..*)$")


def _decompress_text(fp: Path) -> str | None:
    """读为文本；.zstd 走可选解压（std 3.14+ 或 zstandard 包），失败 None。"""
    if fp.name.endswith(".zstd"):
        try:
            raw = fp.read_bytes()
        except OSError:
            return ""
        try:                                # Python ≥3.14 标准库
            from compression import zstd as _zstd
            with _zstd.open(fp, "rb") as fh:
                return fh.read().decode("utf-8", "replace")
        except ImportError:
            pass
        try:
            import zstandard as _zs
            with open(fp, "rb") as fh:
                return _zs.ZstdDecompressor().stream_reader(fh)\
                    .read().decode("utf-8", "replace")
        except ImportError:
            return None                     # 调用方告警跳过
    try:
        return fp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _add_usage(row: ModelUsageRow, rec: dict) -> bool:
    """把一条 usage dict 累进 row；数值非法或缺字段按 0 计。"""
    try:
        inp = max(0, int(rec.get("inputTokens") or 0))
        cache_r = max(0, int(rec.get("cacheReadTokens") or 0))
        cache_w = max(0, int(rec.get("cacheWriteTokens") or 0))
        out = max(0, int(rec.get("outputTokens") or 0))
    except (TypeError, ValueError):
        return False
    reason = 0
    for key in _REASONING_KEYS:
        try:
            reason = max(reason, max(0, int(rec.get(key) or 0)))
        except (TypeError, ValueError):
            pass
    row.input_uncached += max(0, inp - cache_r - cache_w)
    row.cache_read += cache_r
    row.cache_write += cache_w
    row.output += out
    row.reasoning += reason
    row.requests += 1
    return True


def _row_for(agg: dict, provider: str, model: str) -> ModelUsageRow:
    key = (provider, model)
    row = agg.get(key)
    if row is None:
        row = agg[key] = ModelUsageRow(
            model=model, provider=provider, source="dsh")
    return row


def _iter_event_logs(sessions_root: Path) -> list[tuple[str, Path]]:
    """sessions/ 下逐会话目录给出 (session_id, 该代最高的事件日志路径)。"""
    out: list[tuple[str, Path]] = []
    if not sessions_root.is_dir():
        return out
    for bucket in sorted(sessions_root.iterdir()):
        if not bucket.is_dir():
            continue
        for sess in sorted(bucket.iterdir()):
            if not sess.is_dir() or not re.fullmatch(
                    r"[\w-]+|~\w+", sess.name):
                continue
            best: tuple[int, Path] | None = None
            try:
                names = sorted(sess.iterdir())
            except OSError:
                continue
            for fp in names:
                if not fp.is_file() or _TMP_NAME_RE.match(fp.name):
                    continue
                m = _FILENAME_RE.match(fp.name)
                if not m:
                    continue
                gen = int(m.group(1) or 0)
                if best is None or gen > best[0]:
                    best = (gen, fp)
            if best is not None:
                out.append((sess.name, best[1]))
    return out


def _parse_event_log(fp: Path, cutoff_ts_ms: int | None,
                     agg: dict[tuple[str, str], ModelUsageRow],
                     warnings: list[str]) -> tuple[str, bool]:
    """解析单个事件日志，进 agg；返回 (session_id, 是否有计费样本)。"""
    text = _decompress_text(fp)
    if text is None:
        warnings.append(
            f"DSH 事件日志 {fp.name} 为 zstd 压缩且本机无 "
            f"compression.zstd（≥3.14）/zstandard 包，已跳过")
        return fp.parent.name, False
    sid = ""
    inherited_upto = -1
    used = False
    route_provider = route_model = ""
    seen_seq: set[int] = set()
    try:
        events: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                events.append(ev)
        # 第一遍：header 与 fork 界定（end-seed 声明晚于被剔除事件出现，
        # 必须先扫完 seedLength / end-seed 再过计费事件）
        for ev in events:
            etype = str(ev.get("type") or "")
            if etype == "session":
                sid = str(ev.get("id") or "") or sid
                try:
                    inherited_upto = max(
                        inherited_upto, int(ev.get("seedLength") or 0))
                except (TypeError, ValueError):
                    pass
                continue
            if etype != "session/end-seed":
                continue
            try:
                seq_i = int(ev.get("seq"))
            except (TypeError, ValueError):
                continue
            if (ev.get("data") or {}).get("inherited"):
                inherited_upto = max(inherited_upto, seq_i)
        for ev in events:
            etype = str(ev.get("type") or "")
            seq = ev.get("seq")
            try:
                seq_i = int(seq)
            except (TypeError, ValueError):
                seq_i = None
            if etype == "session/end-seed":
                continue
            if etype == "request/header":
                data = ev.get("data") or {}
                route_provider = str(data.get("route_provider")
                                     or route_provider)
                route_model = str(data.get("route_model") or route_model)
                continue
            if etype not in _BILL_TYPES:
                continue
            # fork 前缀：父会话内容，不重复计账
            if seq_i is not None and seq_i <= inherited_upto:
                continue
            if seq_i is not None:
                if seq_i in seen_seq:       # 同点重放（替换样本）不叠加
                    continue
                seen_seq.add(seq_i)
            ts = ev.get("time")
            try:
                ts_ms = int(ts)
            except (TypeError, ValueError):
                ts_ms = None
            if cutoff_ts_ms is not None and (
                    ts_ms is None or ts_ms < cutoff_ts_ms):
                continue
            data = ev.get("data") or {}
            usage = data.get("usage")
            if not isinstance(usage, dict):
                stream = data.get("stream")
                if isinstance(stream, list):
                    for chunk in reversed(stream):
                        c = (chunk or {}).get("chunk") \
                            if isinstance(chunk, dict) else None
                        if isinstance(c, dict) and c.get("type") == "usage":
                            usage = c
                            break
            if not isinstance(usage, dict):
                continue
            src = ((data.get("message") or {}).get("source") or {}) \
                if isinstance(data.get("message"), dict) else {}
            replay = ((src.get("replayState") or {}).get("response") or {}
                      if isinstance(src.get("replayState"), dict) else {})
            provider = str(src.get("provider") or route_provider
                           or DEFAULT_PROVIDER)
            model = str(replay.get("responseModel") or src.get("model")
                        or route_model or "unknown")
            row = _row_for(agg, provider, normalize_model(model))
            if _add_usage(row, usage):
                used = True
    except (OSError, UnicodeError):
        return sid, used
    return sid, used


def _projection_ts_ms(rec: dict) -> int | None:
    rows = rec.get("rows") or {}
    meta = ((rows.get("sessionListMetadata") or {}).get("val") or {})
    tc = ((rows.get("timeContext") or {}).get("val") or {})
    for key in ("lastPromptAt", "lastMessageTime"):
        v = (meta if key == "lastPromptAt" else tc).get(key)
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return None


def _total_int(totals: dict, key: str) -> int:
    try:
        return max(0, int(totals.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def read_dsh_usage(
    since: datetime | None = None,
    dsh_root: Path | None = None,
) -> UsageReport:
    """读取 DSH 本地记录（事件日志 + 投影），按 (provider, 模型) 聚合。

    同 session id 双处都有时事件日志优先、投影跳过；事件日志存在但不含
    计费样本（如空会话/代滚动后内容缺失）时投影兜底。
    dsh_root 参数供测试注入，缺省用 config.dsh_home()。
    """
    report = UsageReport(since=since)
    home = dsh_root or dsh_home()
    if not home.is_dir():
        report.warnings.append(
            "未找到 DSH 数据目录（~/.dsh，DeepSeek Harness），"
            "跳过 DSH 数据源")
        return report
    report.source = str(home)

    cutoff_ts_ms: int | None = None
    if since is not None:
        cutoff_ts_ms = int(since.timestamp() * 1000)

    agg: dict[tuple[str, str], ModelUsageRow] = {}
    covered: set[str] = set()          # 事件日志里确认有计费样本的会话
    for sid, fp in _iter_event_logs(home / "sessions"):
        event_sid, used = _parse_event_log(
            fp, cutoff_ts_ms, agg, report.warnings)
        if used:
            covered.add(event_sid or sid)

    proj_dir = home / "storages" / "session_projcache" / "sessions"
    if proj_dir.is_dir():
        for fp in sorted(proj_dir.glob("session-*.json")):
            sid = fp.stem[len("session-"):]
            if sid and sid in covered:
                continue
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                report.warnings.append(f"DSH 投影 {fp.name} 解析失败，跳过")
                continue
            rec = data.get("record") or {}
            rows = rec.get("rows") or {}
            totals = ((rows.get("tokenUsage") or {}).get("val") or {})\
                .get("totals") or {}
            if not any(_total_int(totals, k) for k in
                       ("uncachedInputTokens", "outputTokens",
                        "cacheReadTokens", "cacheWriteTokens")):
                continue                # 空白/播种会话
            ts_ms = _projection_ts_ms(rec)
            if cutoff_ts_ms is not None and (
                    ts_ms is None or ts_ms < cutoff_ts_ms):
                continue
            last = ((rows.get("modelSelection") or {}).get("val") or {})\
                .get("lastUsed") or {}
            if not isinstance(last, dict):
                last = {}
            model = normalize_model(str(last.get("model") or "unknown"))
            provider = str(last.get("provider") or DEFAULT_PROVIDER)
            row = _row_for(agg, provider, model)
            row.input_uncached += _total_int(totals, "uncachedInputTokens")
            row.cache_read += _total_int(totals, "cacheReadTokens")
            row.cache_write += _total_int(totals, "cacheWriteTokens")
            row.output += _total_int(totals, "outputTokens")
            stats = ((rows.get("sessionStats") or {}).get("val") or {})
            row.requests += _total_int(stats, "steps")
            row.duration_ms += _total_int(stats, "llmMs")

    report.rows = sorted(agg.values(), key=lambda r: r.processed,
                         reverse=True)
    report.total_requests = sum(r.requests for r in report.rows)
    return report
