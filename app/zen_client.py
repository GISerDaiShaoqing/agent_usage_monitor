"""OpenCode Go 官方额度端点客户端。

GET https://opencode.ai/zen/go/v1/usage  (Authorization: Bearer <key>)
响应: {"usage": {"rolling"|"weekly"|"monthly": {"status", "percent", "resetsAt"}}}
percent 为已用百分比，展示层换算成剩余。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import httpx

USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
MODELS_URL = "https://opencode.ai/zen/go/v1/models"

WindowName = Literal["rolling", "weekly", "monthly"]
WINDOW_LABELS: dict[str, str] = {"rolling": "5 小时", "weekly": "每周", "monthly": "每月"}
WINDOW_ORDER: tuple[WindowName, ...] = ("rolling", "weekly", "monthly")
# 旧展示顺序常量（兼容保留）；新代码请用 ordered_windows()
ALL_WINDOW_ORDER: tuple[str, ...] = ("grant", "rolling", "weekly", "monthly")


def fmt_units(n: int | float) -> str:
    """token 数精简展示：300000000 → 3亿，130000000 → 1.3亿，8500万 → 8500万。"""
    n = int(n)
    if n >= 100_000_000:
        s = f"{n / 100_000_000:.2f}"
        unit = "亿"
    elif n >= 10_000:
        s = f"{n / 10_000:.1f}"
        unit = "万"
    else:
        return str(n)
    return s.rstrip("0").rstrip(".") + unit


def ordered_windows(windows: dict[str, WindowUsage]) -> list[WindowUsage]:
    """展示顺序：grant（含 grant:xxx 分模型窗口）→ rolling/weekly/monthly → 其余。"""
    idx = {n: i for i, n in enumerate(WINDOW_ORDER)}

    def rank(item: tuple[str, WindowUsage]) -> tuple[int, object]:
        name = item[0]
        if name == "grant" or name.startswith("grant:"):
            return (0, name)
        if name in idx:
            return (1, idx[name])
        return (2, name)

    return [w for _, w in sorted(windows.items(), key=rank)]


@dataclass
class WindowUsage:
    name: str
    label: str
    status: str          # "ok" | "rate-limited" | 其他
    used_percent: float  # 已用 0-100
    resets_at: datetime | None
    units_remaining: int | None = None  # token 池剩余绝对值（Start Plan 分模型窗口有值）

    @property
    def remaining_percent(self) -> float:
        return max(0.0, min(100.0, 100.0 - self.used_percent))

    @property
    def is_limited(self) -> bool:
        return self.status == "rate-limited"

    @property
    def is_low(self) -> bool:
        return self.remaining_percent <= 15.0


@dataclass
class QuotaSnapshot:
    windows: dict[str, WindowUsage]  # key: rolling/weekly/monthly
    fetched_at: datetime
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.windows)


def _parse_window(name: str, raw: dict) -> WindowUsage:
    percent = raw.get("percent", 0)
    if isinstance(percent, str):
        try:
            percent = float(percent)
        except ValueError:
            percent = 0.0
    resets = None
    raw_resets = raw.get("resetsAt")
    if raw_resets:
        try:
            resets = datetime.fromisoformat(raw_resets.replace("Z", "+00:00"))
        except ValueError:
            pass
    return WindowUsage(
        name=name,
        label=WINDOW_LABELS.get(name, name),
        status=str(raw.get("status", "unknown")),
        used_percent=float(percent),
        resets_at=resets,
    )


async def fetch_quota(api_key: str, timeout: float = 15.0) -> QuotaSnapshot:
    now = datetime.now(timezone.utc)
    if not api_key:
        return QuotaSnapshot(windows={}, fetched_at=now,
                             error="未找到 API key，请到设置页配置")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                USAGE_URL,
                headers={"Authorization": f"Bearer {api_key}",
                         "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        return QuotaSnapshot(windows={}, fetched_at=now, error=f"网络错误: {exc}")

    if resp.status_code == 401:
        return QuotaSnapshot(windows={}, fetched_at=now,
                             error="401 Unauthorized：API key 无效或已过期")
    if resp.status_code == 403:
        return QuotaSnapshot(windows={}, fetched_at=now,
                             error="403 Forbidden：该 key 无 OpenCode Go 订阅权限")
    if resp.status_code != 200:
        return QuotaSnapshot(windows={}, fetched_at=now,
                             error=f"HTTP {resp.status_code}: {resp.text[:200]}")

    try:
        usage = resp.json()["usage"]
    except (KeyError, ValueError):
        return QuotaSnapshot(windows={}, fetched_at=now, error="响应格式无法解析")

    windows = {name: _parse_window(name, usage[name])
               for name in WINDOW_ORDER if name in usage}
    return QuotaSnapshot(windows=windows, fetched_at=now)


async def test_connection(api_key: str) -> tuple[bool, str]:
    """设置页测试连接：返回 (成功?, 描述)。"""
    snap = await fetch_quota(api_key)
    if snap.ok:
        parts = [f"{w.label} 剩余 {w.remaining_percent:.0f}%"
                 for w in snap.windows.values()]
        return True, "连接成功：" + "，".join(parts)
    return False, snap.error or "未知错误"
