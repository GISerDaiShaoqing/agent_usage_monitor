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
# 展示顺序：grant 为 ZCode Start Plan 单池额度窗口，其余为 OpenCode Go 三窗口
ALL_WINDOW_ORDER: tuple[str, ...] = ("grant", "rolling", "weekly", "monthly")


@dataclass
class WindowUsage:
    name: str
    label: str
    status: str          # "ok" | "rate-limited" | 其他
    used_percent: float  # 已用 0-100
    resets_at: datetime | None

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
