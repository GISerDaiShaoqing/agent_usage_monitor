"""桌面悬浮小部件（tkinter）：置顶、半透明、可拖动、可拖拽缩放、背景图皮肤。

运行: Windows 双击 run.bat（pythonw 静默启动）；Linux/macOS 用 ./run.sh
与 TUI 共用同一套 config.json / plans / readers，只读不写（除自身配置）。
"""
from __future__ import annotations

import asyncio
import ctypes
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageTk

from . import config as cfg
from . import plans
from .ui.quota_tab import fmt_countdown
from .zen_client import QuotaSnapshot, WindowUsage, fmt_units, ordered_windows

APP_DIR = Path(__file__).resolve().parent.parent
# 软件显示名（与 README 标题一致）
PRODUCT_NAME = "DaiPulse"

MIN_W, MIN_H = 280, 200   # 高度下限保证 3 窗口 + 底部信息行在 DPI 缩放下不挤压
MAX_W, MAX_H = 900, 700
GRIP = 16          # 右下角缩放手柄尺寸(px)
BAR_H = 10

COLOR_FG = "#e6edf3"
COLOR_DIM = "#9aa4ae"
COLOR_WARN = "#ff4d4f"
COLOR_MID = "#faad14"
COLOR_OK = "#52c41a"


def _ui_font_family() -> str:
    """按平台选择中文字体；缺失时 Tk 会回退系统默认字体。"""
    if os.name == "nt":
        return "Microsoft YaHei UI"
    if sys.platform == "darwin":
        return "PingFang SC"
    return "Noto Sans CJK SC"


def _font(size: int, bold: bool = False):
    fam = _ui_font_family()
    return (fam, size, "bold") if bold else (fam, size)


def _find_terminal() -> list[str] | None:
    """在 Linux 上探测可用的终端模拟器，返回可执行命令前缀。"""
    for term, prefix in (
        ("gnome-terminal", ("--",)),
        ("konsole", ("-e",)),
        ("xfce4-terminal", ("-x",)),
        ("x-terminal-emulator", ("-e",)),
        ("alacritty", ("-e",)),
        ("kitty", ()),
        ("xterm", ("-e",)),
    ):
        exe = shutil.which(term)
        if exe:
            return [exe, *prefix]
    return None


class QuotaWidget:
    def __init__(self) -> None:
        import tkinter as tk
        self.cfg = cfg.load_config()
        self.plan = plans.get_plan(self.cfg.active_plan)
        self._q: queue.Queue = queue.Queue()

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

        self.root = tk.Tk()
        self.root.title(f"{PRODUCT_NAME} · 额度悬浮窗")
        self._w = max(MIN_W, int(self.cfg.widget_width))
        self._h = max(MIN_H, int(self.cfg.widget_height))
        self.root.geometry(f"{self._w}x{self._h}+40+40")
        self.root.overrideredirect(True)      # 无边框
        self.root.attributes("-topmost", self.cfg.widget_topmost)
        self.root.attributes("-alpha", self.cfg.widget_opacity)
        self.root.configure(bg="#161b22")

        self.canvas = tk.Canvas(self.root, width=self._w, height=self._h,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        # 左键拖动/缩放 + 右键菜单
        self._mode = None            # 'move' | 'resize' | None
        self._press = (0, 0, 0, 0)   # (px, py, start_w, start_h)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_menu)
        self._menu = tk.Menu(self.root, tearoff=0)
        self._menu.add_command(label="刷新", command=self._poke)
        self._plan_var = tk.StringVar(value=self.plan.id)
        plan_menu = tk.Menu(self._menu, tearoff=0)
        installed = set(plans.QUOTA_FETCHERS)
        for p in plans.enabled_plans():
            label = (p.name if p.quota_type in installed
                     else f"{p.name}（额度页待接入）")
            plan_menu.add_radiobutton(
                label=label, value=p.id, variable=self._plan_var,
                command=lambda pid=p.id: self._switch_plan(pid))
        self._plan_menu = plan_menu
        self._menu.add_cascade(label="切换套餐", menu=plan_menu)
        self._menu.add_command(label="打开 TUI 仪表盘", command=self._open_tui)
        self._menu.add_separator()
        self._topmost_var = tk.BooleanVar(value=self.cfg.widget_topmost)
        self._menu.add_checkbutton(label="置顶", variable=self._topmost_var,
                                   command=self._toggle_topmost)
        self._menu.add_command(label="隐藏", state="disabled",
                               command=self._hide_now)
        self._hide_idx = int(self._menu.index("end"))
        self._menu.add_command(label="退出", command=self._quit)

        self._bg_image: ImageTk.PhotoImage | None = None
        self._data = None
        self._tray = None
        if os.name == "nt":
            self._tray_q: queue.Queue = queue.Queue()
            try:
                from .tray import TrayIcon
                self._tray = TrayIcon(self._tray_q)
                self._tray.start(PRODUCT_NAME)
            except Exception:
                self._tray = None
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        self.root.after(200, self._pump)
        self.root.after(1000, self._tick_60s)

    # ---- 数据 ----
    def _worker(self) -> None:
        """后台线程：拉额度 + 统计模型用量，结果入队。"""
        while True:
            try:
                key = self.cfg.discovered_key or self.cfg.api_key
                snap = asyncio.run(self.plan.fetch_quota(key))
                stats = self._model_summary()
                self._q.put((snap, stats, None))
            except Exception as exc:  # 网络等异常也进 UI
                self._q.put((None, None, str(exc)))
            time.sleep(60)

    def _model_summary(self) -> str:
        from .readers.zcode_reader import read_zcode_usage
        since = datetime.now(timezone.utc) - timedelta(days=7)
        try:
            rep = read_zcode_usage(
                since=since,
                provider_prefixes=plans.plan_provider_prefixes(self.plan.id))
        except Exception:
            return ""
        rows = rep.rows
        if self.plan.id == "opencode-go":
            # Codex 调用同样走 OpenCode Go 套餐，纳入摘要（ZCode 库看不到）
            try:
                from .readers.codex_reader import read_codex_usage
                rows = rows + read_codex_usage(since=since).rows
            except Exception:
                pass
            # OpenCode CLI 的调用同理（走 zen/go 网关，记在 opencode.db）
            try:
                from .readers.opencode_reader import read_opencode_usage
                oc_rows = read_opencode_usage(since=since).rows
                rows = rows + [r for r in oc_rows
                               if r.provider.startswith("opencode")]
            except Exception:
                pass
            # daiRH（R 包直连网关）同理：usage.jsonl 账本；无 agent 机器
            # （如台式机）上这是分模型统计的主渠道
            try:
                from .readers.dairh_reader import read_dairh_usage
                dr_rows = read_dairh_usage(since=since).rows
                rows = rows + [r for r in dr_rows
                               if r.provider.startswith("opencode")]
            except Exception:
                pass
            # DSH 配置成 zen/go 网关时同理（deepseek-account 归 DS 套餐）
            try:
                from .readers.dsh_reader import read_dsh_usage
                rows = rows + [r for r in read_dsh_usage(since=since).rows
                               if r.provider.startswith("opencode")]
            except Exception:
                pass
        elif self.plan.id == "deepseek-balance":
            # DSH 直连 deepseek-account 的调用就是本套餐的主体
            try:
                from .readers.dsh_reader import read_dsh_usage
                rows = rows + [r for r in read_dsh_usage(since=since).rows
                               if r.provider.startswith("deepseek")]
            except Exception:
                pass
        if not rows:
            return "7 天暂无用量"
        rows = sorted(rows, key=lambda r: r.processed, reverse=True)
        total_req = sum(r.requests for r in rows)
        top = rows[0]
        extra = f" +{len(rows) - 1} 模型" if len(rows) > 1 else ""
        return f"{top.model} {total_req} 次{extra}（7天）"

    def _poke(self) -> None:
        threading.Thread(target=self._fetch_once, daemon=True).start()

    def _fetch_once(self) -> None:
        try:
            key = self.cfg.discovered_key or self.cfg.api_key
            snap = asyncio.run(self.plan.fetch_quota(key))
            stats = self._model_summary()
            self._q.put((snap, stats, None))
        except Exception as exc:
            self._q.put((None, None, str(exc)))

    def _pump(self) -> None:
        """主线程每 500ms 检查队列并重绘。"""
        self._drain_tray()
        try:
            while True:
                self._data = self._q.get_nowait()
        except queue.Empty:
            pass
        self._draw()
        if self._tray is not None:
            color, tip = self._tray_state()
            self._tray.update(color, tip)
        self.root.after(500, self._pump)

    def _drain_tray(self) -> None:
        """托盘事件：左键切换显示/隐藏，右键弹部件菜单。"""
        if self._tray is None:
            return
        try:
            while True:
                evt = self._tray_q.get_nowait()
                kind = evt[0]
                if kind == "toggle":
                    self._toggle_hidden()
                elif kind == "menu":
                    x = evt[1] if len(evt) > 1 else 0
                    y = evt[2] if len(evt) > 2 else 0
                    self._plan_var.set(self.plan.id)
                    self._menu.tk_popup(x, y)
                elif kind == "ready":
                    ok = len(evt) > 1 and bool(evt[1])
                    if ok:
                        self._menu.entryconfig(self._hide_idx, state="normal")
                    else:
                        self._log_line("托盘图标注册失败，菜单「隐藏」保持禁用")
        except queue.Empty:
            pass

    def _tray_state(self) -> tuple[str, str]:
        """托盘图标（颜色, tooltip）：取剩余最少的窗口。"""
        grey = "#8b949e"
        snap = self._data[0] if self._data else None
        if snap is None or not snap.ok or not snap.windows:
            return grey, f"{PRODUCT_NAME}（数据不可用）"
        u = min(snap.windows.values(), key=lambda w: w.remaining_percent)
        tip = f"{PRODUCT_NAME}｜剩 {u.remaining_percent:.0f}%（{u.label}）"
        return self._row_color(u), tip

    def _toggle_hidden(self) -> None:
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            self.root.attributes("-topmost", self.cfg.widget_topmost)
            self.root.attributes("-alpha", self.cfg.widget_opacity)
            self._poke()
        else:
            self.root.withdraw()

    def _hide_now(self) -> None:
        self.root.withdraw()

    def _quit(self) -> None:
        if self._tray is not None:
            try:
                self._tray.stop()      # 移除托盘图标后退出
            except Exception:
                pass
        self.root.destroy()

    def _log_line(self, text: str) -> None:
        try:
            APP_DIR.joinpath("data").mkdir(parents=True, exist_ok=True)
            with open(APP_DIR / "data" / "widget_error.log", "a",
                      encoding="utf-8") as f:
                f.write(datetime.now().strftime("%m-%d %H:%M:%S ")
                        + text + "\n")
        except OSError:
            pass

    def _tick_60s(self) -> None:
        self._draw()
        self.root.after(60000, self._tick_60s)

    # ---- 绘制 ----
    @staticmethod
    def _row_color(usage: WindowUsage) -> str:
        expired = (usage.resets_at is not None
                   and usage.resets_at <= datetime.now(timezone.utc))
        if expired:
            return COLOR_DIM
        if usage.is_limited or usage.is_low:
            return COLOR_WARN
        if usage.remaining_percent <= 40:
            return COLOR_MID
        return COLOR_OK

    def _draw_box(self, w: int, bottom: int) -> None:
        """换肤时内容区的边框，底边随行数自适应。"""
        if self._bg_image is not None:
            self.canvas.create_rectangle(0, 28, w, max(bottom, 100),
                                         fill="", outline="#444c56")

    def _draw(self) -> None:
        import tkinter.font as tkfont
        c = self.canvas
        w, h = self._w, self._h
        c.delete("all")
        self._draw_bg(w, h)

        # 字体行高按当前 DPI 实测，布局随系统缩放自适应
        f_title = tkfont.Font(font=_font(11, True))
        f_lab = tkfont.Font(font=_font(9))
        f_small = tkfont.Font(font=_font(8))
        ls_title = f_title.metrics("linespace")
        ls9 = f_lab.metrics("linespace")
        right_x = w - GRIP - 8

        # 标题行：产品名（主）+ 当前套餐（弱化跟随）+ 右更新时间
        updated = f"更新 {datetime.now().strftime('%H:%M')}"
        c.create_text(12, 14, anchor="w", text=PRODUCT_NAME,
                      fill=COLOR_FG, font=_font(11, True))
        plan_name = self.plan.name
        f_bold8 = tkfont.Font(font=_font(8, True))
        plan_text_x = 12 + f_title.measure(PRODUCT_NAME) + 6 + 3 + 4
        avail = right_x - 10 - f_small.measure(updated) - 8 - plan_text_x
        while plan_name and f_bold8.measure(plan_name + "…") > avail:
            plan_name = plan_name[:-1]
        if plan_name != self.plan.name and plan_name:
            plan_name += "…"
        if plan_name:
            # 3px 手绘小圆点分隔，居中于标题行
            cx = 12 + f_title.measure(PRODUCT_NAME) + 6 + 1.5
            c.create_oval(cx - 1.5, 13.5, cx + 1.5, 16.5,
                          fill=COLOR_DIM, outline="")
            c.create_text(plan_text_x, 15, anchor="w", text=plan_name,
                          fill=COLOR_FG, font=_font(8, True))
        c.create_text(w - GRIP - 10, 14, anchor="e", text=updated,
                      fill=COLOR_DIM, font=_font(8))

        snap = self._data[0] if self._data else None
        stats = self._data[1] if self._data else None
        err = self._data[2] if self._data else None

        if err or snap is None or not snap.ok:
            msg = (err or (snap.error if snap else "加载中…"))
            c.create_text(w // 2, h // 2 - 8, text=str(msg)[:46],
                          fill=COLOR_WARN, font=_font(9))
            self._draw_box(w, 132)
            self._draw_grip(w, h)
            return

        # 额度行：第一行 label + 剩余%（含 token 余量），第二行进度条 +
        # 行内倒计时。标签与进度条分层，长标签不再被条形盖住。
        f_bold = tkfont.Font(font=_font(9, True))
        y = 14 + ls_title // 2 + 16
        last_bar_bottom = y
        for usage in ordered_windows(snap.windows):
            color = self._row_color(usage)
            if usage.is_note_window:
                # 余额型窗口（DeepSeek 钱包）：金额直显，不渲染"剩余%"语义
                right = usage.note
            else:
                right = f"剩余 {usage.remaining_percent:.0f}%"
                if usage.units_remaining is not None:
                    right += f"（{fmt_units(usage.units_remaining)}）"
            right_w = f_bold.measure(right)
            label = usage.label
            avail = right_x - 12 - right_w - 10
            while label and f_lab.measure(label + "…") > avail:
                label = label[:-1]
            if label != usage.label and label:
                label += "…"
            c.create_text(12, y, anchor="w", text=label,
                          fill=COLOR_DIM, font=_font(9))
            c.create_text(right_x, y, anchor="e", text=right,
                          fill=color, font=_font(9, True))

            countdown = ""
            if usage.resets_at is not None:
                verb = "到期" if usage.name.startswith("grant") else "重置"
                countdown = fmt_countdown(usage.resets_at, verb=verb)
            bar_w = max(40, right_x - 12
                        - (f_small.measure(countdown) + 10
                           if countdown else 0))
            bar_top = y + ls9 // 2 + 8
            used = max(0.0, min(100.0, usage.used_percent))
            c.create_rectangle(12, bar_top, 12 + bar_w, bar_top + BAR_H,
                               outline="#444c56", fill="#21262b")
            c.create_rectangle(12, bar_top, 12 + int(bar_w * used / 100),
                               bar_top + BAR_H, width=0, fill=color)
            if countdown:
                c.create_text(right_x, bar_top + BAR_H // 2, anchor="e",
                              text=countdown, fill="#8b949e", font=_font(8))
            y = bar_top + BAR_H + 10 + ls9 // 2
            last_bar_bottom = bar_top + BAR_H

        # 底部信息栏：优先锚定在右下角，内容挤不下时跟随最后一行下移
        stats_y = max(h - GRIP - 26, last_bar_bottom + 16)
        if self._bg_image is not None and last_bar_bottom + 6 <= stats_y - 8:
            self._draw_box(w, last_bar_bottom + 6)

        countdown = ""
        for usage in ordered_windows(snap.windows):
            if usage.resets_at is not None:
                verb = "到期" if usage.name.startswith("grant") else "重置"
                countdown = fmt_countdown(usage.resets_at, verb=verb)
                break
        if stats:
            stats_text = str(stats)[:44]
            avail = max(0, w - GRIP - 16)
            while stats_text and f_lab.measure(stats_text + "…") > avail:
                stats_text = stats_text[:-1]
            if stats_text != str(stats)[:44]:
                stats_text = stats_text.rstrip() + "…"
            if stats_text:
                c.create_text(12, stats_y, anchor="w", text=stats_text,
                              fill="#c9d1d9", font=_font(9))
        if countdown:
            c.create_text(w - GRIP - 8, max(h - GRIP - 4, stats_y + 20),
                          anchor="e", text=countdown, fill="#8b949e",
                          font=_font(8))
        self._draw_grip(w, h)

    def _draw_bg(self, w: int, h: int) -> None:
        path = self.cfg.background_image
        if not path:
            return
        try:
            img = Image.open(path).convert("RGB")
        except Exception:
            return
        scale = max(w / img.width, h / img.height)
        new_w = max(w, round(img.width * scale))
        new_h = max(h, round(img.height * scale))
        img = img.resize((new_w, new_h))
        left, top = (new_w - w) // 2, (new_h - h) // 2
        img = img.crop((left, top, left + w, top + h))
        dim = max(0.0, min(0.9, self.cfg.background_dim))
        if dim > 0:
            img = Image.blend(Image.new("RGB", img.size, (0, 0, 0)), img, 1.0 - dim)
        self._bg_image = ImageTk.PhotoImage(img)
        self.canvas.create_image(0, 0, image=self._bg_image, anchor="nw")

    def _draw_grip(self, w: int, h: int) -> None:
        """右下角缩放手柄（三道斜线）。"""
        for i in (0, 5, 10):
            self.canvas.create_line(w - 14 + i, h - 4, w - 4, h - 14 + i,
                                    fill="#667084", width=1)

    # ---- 交互 ----
    def _on_press(self, event) -> None:
        w, h = self._w, self._h
        if event.x >= w - GRIP and event.y >= h - GRIP:
            self._press = (event.x_root, event.y_root, w, h)
            self._mode = "resize"
        else:
            self._press = (event.x_root, event.y_root,
                           self.root.winfo_x(), self.root.winfo_y())
            self._mode = "move"

    def _on_motion(self, event) -> None:
        if self._mode == "move":
            dx = event.x_root - self._press[0]
            dy = event.y_root - self._press[1]
            self.root.geometry(f"+{self._press[2] + dx}+{self._press[3] + dy}")
        elif self._mode == "resize":
            dw = event.x_root - self._press[0]
            dh = event.y_root - self._press[1]
            w = max(MIN_W, min(MAX_W, self._press[2] + dw))
            h = max(MIN_H, min(MAX_H, self._press[3] + dh))
            self._w, self._h = w, h
            self.root.geometry(f"{w}x{h}+{self.root.winfo_x()}+{self.root.winfo_y()}")
            self.canvas.configure(width=w, height=h)
            self._draw()

    def _on_release(self, event) -> None:
        if self._mode == "resize":
            self.cfg.widget_width = self._w
            self.cfg.widget_height = self._h
            self.cfg.save()
        self._mode = None

    def _on_menu(self, event) -> None:
        self._plan_var.set(self.plan.id)
        self._menu.tk_popup(event.x_root, event.y_root)

    def _open_tui(self) -> None:
        """新开一个终端窗口运行 TUI 仪表盘（按平台选择终端）。"""
        if os.name == "nt":
            python = os.path.join(os.path.dirname(sys.executable), "python.exe")
            if not os.path.exists(python):
                python = sys.executable
            subprocess.Popen([python, "-X", "utf8", "-m", "app.main"],
                             cwd=str(APP_DIR),
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
            return
        cmd = (f"cd {shlex.quote(str(APP_DIR))} && "
               f"exec {shlex.quote(sys.executable)} -X utf8 -m app.main")
        if sys.platform == "darwin":
            # macOS：AppleScript 在 Terminal.app 新窗口执行（单引号路径安全）
            subprocess.Popen([
                "osascript", "-e",
                f'tell application "Terminal" to do script "{cmd}"'])
            return
        # Linux：探测常见终端模拟器
        terminal = _find_terminal()
        if terminal:
            subprocess.Popen([*terminal, "bash", "-c", cmd])
            return
        self._notify_no_terminal()

    def _notify_no_terminal(self) -> None:
        """找不到终端模拟器时记录提示。"""
        try:
            APP_DIR.joinpath("data").mkdir(parents=True, exist_ok=True)
            APP_DIR.joinpath("data", "widget_error.log").write_text(
                "打开 TUI 失败：未找到终端模拟器（gnome-terminal/konsole/"
                "xterm 等），请手动在终端运行: python -X utf8 -m app.main\n",
                encoding="utf-8")
        except OSError:
            pass

    def _toggle_topmost(self) -> None:
        self.root.attributes("-topmost", bool(self._topmost_var.get()))
        self.cfg.widget_topmost = bool(self._topmost_var.get())
        self.cfg.save()

    def _switch_plan(self, plan_id: str) -> None:
        if plan_id == self.plan.id:
            return
        self.plan = plans.get_plan(plan_id)
        self.cfg.active_plan = plan_id
        self.cfg.save()
        self._poke()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    try:
        QuotaWidget().run()
    except Exception:
        # pythonw 静默运行时异常不可见，落盘便于排查
        import traceback
        APP_DIR.joinpath("data").mkdir(parents=True, exist_ok=True)
        APP_DIR.joinpath("data", "widget_error.log").write_text(
            traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
