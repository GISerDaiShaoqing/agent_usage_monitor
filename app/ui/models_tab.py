"""模型页：分模型 token 统计表格 + 对比条 + 时间范围切换。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from rich.text import Text
from textual.containers import Vertical
from textual.widgets import DataTable, Static

from ..pricing import Pricing
from ..readers.zcode_reader import ModelUsageRow, UsageReport
from .. import config as cfg

TIME_RANGES = [("today", "今天 (1)"), ("7d", "7 天 (2)"),
               ("30d", "30 天 (3)"), ("all", "全部 (4)")]

BAR_WIDTH = 20


def human_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1e9:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if n >= 10_000:
        return f"{n / 1e3:.0f}k"
    return f"{n:,}"


def usd_str(cost: float | None) -> str:
    return f"${cost:.4f}" if cost is not None else "—"


class ModelsTab(Vertical):
    def __init__(self, cfg_obj: cfg.Config, **kwargs):
        super().__init__(**kwargs)
        self._cfg = cfg_obj
        self._pricing = Pricing()
        self._range_key = "7d"
        self._rows: list[ModelUsageRow] = []
        self._warnings: list[str] = []

    def compose(self):
        self._header = Static("", id="models-header")
        yield self._header
        table = DataTable(id="models-table", cursor_type="row")
        table.add_columns("模型", "请求", "输入", "缓存读", "缓存写",
                          "输出", "推理", "合计", "对比条", "估算花费")
        yield table
        self._table = table
        self._footer = Static("", id="models-footer")
        yield self._footer

    def on_mount(self) -> None:
        self._load_and_render()

    def set_range(self, key: str) -> None:
        if key in dict(TIME_RANGES):
            self._range_key = key
            self._load_and_render()

    def refresh_data(self) -> None:
        self._load_and_render()

    def _since(self) -> Optional[datetime]:
        now = datetime.now(timezone.utc)
        return {
            "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
            "7d": now - timedelta(days=7),
            "30d": now - timedelta(days=30),
            "all": None,
        }[self._range_key]

    def _load_and_render(self) -> None:
        from ..readers.zcode_reader import read_zcode_usage
        from ..readers.opencode_reader import read_opencode_usage
        from .. import plans as plans_mod

        since = self._since()
        flt = self._cfg.provider_filter
        if flt == "*":
            prefixes = None          # 全部 provider
        elif flt:
            prefixes = (flt,)        # 用户指定前缀
        else:
            prefixes = plans_mod.plan_provider_prefixes(self._cfg.active_plan)
        zr = read_zcode_usage(since=since, provider_prefixes=prefixes,
                              pricing=self._pricing)
        or_ = read_opencode_usage(since=since, pricing=self._pricing)

        self._rows = self._merge(zr, or_)
        self._warnings = zr.warnings + or_.warnings
        self._render_table()

    def _merge(self, *reports: UsageReport) -> list[ModelUsageRow]:
        agg: dict[tuple[str, str], ModelUsageRow] = {}
        for rep in reports:
            for row in rep.rows:
                key = (row.provider, row.model)
                dst = agg.get(key)
                if dst is None:
                    agg[key] = row
                    continue
                dst.requests += row.requests
                dst.errors += row.errors
                dst.input_uncached += row.input_uncached
                dst.cache_read += row.cache_read
                dst.cache_write += row.cache_write
                dst.output += row.output
                dst.reasoning += row.reasoning
                dst.duration_ms += row.duration_ms
        return sorted(agg.values(), key=lambda r: r.processed, reverse=True)

    def _render_table(self) -> None:
        range_label = dict(TIME_RANGES)[self._range_key]
        total_req = sum(r.requests for r in self._rows)
        self._header.update(Text(
            f"分模型 Token 统计 · 范围: {range_label} · 共 {total_req} 次请求",
            style="bold"))

        table = self._table
        table.clear()
        max_processed = max((r.processed for r in self._rows), default=0) or 1
        for r in self._rows[:50]:
            cost = self._pricing.cost(
                r.model, input_uncached=r.input_uncached, output=r.output,
                cache_read=r.cache_read, cache_write=r.cache_write)
            bar_len = int(r.processed / max_processed * BAR_WIDTH)
            bar = Text("█" * bar_len + "░" * (BAR_WIDTH - bar_len))
            bar.style = "cyan"
            model_text = Text(r.model)
            if r.errors:
                model_text.append(f" ({r.errors} err)", style="red")
            table.add_row(
                model_text,
                Text(str(r.requests), justify="right"),
                Text(human_tokens(r.input_uncached), justify="right"),
                Text(human_tokens(r.cache_read), justify="right", style="dim"),
                Text(human_tokens(r.cache_write), justify="right", style="dim"),
                Text(human_tokens(r.output), justify="right"),
                Text(human_tokens(r.reasoning), justify="right", style="dim"),
                Text(human_tokens(r.processed), justify="right", style="bold"),
                bar,
                usd_str(cost),
            )

        if not self._rows:
            self._footer.update(Text(
                "该时间范围内没有用量数据", style="dim"))
            return
        table.display = True
        total_cost = 0.0
        unpriced = False
        for r in self._rows:
            c = self._pricing.cost(
                r.model, input_uncached=r.input_uncached, output=r.output,
                cache_read=r.cache_read, cache_write=r.cache_write)
            if c is None:
                unpriced = True
            else:
                total_cost += c
        note = f"估算总花费 ${total_cost:.4f}"
        if unpriced:
            note += "（部分模型无单价，未计入）"
        if self._warnings:
            note += " · " + "；".join(self._warnings[:2])
        self._footer.update(Text(note, style="dim"))
