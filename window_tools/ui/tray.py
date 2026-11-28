"""系统托盘图标封装（Shell_NotifyIcon，纯 ctypes，无第三方依赖）。

提供：添加/更新/删除托盘图标，右键菜单，图标来自应用 ICO 资源。
托盘消息（WM_TRAYICON）由宿主窗体在 WndProc 中分发到本模块的回调。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Callable

import win32con
import win32gui

_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010

# 托盘图标回调消息：WM_USER 之后自定义
WM_TRAYICON = win32con.WM_USER + 100
# 托盘菜单命令 ID（避开窗体控件 ID_BASE=100 段）
TRAY_SHOW = 2001
TRAY_HIDE = 2002
TRAY_EXIT = 2003
TRAY_RESCUE = 2004
TRAY_MOUSE_TOGGLE = 2005

_shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
_shell32.Shell_NotifyIconW.restype = wintypes.BOOL


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class TrayIcon:
    """管理单个托盘图标的生命周期。"""

    def __init__(self, hwnd: int, icon_path: str, tip: str):
        self.hwnd = hwnd
        self.tip = tip
        self.uid = 1
        self.added = False
        self._hwnd_icon_owned = False
        # 从 ICO 文件加载 32x32 图标句柄，窗口与托盘共用同一资源
        self.hicon = win32gui.LoadImage(
            0, icon_path, win32con.IMAGE_ICON, 32, 32,
            win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE,
        )
        if not self.hicon:
            # 回退到系统默认应用图标
            self.hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

    def _data(self, flags: int) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self.hwnd
        data.uID = self.uid
        data.uFlags = flags
        if flags & NIF_MESSAGE:
            data.uCallbackMessage = WM_TRAYICON
        if flags & NIF_ICON:
            data.hIcon = self.hicon
        if flags & NIF_TIP:
            data.szTip = self.tip[:127]
        return data

    def add(self) -> bool:
        ok = bool(_shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(
            self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP))))
        self.added = ok
        return ok

    def update_tip(self, tip: str) -> None:
        if not self.added:
            return
        self.tip = tip
        _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(
            self._data(NIF_TIP)))

    def notify(self, message: str, title: str | None = None) -> None:
        """显示气泡通知。"""
        if not self.added:
            return
        data = self._data(NIF_INFO)
        data.szInfo = message[:255]
        data.szInfoTitle = (title or self.tip)[:63]
        data.dwInfoFlags = 0x00000001  # NIIF_INFO
        data.uTimeoutOrVersion = 5000
        _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    def remove(self) -> None:
        if self.added:
            data = self._data(0)
            _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
            self.added = False


class TrayMenu:
    """右键弹出菜单（原生菜单，简短命令列表）。"""

    def __init__(self, hwnd: int, callback: Callable[[int], None]):
        self.hwnd = hwnd
        self.callback = callback
        self.menu = 0

    def show(self, visible: bool, mouse_locked: bool = False) -> None:
        menu = win32gui.CreatePopupMenu()
        try:
            show_flags = win32con.MF_STRING | (win32con.MF_GRAYED if visible else 0)
            hide_flags = win32con.MF_STRING | (win32con.MF_GRAYED if not visible else 0)
            win32gui.AppendMenu(menu, show_flags, TRAY_SHOW, "显示窗口")
            win32gui.AppendMenu(menu, hide_flags, TRAY_HIDE, "隐藏到托盘")
            # 救援始终可用：窗口越界时主界面点不到，正需要托盘入口救回
            win32gui.AppendMenu(menu, win32con.MF_STRING, TRAY_RESCUE,
                                "救援越界窗口")
            # 鼠标锁定/解锁共用一个切换命令，按当前状态显示文字，避免歧义
            mouse_label = "解锁鼠标" if mouse_locked else "锁定鼠标"
            win32gui.AppendMenu(menu, win32con.MF_STRING, TRAY_MOUSE_TOGGLE,
                                mouse_label)
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(menu, win32con.MF_STRING, TRAY_EXIT, "退出程序")
            # 先置前台，否则菜单可能不弹出；前台锁定可能失败，忽略即可
            try:
                _user32.SetForegroundWindow(ctypes.c_void_p(self.hwnd))
            except Exception:
                pass
            pos = win32gui.GetCursorPos()
            cmd = win32gui.TrackPopupMenu(
                menu, win32con.TPM_RETURNCMD | win32con.TPM_RIGHTALIGN,
                pos[0], pos[1], 0, self.hwnd, None,
            )
            win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
            if cmd:
                self.callback(cmd)
        finally:
            win32gui.DestroyMenu(menu)
