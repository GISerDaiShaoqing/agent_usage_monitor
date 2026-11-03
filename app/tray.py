"""Windows 系统托盘图标（纯 ctypes，零依赖）。

独立 daemon 线程：注册 message-only 窗口（自定义 WNDPROC）→
Shell_NotifyIconW(NIM_ADD) → GetMessage 循环。托盘鼠标事件通过
queue 回传给 Tk 主线程（绝不在此线程碰 Tk 对象）。

接口：
    tray = TrayIcon(events_queue)
    tray.start(tip, color)          注册托盘（结果经 ("ready", ok) 事件回传）
    tray.update(color, tip)         换图标颜色 + tooltip（主线程可调，防重入去重）
    tray.stop()                     移除图标并停线程（阻塞 ≤1.5s）

仅 os.name == "nt" 可用；图标准则 16×16 圆点（手绘 BGRA 字节，
外圈白描边，颜色即当前额度状态色）。
"""
from __future__ import annotations

import ctypes
import os
import queue
import threading
from ctypes import wintypes

WM_CONTEXTMENU = 0x007B
WM_LBUTTONUP = 0x0202
WM_NCCREATE = 0x0081
WM_APP = 0x8000
WM_QUIT = 0x0012

CB_MSG = WM_APP + 1        # 托盘回调消息
WAKE_MSG = WM_APP + 2      # 应用挂起的颜色/tooltip 更新

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
HWND_MESSAGE = -3          # message-only 窗口父句柄常量

_ICON_SIZE = 16


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wintypes.UINT),          # 与 szInfoVersion 重叠
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_ubyte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


# wintypes 在部分 Python 发行版被裁剪（无 WNDCLASSW/WNDPROC），本地定义
_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_longlong, wintypes.HWND, ctypes.c_uint, ctypes.c_uint, ctypes.c_long)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


# ECG 折线（像素中心坐标系，0..16；与圆点同圆心 7.5）：
# 平基线 → 小坑 → 主峰 → 回落 → 平基线，峰顶略偏左避开正中
_PULSE_PATH = (
    (1.5, 8.0), (4.6, 8.0), (5.6, 10.2), (6.5, 5.0),
    (7.4, 10.6), (8.4, 8.0), (13.5, 8.0),
)


def _pulse_dist(x: float, y: float) -> float:
    """点到心率折线的最短距离。"""
    best = 1e9
    for (x1, y1), (x2, y2) in zip(_PULSE_PATH, _PULSE_PATH[1:]):
        dx, dy = x2 - x1, y2 - y1
        t = ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))
        best = min(best, ((x - (x1 + t * dx)) ** 2
                          + (y - (y1 + t * dy)) ** 2) ** 0.5)
    return best


def _icon_bytes(color: str) -> tuple[bytes, bytes]:
    """16×16 圆点 + 白描边 + 白色心率折线 → (XOR BGRA 32bpp, AND 掩码)。

    圆内 d<=6.5 实色、6.5<d<=7.5 白描边、圆外 alpha=0；AND 全 0 = 不截色。
    心率线优先于底色（圆内 d<=7.2 且距折线 <=0.9 时像素置白）。
    """
    r, g, b = _hex_to_rgb(color)
    xor = bytearray()
    for y in range(_ICON_SIZE):
        for x in range(_ICON_SIZE):
            d = ((x - 7.5) ** 2 + (y - 7.5) ** 2) ** 0.5
            if d > 7.5:
                xor += bytes((0, 0, 0, 0))
            elif d <= 7.2 and _pulse_dist(x, y) <= 0.9:
                xor += bytes((255, 255, 255, 255))   # 白色心率线
            elif d > 6.5:
                xor += bytes((255, 255, 255, 255))   # 白描边
            else:
                xor += bytes((b, g, r, 255))
    # XOR 位图行序上下对称，无需倒序
    return bytes(xor), bytes(_ICON_SIZE * _ICON_SIZE // 8)


class TrayIcon:
    """图标状态句柄（创建于主线程，工作由内部 daemon 线程完成）。"""

    def __init__(self, events: queue.Queue, tip_default: str = "DaiPulse"):
        self._events = events
        self._tip_default = tip_default
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._shell = ctypes.windll.shell32
        self._shell.Shell_NotifyIconW.argtypes = [
            ctypes.c_uint, ctypes.POINTER(NOTIFYICONDATAW)]

        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._hwnd: wintypes.HWND | None = None
        self._hinstance = self._kernel32.GetModuleHandleW(None)
        self._hicon: wintypes.HICON | None = None
        self._nid = NOTIFYICONDATAW()
        self._pending: tuple[str, str] | None = None
        self._applied: tuple[str, str] | None = None
        self._ready = False

    # ---- 主线程接口 ----
    def start(self, tip: str = "", color: str = "#8b949e") -> None:
        t = threading.Thread(target=self._run, args=(tip or self._tip_default, color),
                             daemon=True, name="ocgo-tray")
        self._thread = t
        t.start()

    def update(self, color: str, tip: str) -> None:
        """换颜色/tooltip。与已应用内容相同则忽略（防 NIF_MODIFY 刷屏）。"""
        if not self._ready:
            return
        pinned = (color, tip or self._tip_default)
        if self._pending == pinned or self._applied == pinned:
            return
        self._pending = pinned
        if self._hwnd:
            self._user32.PostMessageW(self._hwnd, WAKE_MSG, 0, 0)

    def stop(self) -> None:
        if self._thread_id:
            self._user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(1.5)

    # ---- 托盘线程内部 ----
    def _run(self, tip: str, color: str) -> None:
        self._thread_id = self._kernel32.GetCurrentThreadId()
        wndproc = self._make_wndproc()
        cls_name = f"ocgo_tray_{os.getpid()}"

        wc = WNDCLASSW()
        wc.lpfnWndProc = wndproc
        wc.hInstance = self._hinstance
        wc.lpszClassName = cls_name
        if not self._user32.RegisterClassW(ctypes.byref(wc)):
            self._events.put(("ready", False))
            return
        try:
            self._hwnd = self._user32.CreateWindowExW(
                0, cls_name, "ocgo-tray", 0, 0, 0, 0, 0,
                wintypes.HWND(HWND_MESSAGE), 0, self._hinstance, None)
            if not self._hwnd:
                self._events.put(("ready", False))
                return

            self._set_icon(color)
            self._fill_nid(tip)
            ok = bool(self._shell.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid)))
            self._ready = ok
            self._applied = (color, tip or self._tip_default)
            self._events.put(("ready", ok))
            if not ok:
                return

            msg = wintypes.MSG()
            while self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                self._user32.TranslateMessage(ctypes.byref(msg))
                self._user32.DispatchMessageW(ctypes.byref(msg))
            # WM_QUIT 退出：清理图标与窗口（窗口必须由其线程销毁）
            self._user32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._user32.DestroyWindow(self._hwnd)
        except Exception:
            self._events.put(("ready", False))
        finally:
            self._user32.UnregisterClassW(cls_name, self._hinstance)

    def _make_wndproc(self):
        user32 = self._user32
        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, wintypes.HWND, ctypes.c_uint,
            ctypes.c_uint, ctypes.c_long)

        def proc(hwnd, msg, wparam, lparam):
            if msg == WM_NCCREATE:
                # 必须返回 1，窗口创建才会继续（这里的 DefWindowProc
                # 经 ctypes 回调链路对创建消息不可靠，显式放行）
                return 1
            if msg == CB_MSG:
                ev = lparam & 0xFFFFFFFF
                if ev == WM_LBUTTONUP:
                    self._events.put(("toggle",))
                elif ev == WM_CONTEXTMENU:
                    x = ctypes.c_short(ev & 0xFFFF).value
                    y = ctypes.c_short((ev >> 16) & 0xFFFF).value
                    self._events.put(("menu", x or 0, y or 0))
                return 0
            if msg == WAKE_MSG:
                self._apply_pending()
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._keep_proc = proc            # 防 ctypes 回调被 GC
        return _WNDPROC(proc)

    def _set_icon(self, color: str) -> None:
        xor, mask = _icon_bytes(color)
        if self._hicon:
            self._user32.DestroyIcon(self._hicon)
            self._hicon = None
        self._hicon = self._user32.CreateIcon(
            self._hinstance, _ICON_SIZE, _ICON_SIZE, 1, 32, mask, xor)

    def _fill_nid(self, tip: str) -> None:
        nid = self._nid
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = CB_MSG
        nid.hIcon = self._hicon
        nid.szTip = (tip or self._tip_default)[:127]

    def _apply_pending(self) -> None:
        pinned, self._pending = self._pending, None
        if pinned is None:
            return
        color, tip = pinned
        if self._hwnd is None:
            return
        try:
            self._set_icon(color)
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self._hwnd
            nid.uID = 1
            nid.uFlags = NIF_ICON | NIF_TIP
            nid.hIcon = self._hicon
            nid.szTip = (tip or self._tip_default)[:127]
            self._shell.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
            self._applied = pinned
        except Exception:
            # 非法颜色等：跳过本次，保留旧图标；回调里绝不让异常外逃
            pass
