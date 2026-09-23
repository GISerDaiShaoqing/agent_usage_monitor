"""订阅套餐抽象：每种 coding plan 一个 PlanDef。

额度拉取按 quota_type 分发；新套餐只需在 PLAN_DEFS 注册并实现对应
fetcher（app/zen_client.fetch_quota 为 zen-go 类型）即可接入。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from . import config as cfg
from .zen_client import QuotaSnapshot, fetch_quota


# ---- 额度拉取器注册表：quota_type -> fetcher(api_key) ----
QUOTA_FETCHERS: dict[str, Callable[[str], Awaitable[QuotaSnapshot]]] = {}


async def _fetch_zen_go(api_key: str) -> QuotaSnapshot:
    return await fetch_quota(api_key)


QUOTA_FETCHERS["zen-go"] = _fetch_zen_go


@dataclass
class PlanDef:
    id: str
    name: str
    quota_type: str                          # 额度接口类型
    provider_prefixes: tuple[str, ...] = ()  # 分模型统计的 provider 过滤（空=自动发现）
    note: str = ""                           # UI 提示（如未实现说明）

    async def fetch_quota(self, api_key: str) -> QuotaSnapshot:
        fetcher = QUOTA_FETCHERS.get(self.quota_type)
        now = datetime.now(timezone.utc)
        if fetcher is None:
            return QuotaSnapshot(
                windows={}, fetched_at=now,
                error=f"「{self.name}」的额度接口暂未实现（预留扩展点："
                      f"quota_type=\"{self.quota_type}\"），可先在模型页看用量统计")
        return await fetcher(api_key)


# ---- 套餐注册表 ----
# opencode-go: 已实现；其余为预留槽位（接新订阅时注册对应 fetcher 即可）
PLAN_DEFS: dict[str, PlanDef] = {
    "opencode-go": PlanDef(
        id="opencode-go",
        name="OpenCode Go",
        quota_type="zen-go",
        provider_prefixes=(),  # 空 = 自动发现（config.discover_provider_ids）
    ),
    "bigmodel-coding-plan": PlanDef(
        id="bigmodel-coding-plan",
        name="智谱 GLM Coding Plan",
        quota_type="bigmodel",
        provider_prefixes=("builtin:bigmodel", "account:bigmodel"),
        note="额度接口待接入；模型页用量统计已可追踪",
    ),
    "zai-coding-plan": PlanDef(
        id="zai-coding-plan",
        name="Z.ai Coding Plan",
        quota_type="zai",
        provider_prefixes=("builtin:zai", "account:zai"),
        note="额度接口待接入；模型页用量统计已可追踪",
    ),
    "kimi-coding-plan": PlanDef(
        id="kimi-coding-plan",
        name="Kimi 会员",
        quota_type="kimi",
        provider_prefixes=("builtin:kimi",),
        note="额度接口待接入",
    ),
}


def get_plan(plan_id: str) -> PlanDef:
    return PLAN_DEFS.get(plan_id) or PLAN_DEFS["opencode-go"]


def enabled_plans() -> list[PlanDef]:
    """按注册顺序返回全部套餐（未来可接 config 开关过滤）。"""
    return list(PLAN_DEFS.values())


def cycle_plan(current_id: str) -> PlanDef:
    """在注册表里循环切换。"""
    plans = enabled_plans()
    ids = [p.id for p in plans]
    try:
        return plans[(ids.index(current_id) + 1) % len(plans)]
    except ValueError:
        return plans[0]


def plan_provider_prefixes(plan_id: str) -> tuple[str, ...]:
    """分模型统计的 provider 过滤。opencode-go 动态发现；其余用注册前缀。"""
    plan = get_plan(plan_id)
    if plan.id == "opencode-go":
        return cfg.discover_provider_ids()
    return plan.provider_prefixes
