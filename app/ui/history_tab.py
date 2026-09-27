"""历史页：各额度窗口剩余% 快照 sparkline + 明细（按当前套餐键控）。"""
from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from .. import config as cfg_mod
from .. import store

SPARK_CHARS = "▁▂▃▄▅▆▇█"
WINDOW_LABELS = {"grant": "Start Plan 剩余%",
                 "rolling": "5 小时剩余%", "weekly": "每周剩余%",
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
    def __init__(self, cfg_obj: cfg_mod.Config | None = None, **kwargs):
        super().__init__(**kwargs)
        self._cfg = cfg_obj

    def compose(self):
        self._rows: dict[str, Static] = {}
        self._detail = Static(id="hist-detail")
        yield self._detail

    def refresh_data(self) -> None:
        plan_id = self._cfg.active_plan if self._cfg is not None else ""
        windows = store.history_windows(plan_id) or ["rolling", "weekly",
                                                     "monthly"]
        if list(self._rows) != windows:
            for old in self._rows.values():
                old.remove()
            self._rows = {}
            for name in windows:
                w = Static(id=f"hist-{name}")
                self._rows[name] = w
                self.mount(w)
        lines: list[tuple[str, str]] = []
        for name, widget in self._rows.items():
            hist = store_load(name, hours=24.0, plan_id=plan_id)
            label = WINDOW_LABELS.get(name, name)
            widget.update(
                Text(f"{label}（24h）\n", style="bold")
                + sparkline([v for _, v in hist]))
            if hist:
                first_t, first_v = hist[0]
                lines.append((label,
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


def store_load(window: str, hours: float, plan_id: str = ""):
    from .. import store
    return store.load_history(window, hours, plan_id=plan_id)


def fmt_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return iso
