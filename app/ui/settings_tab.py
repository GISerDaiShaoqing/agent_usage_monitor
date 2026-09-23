"""设置页：API key 覆盖、轮询间隔、provider 过滤、连接测试。"""
from __future__ import annotations

import asyncio

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Static

from .. import config as cfg
from ..zen_client import test_connection


def mask(key: str) -> str:
    if not key:
        return "（未发现）"
    if len(key) <= 12:
        return key[:4] + "****"
    return f"{key[:8]}...{key[-4:]}"


class SettingsTab(Vertical):
    def __init__(self, cfg_obj: cfg.Config, **kwargs):
        super().__init__(**kwargs)
        self._cfg = cfg_obj
        self._conn_state: Static | None = None

    def compose(self):
        yield Static(self._summary_text(), id="settings-summary")

        with Horizontal(classes="setting-row"):
            yield Static("当前套餐：", classes="setting-label")
            yield Button("◀ 切换 (s)", id="plan-prev-btn")
            self._plan_label = Static("", id="plan-label")
            yield self._plan_label
            yield Button("切换 ▶", id="plan-next-btn")

        with Horizontal(classes="setting-row"):
            yield Static("API key 覆盖：", classes="setting-label")
            self._key_input = Input(
                placeholder="留空=使用自动发现的 key",
                value=self._cfg.api_key, id="key-input")
            yield self._key_input

        with Horizontal(classes="setting-row"):
            yield Static("轮询间隔（秒）：", classes="setting-label")
            self._poll_input = Input(str(self._cfg.poll_interval),
                                     id="poll-input")
            yield self._poll_input
            yield Static("UI 刷新（秒）：", classes="setting-label")
            self._refresh_input = Input(str(self._cfg.refresh_interval),
                                        id="refresh-input")
            yield self._refresh_input

        with Horizontal(classes="setting-row"):
            yield Static("provider 过滤：", classes="setting-label")
            self._provider_input = Input(
                self._cfg.provider_filter,
                placeholder="空=全部（如 opencode-go-chat）",
                id="provider-input")
            yield self._provider_input

        with Horizontal(classes="setting-row"):
            yield Button("保存设置", id="save-btn", variant="primary")
            yield Button("测试连接 (t)", id="test-btn")
            self._conn_state = Static("", id="conn-state")
            yield self._conn_state

        self._hint = Static(Text(
            "· API key 自动发现顺序：手动配置 > 环境变量 OPENCODE_GO_API_KEY"
            " > ZCode provider 配置 > OpenCode auth.json\n"
            "· provider 过滤：空=当前套餐默认；*=全部；其它=provider 前缀\n"
            "· 数据库均为只读读取，本 App 唯一写入自己的 data/snapshots.db",
            style="dim"), id="settings-hint")
        yield self._hint
        self._update_plan_label()

    def _summary_text(self) -> Text:
        t = Text("凭据与数据源\n", style="bold")
        t.append("发现 key: ", style="dim")
        t.append(mask(self._cfg.discovered_key) + "\n")
        t.append("来源: ", style="dim")
        t.append(self._cfg.discovered_from or "无\n")
        return t

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            self._save()
        elif event.button.id == "test-btn":
            self.run_worker(self._do_test(), exclusive=True)
        elif event.button.id in ("plan-prev-btn", "plan-next-btn"):
            from .. import plans as plans_mod
            if event.button.id == "plan-prev-btn":
                # 反向循环：往前两次再往后一次
                nxt = plans_mod.cycle_plan(self._cfg.active_plan)
                nxt = plans_mod.cycle_plan(nxt.id)
            else:
                nxt = plans_mod.cycle_plan(self._cfg.active_plan)
            self._cfg.active_plan = nxt.id
            self._cfg.save()
            self._update_plan_label()

    def _update_plan_label(self) -> None:
        from .. import plans as plans_mod
        plan = plans_mod.get_plan(self._cfg.active_plan)
        if hasattr(self, "_plan_label"):
            self._plan_label.update(
                Text(f"{plan.name}（{plan.id}）" +
                     (f" · {plan.note}" if plan.note else ""),
                     style="bold cyan"))

    def _save(self) -> None:
        self._cfg.api_key = self._key_input.value.strip()
        try:
            self._cfg.poll_interval = max(10, int(self._poll_input.value))
        except ValueError:
            pass
        try:
            self._cfg.refresh_interval = max(5, int(self._refresh_input.value))
        except ValueError:
            pass
        self._cfg.provider_filter = self._provider_input.value.strip()
        self._cfg.save()
        self._hint.update(Text("✅ 已保存 config.json（部分设置重启后生效）",
                               style="bold green"))

    async def _do_test(self) -> None:
        if self._conn_state is None:
            return
        key = self._key_input.value.strip() or self._cfg.discovered_key
        self._conn_state.update(Text("测试中…", style="yellow"))
        ok, msg = await asyncio.to_thread(_sync_test, key)
        self._conn_state.update(
            Text(("✅ " if ok else "❌ ") + msg,
                 style="green" if ok else "red"))


def _sync_test(key: str) -> tuple[bool, str]:
    return asyncio.run(_atest(key))


async def _atest(key: str) -> tuple[bool, str]:
    from ..zen_client import test_connection
    return await test_connection(key)
