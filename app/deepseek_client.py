"""DeepSeek 账户余额客户端（DSH 凭据复用，零配置）。

DSH（DeepSeek Harness）登录平台账户后把 grant token 存在
`~/.dsh/.credentials.yaml`（records."deepseek-account-platform/default"
payload.{token, issuer}）。DeepSeek 账户页余额与 DSH app 同源：

    GET <issuer>/api/v0/users/get_user_summary
    头: x-dsh-auth-token: <grant>            # 非 Bearer（DSH 内部协议）

响应: {"code","msg","data":{"biz_code","biz_data":{
    "normal_wallets": [{"currency","balance","token_estimation"}],   # 充值钱包
    "bonus_wallets":  [{"currency","balance"}],                       # 赠币钱包
    "total_costs":    [{"currency","amount"}],}}}
balance/amount 为平台 Web 同款高精度十进制串（big.js 语法），展示量化 2 位。

纯只读：不写任何 DSH / 平台文件，不持久化 token。
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx

from .config import dsh_home
from .zen_client import QuotaSnapshot, WindowUsage

SUMMARY_PATH = "/api/v0/users/get_user_summary"
_FALLBACK_ORIGIN = "https://platform.deepseek.com"

_CURRENCY_GLYPH = {"CNY": "¥", "USD": "$", "EUR": "€"}

# 凭据候选 key：新契约 /default；老约定（平台根 key）兜底
_GRANT_RECORD_IDS = (
    "deepseek-account-platform/default",
    "deepseek-account-platform",
)


def _unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _scan_grant(raw: str, record_ids: tuple[str, ...]) -> tuple[str, str]:
    """无 yaml 依赖的极小扫描：定位 record 键块内 6 缩进的 token/issuer。"""
    lines = raw.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^  ([\w./-]+):\s*$", line)
        if not m or m.group(1) not in record_ids:
            continue
        token = issuer = ""
        for follow in lines[i + 1:]:
            if follow and not follow.startswith("    "):
                break                       # 离开该 record 块
            fm = re.match(r"^\s{6}(token|issuer):\s*(\S.*)$", follow)
            if fm:
                if fm.group(1) == "token":
                    token = _unquote(fm.group(2))
                else:
                    issuer = _unquote(fm.group(2))
        if token:
            return token, issuer
    return "", ""


def discover_dsh_grant() -> tuple[str, str]:
    """从 DSH 本地凭据读平台 grant token。返回 (token, issuer 原文)。

    token 是用户自己账户的凭据，仅供余额端点复用；解析失败返回 ("", "")。"""
    path = dsh_home() / ".credentials.yaml"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", ""
    try:                                    # 有 yaml 库则优先结构化解析
        import yaml                         # type: ignore
        data = yaml.safe_load(raw) or {}
        records = data.get("records") or {}
        for rid in _GRANT_RECORD_IDS:
            payload = ((records.get(rid) or {}).get("payload") or {})
            token = str(payload.get("token") or "")
            if token:
                return token, str(payload.get("issuer") or "")
    except (ImportError, ValueError, AttributeError):
        pass
    return _scan_grant(raw, _GRANT_RECORD_IDS)


def _derive_origin(issuer: str) -> str:
    m = re.match(r"^\s*(https?://[^/]+)", issuer or "")
    return m.group(1) if m else _FALLBACK_ORIGIN


def _fmt_money(entry: dict, total_cost: str | None) -> str:
    """{"currency":"CNY","balance":"12.3456…"} → "¥12.35（累计 ¥45.67）"。"""
    currency = str(entry.get("currency") or "CNY")
    glyph = _CURRENCY_GLYPH.get(currency, f"{currency} ")
    try:
        amount = Decimal(str(entry.get("balance") or "0")).quantize(
            Decimal("0.01"))
    except InvalidOperation:
        amount = Decimal("0.00")
    note = f"{glyph}{amount}"
    if total_cost is not None:
        try:
            cost = Decimal(total_cost).quantize(Decimal("0.01"))
            note += f"（累计 {glyph}{cost}）"
        except InvalidOperation:
            pass
    return note


def _build_windows(biz: dict) -> dict[str, WindowUsage]:
    windows: dict[str, WindowUsage] = {}
    total_cost = None
    costs = biz.get("total_costs")
    if isinstance(costs, list) and costs and isinstance(costs[0], dict):
        total_cost = str(costs[0].get("amount") or "")
    for name, label, key in (("balance:main", "DS 账户余额（主钱包）",
                              "normal_wallets"),
                             ("balance:bonus", "DS 赠币钱包", "bonus_wallets")):
        entries = biz.get(key)
        if not isinstance(entries, list) or not entries \
                or not isinstance(entries[0], dict):
            continue
        windows[name] = WindowUsage(
            name=name, label=label, status="ok",
            used_percent=0.0, resets_at=None,
            note=_fmt_money(entries[0], total_cost
                            if key == "normal_wallets" else None))
    return windows


async def fetch_quota(api_key: str = "", timeout: float = 20.0) -> QuotaSnapshot:
    """拉取 DeepSeek 账户余额。api_key 参数与注册表签名对齐，不使用。"""
    now = datetime.now(timezone.utc)
    token, issuer = discover_dsh_grant()
    if not token:
        return QuotaSnapshot(
            windows={}, fetched_at=now,
            error="未找到 DSH 平台凭据：先在 DeepSeek Harness 里登录账户"
                  "（~/.dsh/.credentials.yaml），或切回其他套餐")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                _derive_origin(issuer) + SUMMARY_PATH,
                headers={"x-dsh-auth-token": token,
                         "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        return QuotaSnapshot(windows={}, fetched_at=now, error=f"网络错误: {exc}")

    if resp.status_code in (401, 403):
        return QuotaSnapshot(
            windows={}, fetched_at=now,
            error=f"HTTP {resp.status_code}：DSH 平台凭据失效，"
                  "请在 DeepSeek Harness 重新登录账户")
    if resp.status_code != 200:
        masked = re.sub(r"[A-Za-z0-9_+/=]{16,}", "<token>",
                        resp.text[:200])
        return QuotaSnapshot(windows={}, fetched_at=now,
                             error=f"HTTP {resp.status_code}: {masked}")

    try:
        body = resp.json()
    except ValueError:
        return QuotaSnapshot(windows={}, fetched_at=now, error="响应非 JSON")
    data = body.get("data")
    data = data if isinstance(data, dict) else {}
    biz_code = data.get("biz_code", body.get("code"))
    try:
        ok_code = int(biz_code) == 0
    except (TypeError, ValueError):
        ok_code = False
    if not ok_code:
        msg = str(data.get("biz_msg") or body.get("msg") or "")
        return QuotaSnapshot(windows={}, fetched_at=now,
                             error=f"平台返回错误 {biz_code}: {msg[:80]}")
    biz_data = data.get("biz_data")
    biz_data = biz_data if isinstance(biz_data, dict) else {}
    windows = _build_windows(biz_data)
    if not windows:
        return QuotaSnapshot(windows={}, fetched_at=now,
                             error="响应里没有钱包数据（账户未充值或协议变更）")
    return QuotaSnapshot(windows=windows, fetched_at=now)


async def test_connection() -> tuple[bool, str]:
    """设置页连接测试：返回 (成功?, 描述)。"""
    snap = await fetch_quota()
    if snap.ok:
        parts = [f"{w.label} {w.note}" for w in snap.windows.values()]
        return True, "连接成功：" + "，".join(parts)
    return False, snap.error or "未知错误"
