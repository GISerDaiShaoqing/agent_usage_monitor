"""OpenCode Go / coding plan 额度追踪 TUI 主入口。

运行: python -m app.main   （详见 README.md）
"""
from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, TabPane, TabbedContent

from . import config as cfg
from . import plans, store
from .ui.history_tab import HistoryTab
from .ui.models_tab import ModelsTab
from .ui.quota_tab import QuotaTab
from .ui.settings_tab import SettingsTab


class OCGoQuotaApp(App):
    TITLE = "OpenCode Go 额度追踪"
    SUB_TITLE = "5小时 / 周 / 月额度 · 分模型 token 统计"

    CSS = """
    Screen { background: #0d1117; }
    #quota-cards { height: 1fr; align: center middle; }
    WindowCard {
        border: round $primary;
        width: 1fr; height: 60%;
        padding: 1 2;
        margin: 0 1;
        background: #161b22;
    }
    #quota-status, #models-header, #models-footer, #hist-detail {
        padding: 1 2; height: auto;
    }
    QuotaTab, ModelsTab, HistoryTab, SettingsTab { padding: 1 1; }
    #models-table { height: 1fr; }
    DataTable { background: #161b22; }
    .setting-row { height: 3; margin: 0 0 1 0; }
    .setting-label { width: 20; padding-top: 1; }
    Input { width: 1fr; }
    Static#settings-summary { padding: 1 2; }
    Static#settings-hint { padding: 0 1; }
    """
    BINDINGS = [
        Binding("r", "refresh_all", "刷新"),
        Binding("s", "switch_plan", "切换套餐"),
        Binding("1", "range('today')", "今天", show=False),
        Binding("2", "range('7d')", "7天", show=False),
        Binding("3", "range('30d')", "30天", show=False),
        Binding("4", "range('all')", "全部", show=False),
        Binding("t", "test_connection", "测试连接", show=False),
        Binding("tab", "focus_next", "下一个", show=False),
        Binding("q", "quit", "退出"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg = cfg.load_config()
        self._quota_tab: QuotaTab | None = None
        self._models_tab: ModelsTab | None = None
        self._history_tab: HistoryTab | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="quota"):
            with TabPane("额度", id="quota"):
                self._quota_tab = QuotaTab(id="quota-inner")
                yield self._quota_tab
            with TabPane("模型", id="models"):
                self._models_tab = ModelsTab(self.cfg, id="models-inner")
                yield self._models_tab
            with TabPane("历史", id="history"):
                self._history_tab = HistoryTab(self.cfg, id="history-inner")
                yield self._history_tab
            with TabPane("设置", id="settings"):
                yield SettingsTab(self.cfg, id="settings-inner")
        yield Footer()

    def _active_plan(self) -> plans.PlanDef:
        return plans.get_plan(self.cfg.active_plan)

    def _poll_and_store(self) -> None:
        self.run_worker(self._poll_worker(), exclusive=True)

    async def _poll_worker(self) -> None:
        plan = self._active_plan()
        snap = await plan.fetch_quota(self.cfg.discovered_key or self.cfg.api_key)
        store.save_snapshot(snap, plan_id=plan.id)
        if self._quota_tab is not None:
            self._quota_tab.update_snapshot(snap, plan=plan)

    def _ui_tick(self) -> None:
        if self._quota_tab is not None:
            self._poll_and_store()
        if self._models_tab is not None:
            self._models_tab.refresh_data()
        if self._history_tab is not None:
            self._history_tab.refresh_data()

    async def on_mount(self) -> None:
        plan = self._active_plan()
        self.sub_title = f"当前套餐: {plan.name}"
        self._poll_and_store()
        self.set_interval(self.cfg.refresh_interval, self._ui_tick)
        self.set_interval(self.cfg.poll_interval * 2, store.prune)

    def action_refresh_all(self) -> None:
        self._ui_tick()
        self.notify("已刷新", timeout=2)

    def action_switch_plan(self) -> None:
        next_plan = plans.cycle_plan(self.cfg.active_plan)
        self.cfg.active_plan = next_plan.id
        self.cfg.save()
        self.sub_title = f"当前套餐: {next_plan.name}"
        self._ui_tick()
        self.notify(f"已切换到套餐：{next_plan.name}", timeout=3)

    def action_range(self, key: str) -> None:
        if self._models_tab is not None:
            self._models_tab.set_range(key)

    async def action_test_connection(self) -> None:
        plan = self._active_plan()
        if plan.quota_type == "zcode-start-plan":
            from .zcode_client import test_connection
            ok, msg = await asyncio.to_thread(test_connection)
        else:
            from .zen_client import test_connection
            ok, msg = await test_connection(
                self.cfg.discovered_key or self.cfg.api_key)
        self.notify(("✅ " if ok else "❌ ") + msg, timeout=5,
                    severity="information" if ok else "error")


def main() -> None:
    store.prune()
    app = OCGoQuotaApp()
    app.run()


if __name__ == "__main__":
    main()
