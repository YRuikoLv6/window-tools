"""屏幕左上角 toast 提示窗。

独立的无焦点置顶层窗，圆角卡片样式与主界面一致，显示数秒后自动消失。
用于鼠标锁定/解锁等操作反馈：即使主窗口隐藏到托盘也能看到提示。
"""
from __future__ import annotations

import ctypes

import win32api
import win32con
import win32gui

from . import gdi
from .gdi import C, ALIGN_CENTER

_user32 = ctypes.windll.user32
_user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                             ctypes.c_void_p]
_user32.SetTimer.restype = ctypes.c_uint
_user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_user32.KillTimer.restype = ctypes.c_bool


class ToastWindow:
    """单实例 toast 窗口；创建后常驻隐藏，show() 弹出并自动收起。"""

    CLASS_NAME = "WindowToolsToast"
    LOGICAL_W = 360
    LOGICAL_H = 88
    EDGE = 16
    DURATION_MS = 3500
    TIMER_ID = 77
    # 透明键：COLORREF 0x00BBGGRR 与 GDI+ ARGB 0xAARRGGBB 指向同一洋红像素
    MAGENTA_FILL = gdi.argb(255, 255, 0, 255)
    MAGENTA_KEY = 0x00FF00FF

    def __init__(self, log):
        self.log = log
        self.hwnd = 0
        self.scale = 1.0
        self.title = ""
        self.message = ""
        self.tone = "info"
        self._width = 0
        self._height = 0
        self._create()

    def _create(self) -> None:
        self.scale = max(1.0, float(gdi.UI_SCALE))
        self._width = int(self.LOGICAL_W * self.scale)
        self._height = int(self.LOGICAL_H * self.scale)
        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = self._wnd_proc
        wc.lpszClassName = self.CLASS_NAME
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.hbrBackground = 0
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        try:
            win32gui.RegisterClass(wc)
        except win32gui.error:
            pass
        ex_style = (win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_TOPMOST
                    | win32con.WS_EX_NOACTIVATE | win32con.WS_EX_LAYERED)
        self.hwnd = win32gui.CreateWindowEx(
            ex_style, self.CLASS_NAME, "", win32con.WS_POPUP,
            0, 0, self._width, self._height, 0, 0, wc.hInstance, None,
        )
        win32gui.SetLayeredWindowAttributes(
            self.hwnd, self.MAGENTA_KEY, 0, win32con.LWA_COLORKEY)

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_PAINT:
            self._paint(hwnd)
            return 0
        if msg == win32con.WM_TIMER and wparam == self.TIMER_ID:
            _user32.KillTimer(hwnd, self.TIMER_ID)
            win32gui.ShowWindow(hwnd, win32con.SW_HIDE)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _paint(self, hwnd) -> None:
        hdc, ps = win32gui.BeginPaint(hwnd)
        try:
            w, h = self.LOGICAL_W, self.LOGICAL_H
            with gdi.Canvas(hdc, w, h, self.scale) as c:
                c.clear(self.MAGENTA_FILL)
                c.rounded(3, 5, w - 6, h - 8, 14, C.SHADOW)
                c.rounded(1, 1, w - 2, h - 2, 13, C.CARD, C.BORDER)
                accents = {
                    "success": (C.SUCCESS, C.SUCCESS_SOFT, "✓"),
                    "warning": (C.WARNING, C.WARNING_SOFT, "!"),
                }
                accent, soft, glyph = accents.get(
                    self.tone, (C.PRIMARY, C.PRIMARY_SOFT, "i"))
                c.rounded(1, 1, 5, h - 2, 2, accent)
                cy = (h - 34) / 2.0
                c.ellipse(18, cy, 34, 34, soft)
                c.text(glyph, 18, cy, 34, 34, 15, accent, True, ALIGN_CENTER)
                c.text(self.title, 64, 12, w - 78, 24, 13, C.TEXT, True)
                c.text(self.message, 64, 38, w - 78, 34, 11, C.TEXT_SUB,
                       False, ellipsis=False)
        finally:
            win32gui.EndPaint(hwnd, ps)

    def show(self, title: str, message: str, tone: str = "info") -> None:
        if not self.hwnd:
            return
        self.scale = max(1.0, float(gdi.UI_SCALE))
        self._width = int(self.LOGICAL_W * self.scale)
        self._height = int(self.LOGICAL_H * self.scale)
        self.title, self.message, self.tone = str(title), str(message), tone
        # 主屏工作区左上角 + 边距，避开任务栏
        try:
            work = win32api.SystemParametersInfo(win32con.SPI_GETWORKAREA)
            x, y = int(work[0]) + self.EDGE, int(work[1]) + self.EDGE
        except Exception:
            x = y = self.EDGE
        win32gui.SetWindowPos(
            self.hwnd, win32con.HWND_TOPMOST, x, y, self._width, self._height,
            win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
        )
        win32gui.InvalidateRect(self.hwnd, None, True)
        win32gui.UpdateWindow(self.hwnd)
        _user32.KillTimer(self.hwnd, self.TIMER_ID)
        _user32.SetTimer(self.hwnd, self.TIMER_ID, self.DURATION_MS, None)

    def close(self) -> None:
        if self.hwnd:
            _user32.KillTimer(self.hwnd, self.TIMER_ID)
            win32gui.DestroyWindow(self.hwnd)
            self.hwnd = 0
