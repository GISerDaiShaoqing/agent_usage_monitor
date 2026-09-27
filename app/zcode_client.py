"""ZCode Start Plan（免费领取的 token）额度客户端。

数据源优先级（纯本地只读优先，无 key 也能工作）：
1. ZCode 桌面端本地日志 ~/.zcode/v2/logs/YYYY-MM-DD.log —— 桌面端的
   [usage-stats] 模块每 ~2 分钟轮询 billing/balance 并把完整响应明文写入
   日志，这里解析其中最新一条，与 ZCode 界面同源同新鲜度。
2. 直接请求 https://zcode.z.ai/api/v1/zcode-plan/billing/balance
   （Bearer JWT，从 ZCode 本地配置自动发现）—— 日志缺失时兜底。
   注意：该端点目前对非桌面端请求返回 3001，属尽力而为的备用路径。

响应里 balances[] 含 total_units / used_units / remaining_units /
expires_at，plans[] 含套餐名（如 "ZCode Weekend Build"）。Start Plan 是
一次性发放的单池额度，聚合成单个 "grant" 窗口展示。
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .config import CONFIG_PATH
from .zen_client import QuotaSnapshot, WindowUsage

BALANCE_URL = "https://zcode.z.ai/api/v1/zcode-plan/billing/balance"
APP_VERSION = "3.14.3"          # 与桌面端日志中观察到的版本一致

ZCODE_V2_DIR = Path.home() / ".zcode" / "v2"
ZCODE_V2_CONFIG = ZCODE_V2_DIR / "config.json"
ZCODE_CREDENTIALS = ZCODE_V2_DIR / "credentials.json"
ZCODE_LOG_DIR = ZCODE_V2_DIR / "logs"

LOG_MARKER = "[usage-stats] billing/balance 请求完成 "
_LOG_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)\]")
_GRANT = "grant"


# ---- 主路径：解析桌面端日志 ----

def _log_candidate_files(days: int = 2) -> list[Path]:
    """今天与昨天的日志（文件名按本地日期滚动）。"""
    now = datetime.now()
    return [ZCODE_LOG_DIR / f"{(now - timedelta(days=i)).strftime('%Y-%m-%d')}.log"
            for i in range(days)]


def parse_latest_balance_log() -> tuple[dict, datetime] | None:
    """从日志中取最新一条 billing/balance 响应。

    返回 (响应 data 部分, 日志时间戳)，找不到返回 None。
    """
    for path in _log_candidate_files():
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        best: tuple[dict, datetime] | None = None
        for line in text.splitlines():
            idx = line.find(LOG_MARKER)
            if idx < 0:
                continue
            ts_match = _LOG_TS_RE.match(line)
            json_start = line.find("{", idx)
            if json_start < 0:
                continue
            try:
                entry = json.loads(line[json_start:])
            except ValueError:
                continue
            ts = (datetime.strptime(ts_match.group(1), "%Y-%m-%d %H:%M:%S.%f")
                  .astimezone() if ts_match else datetime.now().astimezone())
            data = (entry.get("payload") or {}).get("data") or entry.get("data")
            if isinstance(data, dict) and data.get("balances"):
                best = (data, ts)      # 同一文件内越靠后越新
        if best is not None:
            return best
    return None


def _aggregate(data: dict) -> WindowUsage | None:
    """把 balances[] 聚合成单个 grant 窗口；无有效余额返回 None。"""
    balances = data.get("balances") or []
    total = used = 0
    expires: list[int] = []
    for b in balances:
        if not isinstance(b, dict):
            continue
        total += max(0, int(b.get("total_units") or 0))
        used += max(0, int(b.get("used_units") or 0))
        for key in ("expires_at", "period_end"):
            v = b.get(key)
            if isinstance(v, (int, float)) and v > 0:
                expires.append(int(v))
    if total <= 0:
        return None
    names: list[str] = []
    for p in data.get("plans") or []:
        if not isinstance(p, dict):
            continue
        if p.get("name"):
            names.append(str(p["name"]))
        v = p.get("ends_at")
        if isinstance(v, (int, float)) and v > 0 and p.get("status") != "expired":
            expires.append(int(v))
    label = " · ".join(dict.fromkeys(names)) if names else "Start Plan"
    resets = (datetime.fromtimestamp(min(expires), tz=timezone.utc)
              if expires else None)
    return WindowUsage(
        name=_GRANT,
        label=label,
        status="ok",
        used_percent=used / total * 100.0,
        resets_at=resets,
    )


# ---- 兜底：直接请求 balance API ----

def discover_zcode_token() -> tuple[str, str]:
    """发现访问 zcode.z.ai 的 JWT，返回 (token, 来源描述)。

    顺序：v2/config.json 中指向 zcode-plan 网关的 provider 的 apiKey
    → credentials.json 的 zcodejwttoken（桌面端本地加密存储时无法读取，
    跳过）。
    """
    try:
        v2 = json.loads(ZCODE_V2_CONFIG.read_text(encoding="utf-8"))
        for pid, entry in (v2.get("provider") or {}).items():
            entry = entry or {}
            base = str((entry.get("options") or {}).get("baseURL", ""))
            key = (entry.get("options") or {}).get("apiKey") or ""
            if key and ("/zcode-plan" in base or "start-plan" in str(pid)):
                return str(key), f"ZCode 配置 {pid}"
    except (OSError, ValueError, AttributeError):
        pass
    try:
        cred = json.loads(ZCODE_CREDENTIALS.read_text(encoding="utf-8"))
        token = cred.get("zcodejwttoken") or ""
        if token and not token.startswith("enc:"):
            return str(token), "ZCode credentials.json"
    except (OSError, ValueError):
        pass
    return "", ""


def _fetch_balance_api(token: str) -> QuotaSnapshot:
    now = datetime.now(timezone.utc)
    try:
        resp = httpx.get(
            BALANCE_URL,
            params={"app_version": APP_VERSION},
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/json"},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        return QuotaSnapshot(windows={}, fetched_at=now, error=f"网络错误: {exc}")
    if resp.status_code != 200:
        return QuotaSnapshot(windows={}, fetched_at=now,
                             error=f"HTTP {resp.status_code}（该端点目前仅"
                                   "接受 ZCode 桌面端请求，属正常现象）")
    try:
        data = resp.json().get("data") or {}
    except ValueError:
        return QuotaSnapshot(windows={}, fetched_at=now, error="响应格式无法解析")
    usage = _aggregate(data)
    if usage is None:
        return QuotaSnapshot(windows={}, fetched_at=now,
                             error="响应中没有有效的额度余额")
    return QuotaSnapshot(windows={_GRANT: usage}, fetched_at=now)


# ---- 对外接口（签名兼容 QUOTA_FETCHERS）----

def _fetch_quota_sync(api_key: str = "") -> QuotaSnapshot:
    parsed = parse_latest_balance_log()
    if parsed is not None:
        data, ts = parsed
        usage = _aggregate(data)
        if usage is not None:
            return QuotaSnapshot(windows={_GRANT: usage}, fetched_at=ts)

    token, _src = discover_zcode_token()
    manual = _manual_override()
    if manual:
        token = manual
    if token:
        snap = _fetch_balance_api(token)
        if snap.ok:
            return snap

    return QuotaSnapshot(
        windows={}, fetched_at=datetime.now(timezone.utc),
        error="未找到 ZCode Start Plan 额度数据：ZCode 桌面端运行时会每 "
              "~2 分钟自动拉取余额并写入本地日志，请先运行一次桌面端 ZCode")


def _manual_override() -> str:
    """config.json 里的 zcode_token 手动覆盖（仅 API 兜底路径使用）。"""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return str(data.get("zcode_token") or "")
    except (OSError, ValueError):
        return ""


async def fetch_quota(api_key: str = "") -> QuotaSnapshot:
    return await asyncio.to_thread(_fetch_quota_sync, api_key)


def test_connection() -> tuple[bool, str]:
    """设置页测试连接：返回 (成功?, 描述)。"""
    snap = _fetch_quota_sync()
    if snap.ok:
        w = next(iter(snap.windows.values()))
        used = w.used_percent
        total_hint = ""
        if w.resets_at is not None:
            from .ui.quota_tab import fmt_countdown
            total_hint = f"，{fmt_countdown(w.resets_at, verb='到期')}"
        return True, (f"连接成功：{w.label} 已用 {used:.1f}%"
                      f"（数据时间 {snap.fetched_at.astimezone().strftime('%H:%M:%S')}）"
                      f"{total_hint}")
    return False, snap.error or "未知错误"
