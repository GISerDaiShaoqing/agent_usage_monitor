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
        from ..readers.dairh_reader import read_dairh_usage
        from ..readers.codex_reader import read_codex_usage
        from ..readers.dsh_reader import read_dsh_usage
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
        if prefixes is not None:
            if flt:
                # 用户显式前缀：与其他数据源同一规则
                or_.rows = [r for r in or_.rows
                            if any(r.provider.startswith(p) for p in prefixes)]
            elif self._cfg.active_plan == "opencode-go":
                # opencode CLI 的调用都落 provider "opencode"（zen 网关），
                # 与 ZCode 侧 opencode-go-* 前缀不同名，按套餐语义放行
                or_.rows = [r for r in or_.rows
                            if r.provider.startswith("opencode")]
            else:
                # 其它套餐按各自前缀过滤，避免非当前套餐的模型混入统计
                or_.rows = [r for r in or_.rows
                            if any(r.provider.startswith(p) for p in prefixes)]

        # Codex 自带本地 rollout 记录，ZCode/OpenCode 的库看不到它的调用。
        # 默认（按套餐）模式下仅 opencode-go 套餐纳入——Codex 侧配置的
        # 就是该套餐的自定义 provider，调用都走它；用户显式过滤器则
        # 与其他数据源同一规则
        # daiRH（R 包直连网关）的本地账本：provider 缺省 opencode-go
        # （zen/go 网关），opencode-go 套餐下与 OpenCode 同源放行
        dr = read_dairh_usage(since=since, pricing=self._pricing)
        if prefixes is not None:
            if flt:
                dr.rows = [r for r in dr.rows
                           if any(r.provider.startswith(p) for p in prefixes)]
            elif self._cfg.active_plan == "opencode-go":
                dr.rows = [r for r in dr.rows
                           if r.provider.startswith("opencode")]
            else:
                dr.rows = []

        cr = read_codex_usage(since=since)
        if flt:
            if prefixes is not None:
                cr.rows = [r for r in cr.rows
                           if any(r.provider.startswith(p) for p in prefixes)]
        elif self._cfg.active_plan == "opencode-go":
            pass
        else:
            cr.rows = []
        # DSH（DeepSeek Harness）本地记录：provider 随 DSH 里的配置——
        # 默认 deepseek-account 只归 DeepSeek 平台套餐；配置成 zen/go
        # 网关的自定义 provider（opencode 开头）才归 opencode-go
        ds = read_dsh_usage(since=since)
        if prefixes is not None:
            if flt:
                ds.rows = [r for r in ds.rows
                           if any(r.provider.startswith(p) for p in prefixes)]
            elif self._cfg.active_plan == "opencode-go":
                ds.rows = [r for r in ds.rows
                           if r.provider.startswith("opencode")]
            else:
                ds.rows = [r for r in ds.rows
                           if any(r.provider.startswith(p) for p in prefixes)]
        reports = [zr, or_]
        if dr.rows:
            reports.append(dr)
        if cr.rows:
            reports.append(cr)
        if ds.rows:
            reports.append(ds)
        self._rows = self._merge(*reports)
        self._warnings = zr.warnings + or_.warnings
        if flt or self._cfg.active_plan == "opencode-go" or dr.rows or cr.rows \
                or ds.rows:
            self._warnings += dr.warnings + cr.warnings + ds.warnings
        self._render_table()

    def _merge(self, *reports: UsageReport) -> list[ModelUsageRow]:
        agg: dict[tuple[str, str], ModelUsageRow] = {}
        for rep in reports:
            for row in rep.rows:
                # source 入键：同名模型跨数据源（如 glm-5.3-flash 同名出现
                # 在 ZCode / daiRH）不吞行，各自带后缀展示
                key = (row.source, row.provider, row.model)
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
            # 每行都标注数据源后缀：三个本地数据源平等，同名模型
            # （如 glm-5.3-flash）跨源出现时一眼可分
            suffix = {"": " ·zcode", "codex": " ·codex",
                      "opencode": " ·opencode", "dairh": " ·daiRH",
                      "dsh": " ·dsh"}.get(r.source, "")
            if suffix:
                model_text.append(suffix, style="dim")
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
