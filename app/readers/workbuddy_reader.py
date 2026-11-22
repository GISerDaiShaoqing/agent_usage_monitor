"""WorkBuddy（腾讯 CodeBuddy）本地用量数据源（第 6）：只读解析其 CLI 会话转录。

数据源：~/.workbuddy/projects/**/*.jsonl 与 ~/.codebuddy/projects/**/*.jsonl
（同一格式；桌面端把内置 CLI 的 home 重定向到 ~/.workbuddy）。
CODEBUDDY_CONFIG_DIR 环境变量重定向时优先（config.workbuddy_project_roots）。

行格式（metrik workbuddy.rs 真机核对）：
    {"id", "timestamp"(epoch 毫秒), "type", "role", "status", "sessionId",
     "cwd", "message": {"model", "usage": {...}},
     "providerData": {"messageId", "traceId", ...}}

口径陷阱（metrik 对真实转录的实验结论，逐条落进实现）：
  - inputTokens 与 input_tokens 都是**含缓存**的 prompt 总量
    （total_tokens == input_tokens + output_tokens，cache_read 在其内）
    ——一律减去 cache_read；cachedMissTokens（已是未缓存口径）存在时优先；
  - function_call 行带 usage 但 status 恒为 null——status 过滤只约束
    message 行（生成完成的 assistant 消息才被改写为 completed），
    对 function_call 行不设限，否则会丢掉大头的工具调用用量；
  - 同一身份（providerData.messageId → traceId → 顶层 id，叠加 sessionId
    限定作用域）渐进更新，分量取最大值——与 Claude 系适配器同构；
  - 老 workbuddy.db（SQLite）不解析：其 used 是上下文占用而非 token 累计。

零侵入：只读，绝不写入。只记 token 与模型名，不含对话内容。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..config import workbuddy_project_roots
from ..pricing import normalize_model
from .zcode_reader import ModelUsageRow, UsageReport

DEFAULT_PROVIDER = "workbuddy"

_INPUT_KEYS = ("inputTokens", "input_tokens")
_READ_KEYS = ("cacheReadTokens", "cacheReadInputTokens",
              "cache_read_input_tokens", "cache_read_tokens")
_WRITE_KEYS = ("cacheCreationTokens", "cache_creation_input_tokens",
               "cacheWriteTokens", "cache_creation_tokens")
_REASON_KEYS = ("reasoningTokens", "reasoning_output_tokens",
                "reasoningOutputTokens")
_OUTPUT_KEYS = ("outputTokens", "output_tokens")


def _num(v) -> int:
    if v is None or isinstance(v, bool):
        return 0
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _first_num(usage: dict, keys: tuple[str, ...]) -> int | None:
    """按序取第一个合法（非负、可转数）的键值；都没有返回 None。"""
    for k in keys:
        v = usage.get(k)
        if v is None or isinstance(v, bool):
            continue
        try:
            n = int(float(v))
        except (TypeError, ValueError):
            continue
        if n >= 0:
            return n
    return None


def _ts_ms(value) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ms = float(value)
        if ms < 1e11:                       # 秒
            ms *= 1000
        return int(ms) if ms >= 0 else None
    return None


def _components(usage: dict) -> tuple[int, int, int, int, int]:
    """usage → (未缓存输入, 缓存读, 缓存写, 输出, 推理)。

    cachedMissTokens 优先（已是未缓存口径）；否则 input 别名一律视为
    含缓存总量，减去缓存读/写。"""
    miss = _num(usage.get("cachedMissTokens"))
    read = _first_num(usage, _READ_KEYS) or 0
    write = _first_num(usage, _WRITE_KEYS) or 0
    out = _first_num(usage, _OUTPUT_KEYS) or 0
    inp = _first_num(usage, _INPUT_KEYS)
    if miss > 0:
        inp_uncached = miss
    elif inp is not None:
        inp_uncached = max(0, inp - read - write)
    else:
        inp_uncached = 0
    reason = _first_num(usage, _REASON_KEYS) or 0
    return inp_uncached, read, write, out, reason


def read_workbuddy_usage(since: datetime | None = None,
                         roots: list[Path] | None = None,
                         pricing=None) -> UsageReport:
    """解析 WorkBuddy/CodeBuddy 会话转录，按 (provider, 模型) 聚合。
    roots 参数供测试注入，缺省用 config.workbuddy_project_roots()。"""
    report = UsageReport(since=since)
    root_list = roots or workbuddy_project_roots()
    existing = [r for r in root_list if r.is_dir()]
    if not existing:
        report.warnings.append(
            "未找到 WorkBuddy/CodeBuddy 会话转录（~/.workbuddy/projects），"
            "跳过 WorkBuddy 数据源")
        return report
    report.source = str(existing[0])

    cutoff_ms = int(since.timestamp() * 1000) if since is not None else None
    # 身份 → (模型, ts_ms, (未缓存输入, 缓存读, 缓存写, 输出, 推理))；
    # 同身份渐进更新取分量最大值，跨根目录同名会话自然去重
    identities: dict[tuple[str, str], tuple] = {}
    bad = no_ts = 0
    for root in existing:
        try:
            files = sorted(root.rglob("*.jsonl"))
        except OSError:
            continue
        for fp in files:
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if not isinstance(rec, dict):
                    bad += 1
                    continue
                message = rec.get("message")
                usage = message.get("usage") \
                    if isinstance(message, dict) else None
                if not isinstance(usage, dict) or not usage:
                    continue                # 非用量行（用户消息/工具结果等）
                # status 只约束 message 行；function_call 行 status 恒 null
                status = rec.get("status")
                if status is not None and str(status) != "completed":
                    continue
                ts = _ts_ms(rec.get("timestamp"))
                if ts is None:
                    no_ts += 1
                    if cutoff_ms is not None:
                        continue            # 无时间戳无法过 since，宁缺勿滥
                elif cutoff_ms is not None and ts < cutoff_ms:
                    continue
                pd = rec.get("providerData")
                pd = pd if isinstance(pd, dict) else {}
                model = str((message or {}).get("model") or pd.get("model")
                            or "unknown")
                ident_scope = str(rec.get("sessionId") or fp)
                ident_id = str(pd.get("messageId") or pd.get("traceId")
                               or rec.get("id") or "")
                if not ident_id:
                    ident_id = f"__line{len(identities)}__"
                key = (ident_scope, ident_id)
                comps = _components(usage)
                prev = identities.get(key)
                if prev is None:
                    identities[key] = (normalize_model(model), ts or 0, comps)
                else:
                    m0, ts0, c0 = prev
                    identities[key] = (
                        m0 if m0 != "unknown" else normalize_model(model),
                        max(ts0, ts or 0),
                        tuple(max(a, b) for a, b in zip(c0, comps)))
    agg: dict[tuple[str, str], ModelUsageRow] = {}
    for _key, (model, _ts, comps) in identities.items():
        key = (DEFAULT_PROVIDER, model)
        row = agg.get(key)
        if row is None:
            row = agg[key] = ModelUsageRow(model=model,
                                           provider=DEFAULT_PROVIDER,
                                           source="workbuddy")
        row.requests += 1
        row.input_uncached += comps[0]
        row.cache_read += comps[1]
        row.cache_write += comps[2]
        row.output += comps[3]
        row.reasoning += comps[4]
    report.rows = sorted(agg.values(), key=lambda r: r.processed,
                         reverse=True)
    report.total_requests = sum(r.requests for r in report.rows)
    if bad or no_ts:
        report.warnings.append(
            f"WorkBuddy 转录：{bad} 行损坏、{no_ts} 行缺时间戳，已跳过")
    return report
