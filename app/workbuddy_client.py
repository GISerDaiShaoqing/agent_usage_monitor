"""WorkBuddy（腾讯 CodeBuddy）官方积分客户端——扫码 OAuth 主线 + Cookie 备用。

台式机真机验证口径（2026-11-21，体验版账户）+ 无凭据存活探测（2026-11-21）：
  - 额度端点：www.workbuddy.cn（备用 copilot.tencent.com / wb.tencentbuddy.com）
    POST /billing/meter/get-user-resource-summary   body {}  → 总余额 + 分组
    POST /billing/meter/get-user-resource-free-packages      → 赠送包明细 + 到期
    POST /billing/meter/get-user-resource-paid-packages      → 付费包明细（体验版
    为空属正常）。两段式：summary 先给出 data.Packages[].PackageCode 与
    data.CreditGroups[]（Kind/Name/CycleRemainCapacity），free/paid 请求体
    必须带 PackageCodes（缺则 400 code 10001），返回 data.Accounts[]
    （CycleStartTime/CycleEndTime 本地时区串、Capacity* 与 *Precise 并存、
    可能带 grantReason；两列表按 AccountId/ResourceId 去重后合并供到期计算）
  - 旧 metrik 路线 /v2/billing/meter/get-user-resource 在网关层仍存活
    （APISIX 401 ≠ 404），但其凭据源（客户端明文 accessToken）随客户端
    5.11.3+ 加密（$wbEncrypted）失效，弃用。
  - 凭据链（refreshToken 优先，Cookie 自动降级备用）：
    ① 扫码 OAuth（官方 plugin 流程，纯 HTTP、免客户端、免逆向、免剪贴板）：
       一键：python -m app.workbuddy_client --login；手动等价三步：
       POST copilot.tencent.com/v2/plugin/auth/state?platform=workbuddy
       → 浏览器开 authUrl 扫码 → 轮询 GET /v2/plugin/auth/token?state=
       → 拿 refreshToken（约半年）填 config.json 的 workbuddy_refresh_token；
       运行时 POST /v2/plugin/auth/token/refresh 换 accessToken（内存缓存、
       提前 5 分钟续期），uid 经 GET /v2/plugin/account 自动补齐（config
       workbuddy_uid 可手动覆盖）。
    ② 官网 Cookie（备用）：浏览器登录 www.workbuddy.cn → F12 任一请求
       Copy as cURL → 整段粘进 config.json 的 workbuddy_cookie（自动提取
       session/session_2；session 有效期由服务端定，过期重抓）。

口径（真机核对）：余额以 summary 为准（TotalCredits + CreditGroups 的
plan_base/plan_bonus/addon/benefit = 基础/赠送/购买/奖励）；到期时间取
free-packages 的 CycleEndTime（summary 不带时限）；TotalDosage 是累计
已消耗不是剩余；数值多为 *Precise 字符串；同列表按 ResourceId 去重；
按天发放包（CapacityType 4）请求时带当天 SlicePeriod 时间窗。

凭据仅随请求头发给腾讯官方服务器，不落日志、错误信息不含原文；
拿不到凭据或业务 code != 0 返回明确 error，绝不写零值。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx

from . import config as cfg
from .zen_client import QuotaSnapshot, WindowUsage

SUMMARY_PATH = "/billing/meter/get-user-resource-summary"
FREE_PATH = "/billing/meter/get-user-resource-free-packages"
PAID_PATH = "/billing/meter/get-user-resource-paid-packages"

_AUTH_HOST = "copilot.tencent.com"
_STATE_PATH = "/v2/plugin/auth/state"
_TOKEN_PATH = "/v2/plugin/auth/token"
_REFRESH_PATH = "/v2/plugin/auth/token/refresh"
_ACCOUNT_PATH = "/v2/plugin/account"

# OAuth plugin 流程的无鉴权头集合（发起授权 / 轮询 token 共用）
_NO_AUTH_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "X-Domain": "www.codebuddy.cn",
    "X-Custom-Channel-Id": "unknown",
    "X-No-Authorization": "true",
    "X-No-User-Id": "true",
    "X-No-Enterprise-Id": "true",
    "X-No-Department-Info": "true",
}

_HOSTS = ("www.workbuddy.cn", "copilot.tencent.com", "wb.tencentbuddy.com")
_PAGE_SIZE = 200
_MAX_PAGES = 10
_TOKEN_SLACK_S = 300          # accessToken 提前 5 分钟续期

# 浏览器同款 UA：带登录态的请求若配 python-httpx 默认 UA，会被官方 WAF 拒（403）
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 "
               "Edg/131.0.0.0")

# CreditGroups 分组中文名（未识别的键原样展示）
_GROUP_LABELS = {
    "plan_base": "基础积分",
    "plan_bonus": "赠送积分",
    "addon": "购买积分",
    "benefit": "奖励积分",
}
# 分组对象里找余额的候选键（真机形态：CreditGroups[].CycleRemainCapacity）
_GROUP_CREDIT_KEYS = ("CycleRemainCapacity", "credits", "totalCredits",
                      "credit", "amount", "total", "remain", "remaining",
                      "balance", "value")
# 套餐条目识别标记（命中任一即视为套餐对象）
_PKG_MARKERS = ("CycleEndTime", "CycleCapacitySizePrecise",
                "CycleCapacityRemainPrecise", "CycleCapacityRemain",
                "CycleCapacitySize", "CapacityType", "ResourceId", "grantReason")
# 包 → 分组归属：显式字段优先，grantReason/PackageName 关键词兜底
# （真机：赠送包 PackageName 形如 "CodeBuddy个人版拉新权益包"）；
# 关键词按具体名词在前（"注册积分奖励"归奖励、"注册赠送"归赠送），仅影响到期展示
_GROUP_FIELDS = ("creditGroup", "creditType", "groupType", "group", "planType")
_REASON_HINTS = (("奖励", "benefit"), ("赠送", "plan_bonus"), ("购买", "addon"),
                 ("加油", "addon"), ("拉新", "benefit"), ("体验", "plan_bonus"),
                 ("注册", "benefit"), ("基础", "plan_base"))


class WBError(Exception):
    """终局错误：凭据失效 / 业务失败 / 协议不符，不再换站重试。"""


class WBAuthRejected(WBError):
    """凭据被额度端点整体拒绝（所试站点全部 401/403）——可自动降级备用线。"""


# ---- 凭据 ----

def _extract_cookie(raw: str) -> str:
    """从粘贴内容提取 Cookie 头的值。支持整段 cURL 命令（-H 'Cookie: …'
    / --cookie / -b）、单独的 Cookie 头行、直接的 k=v; k2=v2 串三种形态；
    提取不到含 session 的值返回 ""。"""
    s = (raw or "").strip()
    if not s:
        return ""
    m = (re.search(r"(?is)-h\s*(['\"])cookie\s*[:=]\s*(.*?)\1", s)
         or re.search(r"(?is)(?:--cookie|-b)\s+(['\"])(.*?)\1", s))
    if m:
        s = m.group(2).strip()
    if s[:7].lower() == "cookie:":
        s = s[7:].strip()
    return s if "session" in s.lower() else ""


def _cookie_capture(cookies) -> tuple[str, list[tuple[str, str]]]:
    """窗口会话的 Cookie 集合 → (可发送的 Cookie 头串, 诊断用键域列表)。

    只保留 workbuddy 域的 cookie（登录中转域 copilot.tencent.com 上的同名
    session 键不误收）；域名缺失的形态不筛。全程不落盘、不打印值。
    兼容 pywebview 各版本形态：≥5 返回 SimpleCookie 列表（键名 → Morsel，
    Morsel.key/.value 属性存名值，域在 Morsel["domain"]），4.x 返回
    http.cookiejar.Cookie（.name/.value/.domain），另有 dict 形态兜底。
    """
    if cookies is None:
        return "", []
    if hasattr(cookies, "values") and not isinstance(cookies, (list, tuple)):
        try:
            cookies = list(cookies.values())
        except Exception:
            cookies = []
    pairs: list[tuple[str, str, str]] = []      # (键名, 值, 域名小写)
    for c in cookies or []:
        if isinstance(c, dict):
            morsels = None
            try:                                # SimpleCookie：values() 是 Morsel
                vals = list(c.values())
                if vals and hasattr(vals[0], "key") \
                        and hasattr(vals[0], "value"):
                    morsels = vals
            except Exception:
                morsels = None
            if morsels is not None:
                for m in morsels:
                    if m.key and m.value:
                        dom = ""
                        try:
                            dom = str(m["domain"] or "")
                        except Exception:
                            dom = ""
                        pairs.append((str(m.key), str(m.value), dom.lower()))
                continue
            # 普通字典 / Morsel 本体（dict 子类：key/value 是属性而非条目）
            name = getattr(c, "key", None) or c.get("name") or c.get("Name")
            value = getattr(c, "value", None) or c.get("value") or c.get("Value")
            dom = getattr(c, "domain", None) or c.get("domain") \
                or c.get("Domain") or ""
            if name and value:
                pairs.append((str(name), str(value), str(dom).lower()))
        elif hasattr(c, "key") and hasattr(c, "value"):   # 裸 Morsel
            if c.key and c.value:
                dom = ""
                try:
                    dom = str(c["domain"] or "")
                except Exception:
                    dom = ""
                pairs.append((str(c.key), str(c.value), dom.lower()))
        else:                                             # cookiejar.Cookie
            name = getattr(c, "name", None)
            value = getattr(c, "value", None)
            dom = str(getattr(c, "domain", "") or "")
            if name and value:
                pairs.append((str(name), str(value), dom.lower()))
    diag = [(n, d) for n, v, d in pairs]
    seen: set[str] = set()
    out: list[str] = []
    for n, v, d in pairs:
        if d and "workbuddy" not in d:
            continue                        # 中转域/其它域的同名键不误发
        if n in seen:
            continue
        seen.add(n)
        out.append(f"{n}={v}")
    return "; ".join(out), diag


def _read_config() -> dict:
    try:
        data = json.loads(cfg.CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _pick_credential() -> tuple[str, str, str, str]:
    """凭据链：config refreshToken → env refreshToken → config Cookie →
    env Cookie。返回 (kind, value, source, uid 覆盖)；全空 kind 为 ""。"""
    data = _read_config()
    rt = str(data.get("workbuddy_refresh_token") or "").strip()
    uid = str(data.get("workbuddy_uid") or "").strip()
    if rt:
        return "refresh", rt, "config.json workbuddy_refresh_token", uid
    rt = os.environ.get("WORKBUDDY_REFRESH_TOKEN", "").strip()
    if rt:
        return "refresh", rt, "环境变量 WORKBUDDY_REFRESH_TOKEN", uid
    cookie = _extract_cookie(str(data.get("workbuddy_cookie") or ""))
    if cookie:
        return "cookie", cookie, "config.json workbuddy_cookie", ""
    cookie = _extract_cookie(os.environ.get("WORKBUDDY_COOKIE", ""))
    if cookie:
        return "cookie", cookie, "环境变量 WORKBUDDY_COOKIE", ""
    return "", "", (
        "未配置 WorkBuddy 凭据（二选一填进 config.json）："
        "① 运行 python -m app.workbuddy_client --login 扫码"
        "（推荐，约半年一换）；"
        "② 浏览器登录官网抓包把整段 cURL 粘进 workbuddy_cookie（备用）"
    ), ""


def _bearer_headers(token: str, uid: str) -> dict[str, str]:
    """额度端点的 Bearer 头集合（台式机真机验证过的形状）。
    不发 Origin / Referer；uid 为空时省略 X-User-Id。"""
    h = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "X-Request-ID": secrets.token_hex(16),
        "X-Product": "SaaS",
        "Authorization": f"Bearer {token}",
        "X-Domain": "www.codebuddy.cn",
        "Accept-Language": "zh",
    }
    if uid:
        h["X-User-Id"] = uid
    return h


def _cookie_headers(cookie: str) -> dict[str, str]:
    """额度端点的官网 Cookie 头集合（同为真机验证过的形状）。"""
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "X-Request-ID": secrets.token_hex(16),
        "X-Product": "SaaS",
        "Cookie": cookie,
        "X-Domain": "www.codebuddy.cn",
        "Accept-Language": "zh",
    }


# ---- refreshToken → accessToken（内存缓存） ----

# refreshToken -> (accessToken, 过期 epoch 秒, uid)；仅进程内，重启即失效
_CRED_CACHE: dict[str, tuple[str, float, str]] = {}


def _cache_get(refresh_token: str) -> tuple[str, str] | None:
    hit = _CRED_CACHE.get(refresh_token)
    if hit and hit[1] - time.time() > _TOKEN_SLACK_S:
        return hit[0], hit[2]
    return None


def _parse_refresh_response(body: dict) -> tuple[str, int, str | None]:
    """refresh 响应 → (accessToken, 有效期秒, 轮换出的新 refreshToken 或 None)。"""
    data = body.get("data") if isinstance(body, dict) else None
    data = data if isinstance(data, dict) else body
    data = data if isinstance(data, dict) else {}
    access = ""
    for k in ("accessToken", "access_token"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            access = v.strip()
            break
    if not access:
        raise WBError("auth 响应里没有 accessToken（协议变更）")
    try:
        expires = int(float(data.get("expiresIn")
                            or data.get("expires_in") or 86400))
    except (TypeError, ValueError):
        expires = 86400
    rt = data.get("refreshToken") or data.get("refresh_token")
    rotated = rt.strip() if isinstance(rt, str) and rt.strip() else None
    return access, max(60, expires), rotated


def _parse_uid(body) -> str:
    for k in ("uid", "userId", "user_id"):
        v = _find_key(body, k)
        if v is not None and not isinstance(v, (dict, list, bool)):
            s = str(v).strip()
            if s and s.lower() != "none":
                return s
    return ""


async def _access_token(client: httpx.AsyncClient, refresh_token: str,
                        uid_override: str) -> tuple[str, str]:
    """refreshToken → (accessToken, uid)。命中内存缓存不发请求；
    续期响应若轮换了 refreshToken，缓存换键（无法安全回写 config）。"""
    cached = _cache_get(refresh_token)
    if cached is not None:
        return cached[0], cached[1] or uid_override
    try:
        resp = await client.post(
            f"https://{_AUTH_HOST}{_REFRESH_PATH}", json={},
            headers={"Content-Type": "application/json",
                     "X-Domain": "www.codebuddy.cn",
                     "X-Refresh-Token": refresh_token,
                     "X-Auth-Refresh-Source": "plugin"})
    except httpx.HTTPError as exc:
        raise WBError(f"auth 端点网络错误: {exc}")
    if resp.status_code in (401, 403):
        raise WBError("refreshToken 被拒（失效或已轮换）：重新扫码拿新 token，"
                      "或改用 workbuddy_cookie")
    if resp.status_code != 200:
        masked = re.sub(r"[A-Za-z0-9_+/=]{16,}", "<token>", resp.text[:200])
        raise WBError(f"auth 端点 HTTP {resp.status_code}: {masked}")
    try:
        body = resp.json()
    except ValueError:
        raise WBError("auth 响应非 JSON")
    code = body.get("code") if isinstance(body, dict) else None
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        msg = str(body.get("msg") or "未知错误")
        raise WBError(f"auth 业务错误 code {code}: {msg[:80]}"
                      "（refreshToken 失效或已轮换：重新扫码或改用 Cookie）")
    access, expires, rotated = _parse_refresh_response(body)
    uid = uid_override
    if not uid:
        try:
            acc = await client.get(
                f"https://{_AUTH_HOST}{_ACCOUNT_PATH}",
                headers={"Authorization": f"Bearer {access}",
                         "X-Domain": "www.codebuddy.cn",
                         "Accept": "application/json"})
            if acc.status_code == 200:
                uid = _parse_uid(acc.json())
        except (httpx.HTTPError, ValueError):
            uid = ""
    if rotated and rotated != refresh_token:
        _CRED_CACHE.pop(refresh_token, None)
        _CRED_CACHE[rotated] = (access, time.time() + expires, uid)
    else:
        _CRED_CACHE[refresh_token] = (access, time.time() + expires, uid)
    return access, uid


# ---- 响应解析（容器路径不做假设） ----

def _find_key(obj, name):
    """BFS 找第一个同名键的值。"""
    queue = [obj]
    while queue:
        cur = queue.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k == name:
                    return v
                if isinstance(v, (dict, list)):
                    queue.append(v)
        elif isinstance(cur, list):
            queue.extend(x for x in cur if isinstance(x, (dict, list)))
    return None


def _find_packages(obj) -> list[dict]:
    """深度优先找"套餐条目列表"：列表内带套餐标记键的 dict 即收
    （容忍混合列表），命中后不再下钻条目内部（防把嵌套明细重复当套餐）。
    整列表无一命中才继续下钻找别的容器。"""
    out: list[dict] = []
    queue = [obj]
    while queue:
        cur = queue.pop(0)
        if isinstance(cur, dict):
            queue.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            dicts = [x for x in cur if isinstance(x, dict)]
            hits = [x for x in dicts
                    if any(m in x for m in _PKG_MARKERS)]
            if hits:
                out.extend(hits)
            else:
                queue.extend(x for x in cur if isinstance(x, (dict, list)))
    return out


def _dedupe(packages: list[dict]) -> list[dict]:
    """按 ResourceId/AccountId 去重（无 id 的按内容键，不误删）。"""
    seen: set[str] = set()
    out: list[dict] = []
    for p in packages:
        rid = str(p.get("ResourceId") or p.get("resourceId")
                  or p.get("AccountId") or p.get("accountId") or "")
        key = rid or (f"__n{len(out)}__{p.get('PackageName')}"
                      f"__{p.get('CycleStartTime')}")
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _to_credits(v) -> float | None:
    """"1500.000000" / 1500 / "2200" → 积分数；非法或负数返回 None。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return None
    if not d.is_finite() or d < 0:
        return None
    return float(d)


def _fmt_credits(v: float) -> str:
    """2200.0 → "2200"，12.5 → "12.5"（整数值不带小数尾）。"""
    try:
        return format(Decimal(str(v)).normalize(), "f")
    except InvalidOperation:
        return str(v)


def _parse_dt(value) -> datetime | None:
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
    return naive.astimezone()


def _group_credits(grp) -> float | None:
    """分组余额：裸数字/字符串直接用；对象按候选键序探测。"""
    if isinstance(grp, (int, float)) and not isinstance(grp, bool):
        return _to_credits(grp)
    if isinstance(grp, str):
        return _to_credits(grp)
    if isinstance(grp, dict):
        for k in _GROUP_CREDIT_KEYS:
            if k in grp:
                v = _to_credits(grp[k])
                if v is not None:
                    return v
    return None


def _group_of(pkg: dict, group_keys: set) -> str | None:
    for f in _GROUP_FIELDS:
        v = str(pkg.get(f) or "").strip()
        if v in group_keys:
            return v
    reason = str(pkg.get("grantReason") or "") + " " \
        + str(pkg.get("PackageName") or "")
    for kw, g in _REASON_HINTS:
        if kw in reason and g in group_keys:
            return g
    return None


def _build_windows(summary: dict,
                   packages: list[dict]) -> dict[str, WindowUsage]:
    """summary + 赠送/付费包 → 总余额卡 + 分组卡（余额型窗口，note 直显）。
    到期取各包 CycleEndTime 的最早值；取不到时明示"到期未知"。"""
    pkg_dt = [(p, _parse_dt(p.get("CycleEndTime"))) for p in packages]
    all_exp = [d for _, d in pkg_dt if d is not None]
    earliest = min(all_exp) if all_exp else None

    def note_of(credits: float, expiry: datetime | None) -> str:
        txt = f"剩 {_fmt_credits(credits)} 分"
        txt += (f" · {expiry.strftime('%Y-%m-%d')} 到期" if expiry is not None
                else " · 到期未知")
        return txt

    total = _to_credits(_find_key(summary, "TotalCredits"))
    if total is None:
        return {}
    windows = {"credits": WindowUsage(
        name="credits", label="WorkBuddy 总余额", status="ok",
        used_percent=0.0, resets_at=earliest,
        note=note_of(total, earliest))}

    groups = _find_key(summary, "CreditGroups")
    items: list[tuple[str, object]] = []
    if isinstance(groups, dict):
        items = list(groups.items())
    elif isinstance(groups, list):
        for i, g in enumerate(groups):
            if isinstance(g, dict):
                # 真机形态：列表项带 Kind（plan_base…）/Name/CycleRemainCapacity
                items.append((str(g.get("Kind") or g.get("kind")
                                  or g.get("type") or g.get("name") or i), g))
    gkeys = {k for k, _ in items}
    for key, grp in items:
        val = _group_credits(grp)
        if val is None:
            continue
        g_exp = [d for p, d in pkg_dt
                 if d is not None and _group_of(p, gkeys) == key]
        g_earliest = min(g_exp) if g_exp else None
        windows[f"group:{key}"] = WindowUsage(
            name=f"group:{key}",
            label=_GROUP_LABELS.get(str(key), f"积分（{key}）"),
            status="ok", used_percent=0.0, resets_at=g_earliest,
            note=note_of(val, g_earliest))
    return windows


# ---- 请求 ----

async def _call(client: httpx.AsyncClient, headers: dict, path: str,
                body: dict, errors: list[str], auth_kind: str) -> dict | None:
    """单端点多 host 尝试。业务 code != 0 视为终局（WBError）；
    401/403 记入 errors 换下一站，全站被拒抛 WBAuthRejected（可降级）；
    其它网络/网关类失败记入 errors 换下一站；全败返回 None。"""
    rejected = False
    for host in _HOSTS:
        try:
            resp = await client.post(f"https://{host}{path}", json=body,
                                     headers=headers)
        except httpx.HTTPError as exc:
            errors.append(f"{host} 网络错误: {exc}")
            continue
        if resp.status_code in (401, 403):
            rejected = True
            errors.append(f"{host} HTTP {resp.status_code}（凭据被拒）")
            continue
        if resp.status_code != 200:
            masked = re.sub(r"[A-Za-z0-9_+/=]{16,}", "<token>", resp.text[:200])
            errors.append(f"{host} HTTP {resp.status_code}: {masked}")
            continue
        try:
            data = resp.json()
        except ValueError:
            errors.append(f"{host} 响应非 JSON")
            continue
        code = data.get("code") if isinstance(data, dict) else None
        if isinstance(code, int) and not isinstance(code, bool) and code != 0:
            msg = str(data.get("msg") or "未知错误")
            raise WBError(f"WorkBuddy 业务错误 code {code}: {msg[:80]}")
        return data if isinstance(data, dict) else {}
    if rejected:
        raise WBAuthRejected(
            f"HTTP 401/403：{auth_kind} 凭据被所试站点全部拒绝")
    return None


async def _resolve_headers(client: httpx.AsyncClient, refresh_token: str,
                           uid_override: str,
                           source: str) -> tuple[dict, str]:
    """refreshToken → (Bearer 请求头, 线路说明)。"""
    token, uid = await _access_token(client, refresh_token, uid_override)
    return _bearer_headers(token, uid), f"refreshToken（{source}）"


def _package_codes(summary: dict) -> list[str]:
    """summary → data.Packages[].PackageCode（free/paid 请求体必带）。"""
    data = summary.get("data") if isinstance(summary, dict) else None
    packages = data.get("Packages") if isinstance(data, dict) else None
    if not isinstance(packages, list):
        return []
    return [str(p["PackageCode"]) for p in packages
            if isinstance(p, dict) and p.get("PackageCode")]


async def _pull(client: httpx.AsyncClient, headers: dict, auth_kind: str,
                free_base: dict, paid_base: dict,
                errors: list[str]) -> tuple[dict | None, list[dict]]:
    """两段式拉取：先 summary（余额/分组/PackageCode），再并发 free+paid
    （请求体必须带 PackageCodes，缺则 400 code 10001；两列表各自分页）。
    返回 (summary, packages)；summary 全站失败返回 (None, [])。
    体验版账户 paid 侧为空属正常；summary 无 PackageCode 时跳过套餐查询。"""
    summary = await _call(client, headers, SUMMARY_PATH, {}, errors, auth_kind)
    if summary is None:
        return None, []
    codes = _package_codes(summary)
    packages: list[dict] = []
    if not codes:
        return summary, packages
    free_body = dict(free_base, PackageCodes=codes)
    paid_body = dict(paid_base, PackageCodes=codes)
    first_free, first_paid = await asyncio.gather(
        _call(client, headers, FREE_PATH, free_body, errors, auth_kind),
        _call(client, headers, PAID_PATH, paid_body, errors, auth_kind))
    if first_free is not None:
        packages.extend(_find_packages(first_free))
    if first_paid is not None:
        packages.extend(_find_packages(first_paid))
    for path, base, first in ((FREE_PATH, free_body, first_free),
                              (PAID_PATH, paid_body, first_paid)):
        page, last_count = 2, (len(_find_packages(first))
                               if first is not None else 0)
        while last_count >= _PAGE_SIZE and page <= _MAX_PAGES:
            data = await _call(client, headers, path,
                               dict(base, PageNumber=page), errors, auth_kind)
            if data is None:
                break
            found = _find_packages(data)
            packages.extend(found)
            last_count, page = len(found), page + 1
    return summary, packages


def _cookie_fallback() -> tuple[str, str]:
    """备用 Cookie：config → env。返回 (cookie, 来源) 或 ("", "")。"""
    data = _read_config()
    cookie = _extract_cookie(str(data.get("workbuddy_cookie") or ""))
    if cookie:
        return cookie, "config.json workbuddy_cookie"
    cookie = _extract_cookie(os.environ.get("WORKBUDDY_COOKIE", ""))
    if cookie:
        return cookie, "环境变量 WORKBUDDY_COOKIE"
    return "", ""


# 会话级标记：refreshToken 线被额度端点整体拒绝（401/403）后置位，
# 本进程后续轮询直接走 Cookie 线，省掉每轮的无效重试；重启即复位。
_REFRESH_LINE_DEAD = False


async def fetch_quota(api_key: str = "", timeout: float = 20.0) -> QuotaSnapshot:
    """拉取 WorkBuddy 官方积分。api_key 与注册表签名对齐，不使用。

    refreshToken 线优先；续期失败或被额度端点整体拒绝（401/403）时
    自动降级 Cookie 备用线（配置了才降，本会话内记住降级结果）。
    """
    global _REFRESH_LINE_DEAD
    now = datetime.now(timezone.utc)
    kind, value, source, uid_override = _pick_credential()
    if not kind:
        return QuotaSnapshot(windows={}, fetched_at=now, error=source)
    day_start = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0)
    free_base = {
        "PageNumber": 1,
        "PageSize": _PAGE_SIZE,
        "Status": [0, 3],
        # 按天发放包（CapacityType 4）取当天发放窗口
        "SlicePeriodStartTime": day_start.strftime("%Y-%m-%d %H:%M:%S"),
        "SlicePeriodEndTime": (day_start + timedelta(days=1)
                               - timedelta(seconds=1))
        .strftime("%Y-%m-%d %H:%M:%S"),
    }
    paid_base = {
        "PageNumber": 1,
        "PageSize": _PAGE_SIZE,
        "Status": [0, 3],
        "NeedRenewInfo": True,
    }
    errors: list[str] = []
    summary: dict | None = None
    packages: list[dict] = []
    degraded = ""
    refresh_err = ""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if kind == "refresh" and not _REFRESH_LINE_DEAD:
                try:
                    headers, auth_kind = await _resolve_headers(
                        client, value, uid_override, source)
                    summary, packages = await _pull(
                        client, headers, auth_kind, free_base, paid_base,
                        errors)
                except WBAuthRejected:
                    _REFRESH_LINE_DEAD = True
                    degraded = "（refreshToken 被额度端点拒绝，自动降级 Cookie 线）"
                except WBError as exc:
                    refresh_err = str(exc)
                    degraded = (f"（refreshToken 线失败：{refresh_err[:60]}，"
                                "自动降级 Cookie 线）")
                if summary is None and degraded:
                    errors = []
            if summary is None:
                if kind == "cookie":
                    cookie, csrc = value, source
                else:
                    cookie, csrc = _cookie_fallback()
                if not cookie:
                    if kind == "refresh":
                        return QuotaSnapshot(
                            windows={}, fetched_at=now,
                            error=(refresh_err
                                   or "refreshToken 有效但额度端点拒绝 Bearer"
                                      "（HTTP 401/403）")
                            + "，且未配置 workbuddy_cookie 备用线——运行 "
                              "python -m app.workbuddy_client --login-cookie"
                              "（或双击 login_workbuddy_cookie.bat）自动抓取，"
                              "或按 README 手动抓包粘进 workbuddy_cookie")
                    return QuotaSnapshot(
                        windows={}, fetched_at=now,
                        error="summary 端点全部站点请求失败："
                              + ("；".join(errors[-4:]) or "未知错误"))
                headers = _cookie_headers(cookie)
                auth_kind = f"Cookie（{csrc}）{degraded}"
                errors = []
                summary, packages = await _pull(
                    client, headers, auth_kind, free_base, paid_base, errors)
            if summary is None:
                return QuotaSnapshot(
                    windows={}, fetched_at=now,
                    error="summary 端点全部站点请求失败："
                          + ("；".join(errors[-4:]) or "未知错误") + degraded)
    except WBError as exc:
        return QuotaSnapshot(windows={}, fetched_at=now, error=str(exc))
    windows = _build_windows(summary, _dedupe(packages))
    if not windows:
        return QuotaSnapshot(windows={}, fetched_at=now,
                             error="响应里没有积分数据（登录态失效或协议变更）")
    return QuotaSnapshot(windows=windows, fetched_at=now)


async def test_connection() -> tuple[bool, str]:
    """设置页连接测试：返回 (成功?, 描述)。"""
    snap = await fetch_quota()
    if snap.ok:
        w = snap.windows.get("credits")
        msg = w.note if w else "，".join(
            f"{x.label} {x.note}" for x in snap.windows.values())
        return True, f"连接成功：{msg}"
    return False, snap.error or "未知错误"


# ---- 扫码登录 CLI：python -m app.workbuddy_client --login ----

async def _login_flow() -> int:
    """扫码拿 refreshToken → 续期校验（含轮换采用）→ 写 config.json →
    试拉一次积分对账。任何一步失败都给出明确提示并返回非 0。
    全程不打印 token 原文（只显示长度与末 4 位）。"""
    import webbrowser
    async with httpx.AsyncClient(timeout=20) as client:
        print("① 发起授权…", flush=True)
        try:
            resp = await client.post(
                f"https://{_AUTH_HOST}{_STATE_PATH}?platform=workbuddy",
                json={}, headers=_NO_AUTH_HEADERS)
        except httpx.HTTPError as exc:
            print(f"❌ 网络错误: {exc}")
            return 1
        try:
            body = resp.json()
        except ValueError:
            print("❌ 发起授权响应非 JSON")
            return 1
        if (body.get("code") or 0) != 0:
            print(f"❌ 发起授权失败: {body.get('msg') or body}")
            return 1
        data = body.get("data") or {}
        state = str(data.get("state") or "")
        auth_url = str(data.get("authUrl") or "")
        if not state or not auth_url:
            print("❌ 响应缺 state/authUrl（协议变更）")
            return 1
        print("② 浏览器打开授权链接扫码登录（若没自动弹出，手动打开）：")
        print(f"   {auth_url}")
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass
        print("③ 等待扫码确认（最多 5 分钟，每 5 秒轮询）…", flush=True)
        deadline = time.time() + 300
        rt = ""
        while time.time() < deadline:
            await asyncio.sleep(5)
            try:
                poll = await client.get(
                    f"https://{_AUTH_HOST}{_TOKEN_PATH}",
                    params={"state": state}, headers=_NO_AUTH_HEADERS)
                pbody = poll.json()
            except (httpx.HTTPError, ValueError):
                continue
            if not isinstance(pbody, dict):
                continue
            code = pbody.get("code")
            if str(code) == "11217":
                print("   等待扫码确认…", flush=True)
                continue
            if code == 0 or str(code) == "0":
                try:
                    _access, _expires, rt = _parse_refresh_response(pbody)
                except WBError as exc:
                    print(f"❌ {exc}")
                    return 1
                break
            print(f"❌ 轮询返回 code {code}: {pbody.get('msg') or '未知错误'}")
            return 1
        if not rt:
            print("❌ 超时：5 分钟内未完成扫码，重新运行本命令即可")
            return 1
        print("④ 校验 refreshToken 续期…", flush=True)
        try:
            resp = await client.post(
                f"https://{_AUTH_HOST}{_REFRESH_PATH}", json={},
                headers={"Content-Type": "application/json",
                         "X-Domain": "www.codebuddy.cn",
                         "X-Refresh-Token": rt,
                         "X-Auth-Refresh-Source": "plugin"})
        except httpx.HTTPError as exc:
            print(f"❌ 网络错误: {exc}")
            return 1
        try:
            rbody = resp.json()
        except ValueError:
            print("❌ 续期响应非 JSON")
            return 1
        rcode = rbody.get("code") if isinstance(rbody, dict) else None
        if resp.status_code in (401, 403) or (
                isinstance(rcode, int) and not isinstance(rcode, bool)
                and rcode != 0):
            print(f"❌ refreshToken 续期被拒（HTTP {resp.status_code} / "
                  f"code {rcode}）：{rbody.get('msg') or ''}")
            return 1
        try:
            access, expires, rotated = _parse_refresh_response(rbody)
        except WBError as exc:
            print(f"❌ {exc}")
            return 1
        if rotated and rotated != rt:
            rt = rotated
            print("   服务端轮换了 refreshToken，已采用最新值")
        uid = ""
        try:
            acc = await client.get(
                f"https://{_AUTH_HOST}{_ACCOUNT_PATH}",
                headers={"Authorization": f"Bearer {access}",
                         "X-Domain": "www.codebuddy.cn",
                         "Accept": "application/json"})
            if acc.status_code == 200:
                uid = _parse_uid(acc.json())
        except (httpx.HTTPError, ValueError):
            uid = ""
        print("⑤ 写入 config.json（workbuddy_refresh_token"
              + ("、workbuddy_uid" if uid else "") + "）…", flush=True)
        try:
            cfg_data = _read_config()       # 只更新 workbuddy 键，其余字段原样保留
            cfg_data["workbuddy_refresh_token"] = rt
            if uid:
                cfg_data["workbuddy_uid"] = uid
            cfg.CONFIG_PATH.write_text(
                json.dumps(cfg_data, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as exc:
            print(f"❌ 写 config.json 失败: {exc}")
            return 1
        print(f"   已写入（共 {len(rt)} 字符，末 4 位 {rt[-4:]}）")
        _CRED_CACHE[rt] = (access, time.time() + expires, uid)
        print("⑥ 试拉一次积分（走正式客户端路径）…", flush=True)
        snap = await fetch_quota()
        if snap.ok:
            w = snap.windows.get("credits")
            if w is not None:
                print(f"   ✅ {w.label}：{w.note}")
            else:
                print("   ✅ 拉取成功")
        else:
            print(f"   ❌ {snap.error}")
            print("   凭据已写入但试拉失败：额度端点不收此 Bearer——"
                  "运行 python -m app.workbuddy_client --login-cookie"
                  "（或双击 login_workbuddy_cookie.bat）自动抓取 Cookie。")
        print("完成。重启 TUI / 小部件后生效。")
        return 0


async def _login_cookie_flow() -> int:
    """内置浏览器窗口登录官网 → 轮询抓取 session* Cookie → 写 config.json
    → 试拉一次积分对账。需要 pywebview（未装时给出安装指引）。
    全程不打印 Cookie 原文（只显示键数与末 4 位）。"""
    try:
        import webview
    except ImportError:
        print("❌ 缺少 pywebview：先执行  python -m pip install pywebview")
        print("   （或按 README 手动抓包：F12 → Copy as cURL → 粘进"
              " workbuddy_cookie）")
        return 1
    print("① 打开官网登录窗口，请在窗口内完成登录（扫码/账号均可）…")
    print("   登录成功后本窗口会自动关闭并写入凭据（最多等 5 分钟）", flush=True)
    window = webview.create_window(
        "WorkBuddy 登录（登录成功后本窗口自动关闭）",
        "https://www.workbuddy.cn", width=960, height=720)
    results: dict = {}

    def _poll() -> None:
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                cookie, diag = _cookie_capture(window.get_cookies())
            except Exception:
                cookie, diag = "", []
            if cookie:
                results["cookie"] = cookie
                results["diag"] = diag
                break
            time.sleep(1.5)
        try:
            window.destroy()
        except Exception:
            pass

    threading.Thread(target=_poll, daemon=True).start()
    webview.start()                       # 阻塞至窗口销毁
    cookie = results.get("cookie", "")
    if not cookie:
        print("❌ 未等到登录 Cookie（5 分钟超时或窗口被提前关闭）——"
              "重试本命令，或按 README 手动抓包")
        return 1
    diag = results.get("diag") or []
    diag_str = ", ".join(f"{n}@{d or '?'}" for n, d in diag) or "无"
    print(f"② 已抓到官网域 Cookie：{diag_str}")
    try:
        data = _read_config()             # 只更新 workbuddy 键，其余字段原样保留
        data["workbuddy_cookie"] = cookie
        cfg.CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"❌ 写 config.json 失败: {exc}")
        return 1
    print("③ 已写入 config.json 的 workbuddy_cookie")
    print("④ 试拉一次积分（走正式客户端路径，refreshToken 被拒会自动降级）…",
          flush=True)
    snap = await fetch_quota()
    if snap.ok:
        w = snap.windows.get("credits")
        if w is not None:
            print(f"   ✅ {w.label}：{w.note}")
        else:
            print("   ✅ 拉取成功")
    else:
        print(f"   ❌ {snap.error}")
    print("完成。重启 TUI / 小部件后生效。")
    return 0


# ---- 诊断：--probe 打印真实载荷结构（核对解析器用） ----

def _short(v) -> str:
    s = str(v)
    return s if len(s) <= 48 else s[:44] + "…<截断>"


def _print_tree(obj, indent: int = 0, max_depth: int = 4) -> None:
    """结构化打印：dict 逐键、list 报长度并只展开首元素、长串截断。
    数值/日期原样显示；任何超长不透明值都不完整输出。"""
    pad = "  " * indent
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and indent < max_depth:
                print(f"{pad}{k}:")
                _print_tree(v, indent + 1, max_depth)
            else:
                print(f"{pad}{k} = {_short(v)}")
    elif isinstance(obj, list):
        print(f"{pad}[{len(obj)} 项]")
        if obj and indent < max_depth:
            _print_tree(obj[0], indent + 1, max_depth)
    else:
        print(f"{pad}{_short(obj)}")


async def _probe_flow() -> int:
    """拉取三个端点的真实载荷并打印结构树 + 套餐识别计数，
    用于把解析器锁死在真实形状上（不含对话内容与凭据）。"""
    kind, value, source, uid_override = _pick_credential()
    if not kind:
        print("❌ 未配置凭据：先 --login / --login-cookie")
        return 1
    day_start = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0)
    free_base = {
        "PageNumber": 1, "PageSize": _PAGE_SIZE, "Status": [0, 3],
        "SlicePeriodStartTime": day_start.strftime("%Y-%m-%d %H:%M:%S"),
        "SlicePeriodEndTime": (day_start + timedelta(days=1)
                               - timedelta(seconds=1))
        .strftime("%Y-%m-%d %H:%M:%S"),
    }
    paid_base = {"PageNumber": 1, "PageSize": _PAGE_SIZE,
                 "Status": [0, 3], "NeedRenewInfo": True}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            if kind == "refresh":
                token, uid = await _access_token(client, value, uid_override)
                headers = _bearer_headers(token, uid)
                print(f"凭据线：refreshToken（{source}）")
            else:
                headers = _cookie_headers(value)
                print(f"凭据线：Cookie（{source}）")
            sm = await _call(client, headers, SUMMARY_PATH, {}, [], "probe")
            codes = _package_codes(sm) if sm else []
            fp = pp = None
            if codes:
                fb = dict(free_base, PackageCodes=codes)
                pb = dict(paid_base, PackageCodes=codes)
                fp = await _call(client, headers, FREE_PATH, fb, [], "probe")
                pp = await _call(client, headers, PAID_PATH, pb, [], "probe")
            else:
                print("（summary 未返回 Packages[].PackageCode——"
                      "free/paid 无法查询）")
    except WBError as exc:
        print(f"❌ {exc}")
        return 1
    free_pkgs = _find_packages(fp) if fp else []
    paid_pkgs = _find_packages(pp) if pp else []
    print("\n== summary ==")
    _print_tree(sm)
    print(f"\n== free-packages ==（PackageCodes={codes}；"
          f"识别套餐 {len(free_pkgs)} 个）")
    _print_tree(fp)
    print(f"\n== paid-packages ==（识别套餐 {len(paid_pkgs)} 个）")
    _print_tree(pp)
    if free_pkgs or paid_pkgs:
        print("\n== 首个套餐完整字段 ==")
        for k, v in (free_pkgs or paid_pkgs)[0].items():
            print(f"  {k} = {_short(v)}")
    print("\n（以上仅结构/数值，凭据与对话内容不涉及；把整段输出发回即可）")
    return 0


def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m app.workbuddy_client",
        description="WorkBuddy（腾讯 CodeBuddy）积分客户端辅助工具")
    ap.add_argument("--login", action="store_true",
                    help="扫码获取 refreshToken 并写入 config.json")
    ap.add_argument("--login-cookie", action="store_true",
                    help="内置窗口登录官网，自动抓取 Cookie 写入 config.json"
                         "（需 pywebview，未装时自动提示）")
    ap.add_argument("--probe", action="store_true",
                    help="诊断：拉取三个端点的真实载荷并打印结构树")
    args = ap.parse_args()
    if args.probe:
        return asyncio.run(_probe_flow())
    if args.login_cookie:
        return asyncio.run(_login_cookie_flow())
    if args.login:
        return asyncio.run(_login_flow())
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
