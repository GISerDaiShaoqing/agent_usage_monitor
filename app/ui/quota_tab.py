"""额度页：5小时 / 每周 / 每月 三窗口卡片。"""
from __future__ import annotations

from datetime import datetime, timezone

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from ..zen_client import ALL_WINDOW_ORDER, QuotaSnapshot, WindowUsage

BAR_WIDTH = 24

BLOCKS = "█▉▊▋▌▍▎▏"


def render_bar(percent: float, width: int = BAR_WIDTH) -> Text:
    """块字符进度条：percent 为已用 0-100。"""
    filled = max(0.0, min(100.0, percent)) / 100.0 * width
    full = int(filled)
    frac = filled - full
    bar = "█" * full
    if full < width and frac >= 0.5:
        bar += BLOCKS[3]  # ▌
        bar += " " * (width - full - 1)
    else:
        bar += " " * (width - full)
    return Text(bar)


def fmt_countdown(resets_at: datetime | None, now: datetime | None = None,
                  verb: str = "重置") -> str:
    if resets_at is None:
        return f"{verb}时间未知"
    now = now or datetime.now(timezone.utc)
    delta = resets_at - now
    if delta.total_seconds() <= 0:
        return f"即将{verb}"
    total = int(delta.total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}天{hours}小时后{verb}"
    if hours > 0:
        return f"{hours}小时{minutes}分后{verb}"
    return f"{minutes}分钟后{verb}"


class WindowCard(Static):
    """单个额度窗口卡片。"""

    def set_usage(self, usage: WindowUsage | None, plan_name: str = "") -> None:
        if usage is None:
            self.update(Text("暂无数据", style="dim"))
            return
        if usage.is_limited or usage.is_low:
            color, tag = "red", " ⚠"
        elif usage.remaining_percent <= 40:
            color, tag = "yellow", ""
        else:
            color, tag = "green", ""
        lines = Text()
        lines.append(f"{usage.label}额度{tag}\n", style=f"bold {color}")
        lines.append(render_bar(usage.used_percent))
        lines.append(f" {usage.used_percent:.0f}%\n", style=color)
        lines.append("剩余 ", style="dim")
        lines.append(f"{usage.remaining_percent:.0f}%", style=f"bold {color}")
        lines.append("\n", style="default")
        verb = "到期" if usage.name == "grant" else "重置"
        lines.append(fmt_countdown(usage.resets_at, verb=verb), style="dim")
        if usage.is_limited:
            lines.append("  已限流", style="bold red")
        if plan_name:
            lines.append(f"\n{plan_name}", style="rgb(140,150,160)")
        self.update(lines)


class QuotaTab(Vertical):
    def compose(self):
        self._cards: dict[str, WindowCard] = {}
        with Horizontal(id="quota-cards"):
            for name in ("rolling", "weekly", "monthly"):
                card = WindowCard(id=f"card-{name}")
                self._cards[name] = card
                yield card
        self._status = Static("", id="quota-status")
        yield self._status

    def _rebuild_cards(self, names: list[str]) -> None:
        """按快照实际窗口重建卡片（如 Start Plan 只有单个 grant 窗口）。"""
        container = self.query_one("#quota-cards")
        container.remove_children()
        self._cards = {}
        for name in names:
            card = WindowCard(id=f"card-{name}")
            self._cards[name] = card
            container.mount(card)

    def update_snapshot(self, snap: QuotaSnapshot, plan=None) -> None:
        plan_name = plan.name if plan is not None else ""
        if not snap.ok:
            err = Text(f"❌ {snap.error}", style="bold red")
            if plan_name:
                err.append(f"（套餐: {plan_name}）", style="dim")
            self._status.update(err)
            for card in self._cards.values():
                card.set_usage(None)
            return
        names = [n for n in ALL_WINDOW_ORDER if n in snap.windows]
        if names != list(self._cards):
            self._rebuild_cards(names)
        for name, card in self._cards.items():
            card.set_usage(snap.windows.get(name), plan_name)
        fetched = snap.fetched_at.astimezone().strftime("%H:%M:%S")
        source = getattr(plan, "data_source", "") or "官方用量端点"
        tag = f" · 套餐: {plan_name}" if plan_name else ""
        stale = ""
        age = datetime.now(timezone.utc) - snap.fetched_at
        if age.total_seconds() > 900:
            stale = "，数据有延迟（对应客户端运行时自动更新）"
        self._status.update(
            Text(f"✅ 更新于 {fetched}（数据源: {source}{tag}{stale}）",
                 style="dim"))
