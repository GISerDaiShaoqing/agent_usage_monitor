"""历史页：三窗口剩余% 快照 sparkline + 明细。"""
from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from .. import store

SPARK_CHARS = "▁▂▃▄▅▆▇█"
WINDOW_LABELS = {"rolling": "5 小时剩余%", "weekly": "每周剩余%",
                 "monthly": "每月剩余%"}


def sparkline(values: list[float], width: int = 60) -> Text:
    if not values:
        return Text("（暂无快照数据）", style="dim")
    step = max(1, len(values) // width)
    sampled = values[::step][:width]
    line = Text()
    last = sampled[-1]
    for v in sampled:
        idx = min(int(v / 100 * (len(SPARK_CHARS) - 1)),
                  len(SPARK_CHARS) - 1)
        color = ("green" if v > 40 else
                 "yellow" if v > 15 else "red")
        line.append(SPARK_CHARS[idx], style=color)
    line.append(f"  最新 {last:.0f}%", style="bold")
    return line


class HistoryTab(VerticalScroll):
    def compose(self):
        self._rolling = Static(id="hist-rolling")
        self._weekly = Static(id="hist-weekly")
        self._monthly = Static(id="hist-monthly")
        self._detail = Static(id="hist-detail")
        for w in (self._rolling, self._weekly, self._monthly, self._detail):
            yield w

    def refresh_data(self) -> None:
        lines: list[tuple[str, str]] = []
        for name, widget in (("rolling", self._rolling),
                             ("weekly", self._weekly),
                             ("monthly", self._monthly)):
            hist = store_load(name, hours=24.0)
            widget.update(
                Text(f"{WINDOW_LABELS[name]}（24h）\n", style="bold")
                + sparkline([v for _, v in hist]))
            if hist:
                first_t, first_v = hist[0]
                lines.append((WINDOW_LABELS[name],
                              f"{first_v:.0f}% → {hist[-1][1]:.0f}%"
                              f"（{len(hist)} 点，自 {fmt_time(first_t)}）"))
        detail = Text()
        for label, text in lines:
            detail.append(f"{label}: ", style="dim")
            detail.append(text + "\n")
        if not lines:
            detail.append("快照在 App 运行期间每轮询周期记录一次，"
                          "先让它跑一会儿或切到设置页调大轮询频率。",
                          style="dim")
        self._detail.update(detail)


def store_load(window: str, hours: float):
    from .. import store
    return store.load_history(window, hours)


def fmt_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return iso
