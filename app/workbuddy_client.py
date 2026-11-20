"""WorkBuddy（腾讯 CodeBuddy）官方 Credits 客户端。

端点逆向自 keros68/metrik（src-tauri/src/coding_quota.rs fetch_workbuddy_quota，
2026-07 国内账号真机核对）：POST /v2/billing/meter/get-user-resource，
HTTP 200 里 code != 0 为业务错误；data.Response.Data.Accounts[] 跨套餐求和
CycleCapacitySize / CycleCapacityRemain 得单一 Credits 窗口，重置时间取
最早的 CycleEndTime（"YYYY-MM-DD HH:MM:SS" 本地时区字符串）。

凭据链（metrik 同款"只用明文凭据"原则，不逆向客户端加密）：
  1. config.json workbuddy_token（可选 workbuddy_uid / workbuddy_domain）
  2. 环境变量 WORKBUDDY_ACCESS_TOKEN（可选 WORKBUDDY_USER_ID / WORKBUDDY_DOMAIN）
  3. 客户端明文 auth 文件 CodeBuddyExtension/Data/Public/auth/*.info
     （workbuddy-desktop.info 优先，metrik 同款排序）。WorkBuddy 5.11.3+ 把
     accessToken 加密为 {"$wbEncrypted":...}，此路自动让位并提示手动配置。

凭据仅随单次请求头发给 WorkBuddy/CodeBuddy 官方服务器，不落盘、
错误信息不含 token 原文；拿不到凭据报明确错误，绝不写零值。
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import config as cfg
from .zen_client import QuotaSnapshot, WindowUsage, fmt_units

QUOTA_PATH = "/v2/billing/meter/get-user-resource"

# host 候选：凭据 domain（形如 www.codebuddy.cn）→ 已知网关逐个试；
# 国际站与国内站分流，错站 401 无害继续（metrik 同款）。
_FALLBACK_HOSTS = ("www.codebuddy.cn", "copilot.tencent.com", "www.codebuddy.ai")


@dataclass
class WBCredential:
    token: str
    uid: str = ""      # X-User-Id 头（企业/iOA 账号用；个人账号为空则不发）
    domain: str = ""   # X-Domain 头，兼 host 候选首位
    source: str = ""


def _manual_credential() -> tuple[str, str, str]:
    """config.json 手动字段（设置页无输入框，用户自行编辑文件）。"""
    try:
        data = json.loads(cfg.CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", "", ""
    if not isinstance(data, dict):
        return "", "", ""

    def get(name: str) -> str:
        return str(data.get(name) or "").strip()

    return get("workbuddy_token"), get("workbuddy_uid"), get("workbuddy_domain")


def _auth_dirs(root: Path | None = None) -> list[Path]:
    """客户端 auth 目录候选（metrik 同款落位；root 供测试注入）。"""
    if root is not None:
        return [root]
    out: list[Path] = []

    def add(base: str | None) -> None:
        if not base:
            return
        p = Path(base) / "CodeBuddyExtension" / "Data" / "Public" / "auth"
        if p not in out:
            out.append(p)

    if sys.platform == "win32":
        # Windows 上 Local 优先（metrik data_local_dir → data_dir 同序）
        add(os.environ.get("LOCALAPPDATA"))
        add(os.environ.get("APPDATA"))
    elif sys.platform == "darwin":
        add(str(Path.home() / "Library" / "Application Support"))
    else:
        add(os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share"))
    return out


def _scan_auth_files(
    root: Path | None = None,
) -> tuple[WBCredential | None, bool]:
    """扫 auth 目录 *.info。返回 (凭据, 是否见到加密 token)。

    文件名含 workbuddy 的优先（WorkBuddy 桌面版 workbuddy-desktop.info
    先于 CodeBuddy 的 Tencent-Cloud.coding-copilot.info）；取第一个
    accessToken 为明文串的文件。metrik 要求整文件可解析为字符串 token，
    加密文件直接放弃扫描；这里改为跳过加密文件继续找其余明文文件。
    """
    encrypted_seen = False
    candidates: list[Path] = []
    for d in _auth_dirs(root):
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        candidates.extend(p for p in entries
                          if p.is_file() and p.suffix == ".info")
    # stable sort：workbuddy 命名优先，目录顺序（Local → Roaming）保持
    candidates.sort(key=lambda p: "workbuddy" not in p.name.lower())
    for path in candidates:
        try:
            raw = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        auth = data.get("auth")
        if not isinstance(auth, dict):
            continue
        token = auth.get("accessToken")
        if isinstance(token, dict):
            encrypted_seen = encrypted_seen or "$wbEncrypted" in token
            continue
        if not isinstance(token, str) or not token.strip():
            continue
        account = data.get("account")
        uid = str(account.get("uid") or "") \
            if isinstance(account, dict) else ""
        return WBCredential(
            token=token.strip(),
            uid=uid,
            domain=str(auth.get("domain") or ""),
            source=path.name,
        ), encrypted_seen
    return None, encrypted_seen


def discover_workbuddy_credential() -> tuple[WBCredential | None, str]:
    """凭据发现链。返回 (凭据, 来源描述)；失败返回 (None, 原因说明)。"""
    token, uid, domain = _manual_credential()
    if token:
        return WBCredential(token, uid, domain, "config.json 手动配置"), ""
    env_token = os.environ.get("WORKBUDDY_ACCESS_TOKEN", "").strip()
    if env_token:
        return WBCredential(
            token=env_token,
            uid=os.environ.get("WORKBUDDY_USER_ID", "").strip(),
            domain=os.environ.get("WORKBUDDY_DOMAIN", "").strip(),
            source="环境变量 WORKBUDDY_ACCESS_TOKEN",
        ), ""
    cred, encrypted = _scan_auth_files()
    if cred is not None:
        return cred, ""
    if encrypted:
        return None, (
            "客户端 accessToken 已加密（WorkBuddy 5.11+ 的 $wbEncrypted），"
            "自动发现不可用：请在 config.json 填 workbuddy_token 手动配置")
    return None, (
        "未找到 WorkBuddy/CodeBuddy 登录凭据：先在客户端登录，"
        "或在 config.json 填 workbuddy_token 手动配置")


def _host_candidates(domain: str) -> list[str]:
    hosts: list[str] = []
    d = domain.strip()
    if d and ("codebuddy" in d or "tencent" in d):
        hosts.append(d)
    for h in _FALLBACK_HOSTS:
        if h not in hosts:
            hosts.append(h)
    return hosts


def _num(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return v if v > 0 else 0.0


def _parse_cn_dt(value) -> datetime | None:
    """CycleEndTime：本地时区 "YYYY-MM-DD HH:MM:SS"（兜底 ISO）。"""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    try:
        naive = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            naive = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
        return naive if naive.tzinfo else naive.astimezone()
    return naive.astimezone()          # naive → 本地时区（chrono::Local 同款）


def _build_windows(data: dict) -> dict[str, WindowUsage]:
    """Accounts[] → 单一 credits 窗口（size 全 0 不伪造 100%）。"""
    data = data.get("data") if isinstance(data, dict) else None
    data = data.get("Response") if isinstance(data, dict) else None
    data = data.get("Data") if isinstance(data, dict) else None
    accounts = data.get("Accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, list):
        return {}
    total_size = total_remain = 0.0
    earliest: datetime | None = None
    for acct in accounts:
        if not isinstance(acct, dict):
            continue
        size = _num(acct.get("CycleCapacitySize"))
        if size <= 0:
            continue
        total_size += size
        total_remain += max(0.0, _num(acct.get("CycleCapacityRemain")))
        reset = _parse_cn_dt(acct.get("CycleEndTime"))
        if reset is not None and (earliest is None or reset < earliest):
            earliest = reset
    if total_size <= 0:
        return {}
    remaining_pct = max(0.0, min(100.0, total_remain / total_size * 100.0))
    return {"credits": WindowUsage(
        name="credits",
        label="WorkBuddy Credits",
        status="ok",
        used_percent=100.0 - remaining_pct,
        resets_at=earliest,
        units_remaining=int(round(total_remain)),
    )}


async def fetch_quota(api_key: str = "", timeout: float = 20.0) -> QuotaSnapshot:
    """拉取 WorkBuddy 官方 Credits。api_key 与注册表签名对齐，不使用。"""
    now = datetime.now(timezone.utc)
    cred, why = discover_workbuddy_credential()
    if cred is None:
        return QuotaSnapshot(windows={}, fetched_at=now, error=why)
    n = now
    body = {
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": n.strftime("%Y-%m-%dT%H:%M:%SZ"),
        # 上界给远期（含长期/永久套餐），metrik 同款
        "PackageEndTimeRangeEnd": (n + timedelta(days=365 * 50))
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    headers = {"Authorization": f"Bearer {cred.token}",
               "Content-Type": "application/json",
               "Accept": "application/json"}
    if cred.uid:
        headers["X-User-Id"] = cred.uid
    if cred.domain:
        headers["X-Domain"] = cred.domain

    last_error: str | None = None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for host in _host_candidates(cred.domain):
                try:
                    resp = await client.post(
                        f"https://{host}{QUOTA_PATH}",
                        json=body, headers=headers)
                except httpx.HTTPError as exc:
                    last_error = f"网络错误: {exc}"
                    continue
                if resp.status_code in (401, 403):
                    last_error = (f"HTTP {resp.status_code}：{host} 拒绝该凭据"
                                  "（可能不是账户所在站点）")
                    continue
                if resp.status_code != 200:
                    masked = re.sub(r"[A-Za-z0-9_+/=]{16,}", "<token>",
                                    resp.text[:200])
                    last_error = f"HTTP {resp.status_code}: {masked}"
                    continue
                try:
                    data = resp.json()
                except ValueError:
                    last_error = "响应非 JSON"
                    continue
                code = data.get("code") if isinstance(data, dict) else None
                if isinstance(code, int) and not isinstance(code, bool) \
                        and code != 0:
                    msg = str(data.get("msg") or "未知错误")
                    return QuotaSnapshot(
                        windows={}, fetched_at=now,
                        error=f"WorkBuddy 业务错误 code {code}: {msg[:80]}")
                windows = _build_windows(data)
                if not windows:
                    last_error = ("响应里没有可用套餐（Accounts 为空或协议变更）")
                    continue
                return QuotaSnapshot(windows=windows, fetched_at=now)
    except httpx.HTTPError as exc:
        last_error = f"网络错误: {exc}"
    return QuotaSnapshot(windows={}, fetched_at=now,
                         error=last_error or "全部站点均请求失败")


async def test_connection() -> tuple[bool, str]:
    """设置页连接测试：返回 (成功?, 描述)。"""
    snap = await fetch_quota()
    if snap.ok:
        w = next(iter(snap.windows.values()))
        remain = (f"（{fmt_units(w.units_remaining)} Credits）"
                  if w.units_remaining is not None else "")
        return True, f"连接成功：剩余 {w.remaining_percent:.0f}%{remain}"
    return False, snap.error or "未知错误"
