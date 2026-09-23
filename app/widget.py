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
from .zen_client import QuotaSnapshot

APP_DIR = Path(__file__).resolve().parent.parent

MIN_W, MIN_H = 280, 170
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
        self.root.title("OpenCode Go 额度小部件")
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
        self._menu.add_command(label="切换套餐", command=self._cycle_plan)
        self._menu.add_command(label="打开 TUI 仪表盘", command=self._open_tui)
        self._menu.add_separator()
        self._topmost_var = tk.BooleanVar(value=self.cfg.widget_topmost)
        self._menu.add_checkbutton(label="置顶", variable=self._topmost_var,
                                   command=self._toggle_topmost)
        self._menu.add_command(label="退出", command=self.root.destroy)

        self._bg_image: ImageTk.PhotoImage | None = None
        self._data = None
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
        if not rep.rows:
            return "7 天暂无用量"
        rows = sorted(rep.rows, key=lambda r: r.processed, reverse=True)
        total_req = sum(r.requests for r in rep.rows)
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
        try:
            while True:
                self._data = self._q.get_nowait()
        except queue.Empty:
            pass
        self._draw()
        self.root.after(500, self._pump)

    def _tick_60s(self) -> None:
        self._draw()
        self.root.after(60000, self._tick_60s)

    # ---- 绘制 ----
    def _draw(self) -> None:
        c = self.canvas
        w, h = self._w, self._h
        c.delete("all")
        self._draw_bg(w, h)

        # 标题行
        c.create_text(12, 14, anchor="w", text=f"{self.plan.name}",
                      fill=COLOR_FG, font=_font(11, True))
        updated = datetime.now().strftime("%H:%M")
        c.create_text(w - GRIP - 10, 14, anchor="e", text=f"更新于 {updated}",
                      fill=COLOR_DIM, font=_font(8))

        snap = self._data[0] if self._data else None
        stats = self._data[1] if self._data else None
        err = self._data[2] if self._data else None

        if err or snap is None or not snap.ok:
            msg = (err or (snap.error if snap else "加载中…"))
            c.create_text(w // 2, h // 2 - 8, text=str(msg)[:46],
                          fill=COLOR_WARN, font=_font(9))
            self._draw_grip(w, h)
            return

        y = 42
        for name in ("rolling", "weekly", "monthly"):
            usage = snap.windows.get(name)
            if usage is None:
                continue
            label = {"rolling": "5 小时", "weekly": "每周", "monthly": "每月"}[name]
            c.create_text(12, y - 6, anchor="w", text=label,
                          fill=COLOR_DIM, font=_font(9))
            bar_w = w - 100
            used = max(0.0, min(100.0, usage.used_percent))
            fill = (COLOR_WARN if (usage.is_limited or usage.is_low)
                    else COLOR_MID if usage.remaining_percent <= 40
                    else COLOR_OK)
            c.create_rectangle(56, y - 8, 56 + bar_w, y - 8 + BAR_H,
                               outline="#444c56", fill="#21262b")
            c.create_rectangle(56, y - 8, 56 + int(bar_w * used / 100),
                               y - 8 + BAR_H, width=0, fill=fill)
            c.create_text(w - GRIP - 8, y - 3, anchor="e",
                          text=f"剩余 {usage.remaining_percent:.0f}%",
                          fill=fill, font=_font(9, True))
            y += 34

        if stats:
            c.create_text(12, h - GRIP - 4, anchor="w", text=str(stats)[:44],
                          fill="#c9d1d9", font=_font(9))
        countdown = ""
        if snap.windows.get("rolling") and snap.windows["rolling"].resets_at:
            countdown = fmt_countdown(snap.windows["rolling"].resets_at)
        if countdown:
            c.create_text(w - GRIP - 8, h - GRIP - 4, anchor="e",
                          text=countdown, fill="#8b949e",
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
        self.canvas.create_rectangle(0, 28, w, 132, fill="", outline="#444c56")

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

    def _cycle_plan(self) -> None:
        self.plan = plans.cycle_plan(self.plan.id)
        self.cfg.active_plan = self.plan.id
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
