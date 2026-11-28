"""原生 Win32 自绘窗体宿主（不依赖浏览器、不依赖第三方 GUI 库）。

视觉：GDI+ 绘制的现代化界面——深色标题栏、指标卡片、工具卡片、彩色按钮、
      自绘通知与窗口列表；支持 hover/press、滚轮滚动、窗口拖动与边缘缩放。

插件化约定（保持不变）：
- 每个工具通过 get_ui_actions() 声明按钮，宿主自动生成工具卡片与按钮；
- 按钮点击统一走 tool.handle_action(action_id)，新增工具无需改动本文件；
- 主线程消息循环同时负责热键、WM_DISPLAYCHANGE 与数据刷新。
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from collections import deque
from typing import Dict, List, Tuple

import win32api
import win32con
import win32gui

from .. import __version__
from ..paths import ICON_PATH
from ..ui import gdi
from ..ui.gdi import C
from ..ui.toast import ToastWindow
from ..ui.tray import (
    TRAY_EXIT, TRAY_HIDE, TRAY_MOUSE_TOGGLE, TRAY_RESCUE, TRAY_SHOW,
    TrayIcon, TrayMenu, WM_TRAYICON,
)
from ..utils import autostart, shortcut, winapi
from ..utils.hotkeys import (
    MOD_NOREPEAT, format_hotkey, is_modifier_vk, pressed_modifiers,
    reject_reason,
)

# 单实例唤醒协议：第二实例定位已有窗口后投递 WM_SHOWME，由已有实例显示窗口。
# WM_TRAYICON 占用 WM_USER + 100，这里错开避免冲突。
WM_SHOWME = win32con.WM_USER + 101
WINDOW_CLASS = "WindowToolsDesktop"
DEFAULT_TITLE = "窗口工具集合"
WINDOW_TITLE = f"{DEFAULT_TITLE} v{__version__}"

ID_BASE = 100
TIMER_REFRESH = 1
TIMER_DISPLAY = 2
REFRESH_MS = 800
DISPLAY_DEBOUNCE_MS = 1500
SC_MONITORPOWER = getattr(win32con, "SC_MONITORPOWER", 0xF170)

TITLE_H = 58
PAD = 20
NAV_W = 208
NAV_ROW = 54
SETTINGS_ID = "__settings__"
CARD_RADIUS = 12
BTN_H = 48
BTN_GAP_X = 10
BTN_GAP_Y = 10
LIST_ROW_NOTICE = 44
LIST_ROW_WINDOW = 48
FOOTER_H = 36
SETTINGS_TOOL_ROW = 44
SETTINGS_KEY_ROW = 64

_user32 = ctypes.windll.user32
_user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                             ctypes.c_void_p]
_user32.SetTimer.restype = ctypes.c_uint
_user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_user32.KillTimer.restype = ctypes.c_bool

WM_MOUSELEAVE = 0x02A2
_TME_LEAVE = 0x00000002


class _TrackMouseEvent(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwFlags", ctypes.c_uint),
        ("hwndTrack", ctypes.c_void_p),
        ("dwHoverTime", ctypes.c_uint),
    ]


_user32.TrackMouseEvent.argtypes = [ctypes.POINTER(_TrackMouseEvent)]
_user32.TrackMouseEvent.restype = ctypes.c_bool


def _set_timer(hwnd: int, timer_id: int, milliseconds: int) -> None:
    _user32.SetTimer(hwnd, timer_id, milliseconds, None)


def _kill_timer(hwnd: int, timer_id: int) -> None:
    _user32.KillTimer(hwnd, timer_id)


def _track_mouse_leave(hwnd: int) -> None:
    info = _TrackMouseEvent()
    info.cbSize = ctypes.sizeof(info)
    info.dwFlags = _TME_LEAVE
    info.hwndTrack = hwnd
    info.dwHoverTime = 0
    _user32.TrackMouseEvent(ctypes.byref(info))


def _try_foreground(hwnd: int) -> None:
    """尽力把窗口置前；Windows 前台锁定可能导致失败，忽略即可。"""
    try:
        _user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
    except Exception:
        pass


def wake_existing_window() -> bool:
    """单实例场景：定位已有实例的窗口并请求其显示。

    返回是否成功定位并投递消息。窗口隐藏到托盘后 FindWindow 仍能找到，
    因为它只要求窗口存在、不要求可见。
    """
    hwnd = 0
    try:
        hwnd = win32gui.FindWindow(WINDOW_CLASS, None)
    except Exception:
        pass
    if not hwnd:
        # 兼容仍在运行的旧版本（类名含动态 PID）或窗口标题被定制的情况
        try:
            hwnd = win32gui.FindWindow(None, WINDOW_TITLE)
        except Exception:
            pass
    if not hwnd:
        return False
    # 直接跨进程恢复显示：即使对方是旧版本、不认识 WM_SHOWME 也能被唤醒
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.BringWindowToTop(hwnd)
    except Exception:
        pass
    # 再投递消息，让新版本补做置前与通知（防御性，失败也不影响显示）
    try:
        win32gui.PostMessage(hwnd, WM_SHOWME, 0, 0)
    except Exception:
        pass
    return True


class DesktopApp:
    def __init__(self, log, config, title: str = "窗口工具集合"):
        self.log = log
        self.config = config
        self.title = title
        self.manager = None
        self.hwnd = 0
        self.scale = 1.0
        self.dpi_mode = ""
        self.last_display_change = 0.0
        self._last_signature = None
        self.notices: deque = deque(maxlen=60)

        self._controls_ready = False
        self._next_id = ID_BASE
        self._action_map: Dict[int, Tuple[str, str]] = {}
        self._hotkey_ids: dict = {}
        self._hotkey_callbacks: dict = {}
        self._next_hotkey = 1
        self._bindings: dict = {}

        self._tool_blocks: List[dict] = []
        self._hotspots: List[dict] = []
        self._list_rects: dict = {}
        self._hover = None
        self._pressed = None
        self._selected_tool = ""
        self._nav_scroll = 0
        self._action_scroll = 0
        self._settings_scroll = 0
        self._settings_nav_scroll = 0
        self._settings_hotkey_tool = ""
        self._settings_category = "通用"
        self._settings_return_tool = ""
        self._notice_scroll = 0
        self._window_scroll = 0
        self._notice_lines: List[dict] = []
        self._window_lines: List[dict] = []
        self._pointer: Tuple[int, int] | None = None
        self._tracking_leave = False
        self._notice_hits: List[dict] = []
        self._pending_tip: dict | None = None

        self.tray: TrayIcon | None = None
        self.tray_menu: TrayMenu | None = None
        self.toast: ToastWindow | None = None

        # 关闭确认对话框（模态覆盖层）与托盘“退出程序”直通标志
        self._dialog: dict | None = None
        self._force_exit = False

    # -------------------------------------------------------- 对外接口 --
    def set_manager(self, manager) -> None:
        self.manager = manager

    def register_hotkey(self, key: str, modifiers: int, vk: int, callback) -> bool:
        """在主线程调用，注册结果同步返回。"""
        if not self.hwnd:
            return False
        previous = self._hotkey_ids.get(key)
        if previous is not None:
            try:
                win32gui.UnregisterHotKey(self.hwnd, previous)
            except Exception:
                pass
            self._hotkey_callbacks.pop(previous, None)
        hid = self._next_hotkey
        try:
            win32gui.RegisterHotKey(
                self.hwnd, hid, int(modifiers) | MOD_NOREPEAT, vk)
        except Exception as exc:
            self.log.warning("热键注册失败 key=%s: %s", key, exc)
            return False
        self._next_hotkey += 1
        self._hotkey_ids[key] = hid
        self._hotkey_callbacks[hid] = callback
        return True

    def bind_hotkey(self, binding_id: str, tool: str, title: str, callback,
                    defaults: list) -> str:
        """登记一个功能的默认快捷键，并按用户自定义或默认顺序注册。"""
        self._bindings[binding_id] = {
            "id": binding_id,
            "tool": tool,
            "title": title,
            "callback": callback,
            "defaults": [(int(mods), int(vk)) for mods, vk in defaults],
            "label": "",
            "modifiers": 0,
            "vk": 0,
        }
        return self._apply_binding(binding_id)

    def hotkey_label(self, binding_id: str) -> str:
        binding = self._bindings.get(binding_id) or {}
        return binding.get("label") or ""

    def hotkey_rows(self, tool_name: str) -> list:
        rows = []
        for binding in self._bindings.values():
            if binding["tool"] != tool_name:
                continue
            default = ""
            if binding["defaults"]:
                default = format_hotkey(*binding["defaults"][0])
            rows.append({
                "id": binding["id"],
                "title": binding["title"],
                "label": binding.get("label") or "未注册",
                "default": default,
                "custom": self._saved_hotkey(binding["id"]) is not None,
            })
        return rows

    def _saved_hotkey(self, binding_id: str):
        data = self.config.get("hotkeys") or {}
        item = data.get(binding_id) if isinstance(data, dict) else None
        if not isinstance(item, dict):
            return None
        try:
            return int(item["modifiers"]), int(item["vk"])
        except (KeyError, TypeError, ValueError):
            return None

    def _combo_taken(self, binding_id: str, modifiers: int, vk: int) -> bool:
        for other_id, other in self._bindings.items():
            if other_id == binding_id or not other.get("label"):
                continue
            if other.get("modifiers") == modifiers and other.get("vk") == vk:
                return True
        return False

    def _apply_binding(self, binding_id: str) -> str:
        """有自定义组合时只注册它，默认组合不再响应。

        自定义注册失败时也不退回默认，避免两套快捷键同时生效。
        未自定义时才按默认候选依次尝试。
        """
        binding = self._bindings[binding_id]
        saved = self._saved_hotkey(binding_id)
        if saved:
            modifiers, vk = saved
            if (not self._combo_taken(binding_id, modifiers, vk)
                    and self.register_hotkey(
                        binding_id, modifiers, vk, binding["callback"])):
                binding["modifiers"] = modifiers
                binding["vk"] = vk
                binding["label"] = format_hotkey(modifiers, vk)
                return binding["label"]
            binding["label"] = "未生效"
            binding["modifiers"] = 0
            binding["vk"] = 0
            self.notify(
                "%s 的自定义快捷键无法使用，默认快捷键已停用" % binding["title"],
                "快捷键",
            )
            self.log.warning("自定义快捷键未能注册: %s", binding_id)
            return ""

        for modifiers, vk in binding["defaults"]:
            if self._combo_taken(binding_id, modifiers, vk):
                continue
            if self.register_hotkey(
                    binding_id, modifiers, vk, binding["callback"]):
                binding["modifiers"] = modifiers
                binding["vk"] = vk
                binding["label"] = format_hotkey(modifiers, vk)
                return binding["label"]
        binding["label"] = ""
        binding["modifiers"] = 0
        binding["vk"] = 0
        self.log.warning("快捷键未能注册: %s", binding_id)
        return ""

    def set_hotkey(self, binding_id: str, modifiers: int, vk: int) -> bool:
        binding = self._bindings.get(binding_id)
        if not binding:
            return False
        modifiers = int(modifiers) & ~MOD_NOREPEAT
        reason = reject_reason(modifiers, vk)
        if reason:
            self.notify(reason, "快捷键")
            return False
        if self._combo_taken(binding_id, modifiers, vk):
            self.notify("这个快捷键已经分给了其他功能", "快捷键")
            return False
        if not self.register_hotkey(
                binding_id, modifiers, vk, binding["callback"]):
            self.notify("这个快捷键已被其他程序占用", "快捷键")
            self._apply_binding(binding_id)
            return False
        hotkeys = dict(self.config.get("hotkeys") or {})
        first = binding["defaults"][:1]
        if first and (modifiers, vk) == first[0]:
            hotkeys.pop(binding_id, None)
        else:
            hotkeys[binding_id] = {"modifiers": modifiers, "vk": vk}
        self.config.data["hotkeys"] = hotkeys
        if not self.config.save():
            self.notify("快捷键已切换，但写入配置失败", "快捷键")
        binding["modifiers"] = modifiers
        binding["vk"] = vk
        binding["label"] = format_hotkey(modifiers, vk)
        self.notify("%s 快捷键已改为 %s" % (
            binding["title"], binding["label"]), "快捷键")
        self._refresh()
        return True

    def reset_hotkey(self, binding_id: str) -> None:
        binding = self._bindings.get(binding_id)
        if not binding:
            return
        hotkeys = dict(self.config.get("hotkeys") or {})
        hotkeys.pop(binding_id, None)
        self.config.data["hotkeys"] = hotkeys
        self.config.save()
        label = self._apply_binding(binding_id)
        self.notify("%s 已恢复默认快捷键 %s" % (
            binding["title"], label or "未注册"), "快捷键")
        self._refresh()

    def _capture_hotkey(self, vk: int) -> None:
        if (self._dialog or {}).get("kind") != "hotkey":
            return
        if is_modifier_vk(vk) or vk in (win32con.VK_ESCAPE, 0):
            return
        modifiers = pressed_modifiers()
        reason = reject_reason(modifiers, vk)
        if reason:
            self.notify(reason, "快捷键")
            return
        if self.set_hotkey(self._dialog.get("id"), modifiers, vk):
            self._close_dialog()

    def notify(self, message: str, title: str | None = None) -> None:
        title = title or self.title
        self.notices.append({
            "time": time.strftime("%H:%M:%S"),
            "title": title,
            "message": str(message),
        })
        self.log.info("[通知] %s - %s", title, message)
        self._invalidate()

    def show_toast(self, message: str, title: str | None = None,
                   tone: str = "info") -> None:
        """屏幕左上角悬浮提示；与窗口内通知列表独立。"""
        if not self.toast:
            return
        try:
            self.toast.show(title or self.title, message, tone)
        except Exception:
            self.log.exception("左上角提示显示失败")

    def note_display_change(self) -> None:
        self.last_display_change = time.time()

    def _schedule_display_change(self, reason: str) -> None:
        """统一去抖入口：WM_DISPLAYCHANGE、轮询和电源消息都走这里。"""
        self.note_display_change()
        if self.hwnd:
            _set_timer(self.hwnd, TIMER_DISPLAY, DISPLAY_DEBOUNCE_MS)
        self.log.info("检测到显示器变化（%s）", reason)

    def on_display_change(self) -> None:
        signature = winapi.screen_signature()
        self.log.info("显示器变化去抖完成，分发工具事件")
        if self.manager:
            self.manager.on_display_change(signature)

    # ---------------------------------------------------------- 启动 ----
    def run(self) -> None:
        self.dpi_mode = winapi.enable_dpi_awareness()
        self.log.info("DPI 模式: %s", self.dpi_mode)
        if not gdi.startup():
            self.log.warning("GDI+ 启动失败，界面可能显示异常")

        # 按主屏 DPI 缩放整个界面：逻辑坐标统一，绘制时放大到物理像素
        try:
            screen_hdc = win32gui.GetDC(0)
            dpi_x = ctypes.windll.gdi32.GetDeviceCaps(screen_hdc, 88)
            win32gui.ReleaseDC(0, screen_hdc)
            scale = max(1.0, min(2.0, dpi_x / 96.0))
        except Exception:
            scale = 1.0
        gdi.set_scale(scale)
        self.scale = scale
        self.log.info("UI 缩放: %.2f (DPI=%s)", scale, dpi_x)

        # 固定类名：单实例由 Mutex 保证，便于第二实例据此定位已有窗口
        class_name = WINDOW_CLASS
        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = self._wnd_proc
        wc.lpszClassName = class_name
        wc.style = (win32con.CS_HREDRAW | win32con.CS_VREDRAW
                    | 0x00020000)  # CS_DROPSHADOW
        wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
        try:
            wc.hIcon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        except Exception:
            wc.hIcon = 0
        try:
            win32gui.RegisterClass(wc)
        except win32gui.error:
            pass

        # 逻辑尺寸 1120×760，物理尺寸按 DPI 放大，且不超过屏幕
        screen_w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        screen_h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        width = int(min(1120 * scale, screen_w - 40))
        height = int(min(760 * scale, screen_h - 40))
        left = max(0, (screen_w - width) // 2)
        top = max(0, (screen_h - height) // 2)

        style = (win32con.WS_POPUP | win32con.WS_THICKFRAME
                 | win32con.WS_CLIPSIBLINGS | win32con.WS_CLIPCHILDREN)
        self.hwnd = win32gui.CreateWindowEx(
            0, class_name, f"{self.title} v{__version__}", style,
            left, top, width, height, 0, 0, wc.hInstance, None,
        )
        self._apply_window_icon()

        # 托盘图标：与窗口共用 ICO 资源，最小化后仍可快速唤出
        try:
            self.tray = TrayIcon(self.hwnd, ICON_PATH,
                                 f"{self.title} v{__version__}")
            if self.tray.add():
                self.tray_menu = TrayMenu(self.hwnd, self._on_tray_command)
                self.log.info("系统托盘图标已添加: %s", ICON_PATH)
            else:
                self.log.warning("系统托盘图标添加失败")
        except Exception:
            self.log.exception("系统托盘初始化失败")

        # 左上角 toast：供鼠标锁定/解锁等操作给出悬浮反馈
        try:
            self.toast = ToastWindow(self.log)
            self.log.info("左上角提示窗已创建")
        except Exception:
            self.log.exception("左上角提示窗创建失败")

        if self.manager:
            self.manager.load_all()
        self._sync_tools()
        self._controls_ready = True
        self._refresh(silent=True)

        win32gui.ShowWindow(self.hwnd, win32con.SW_SHOW)
        win32gui.UpdateWindow(self.hwnd)
        _set_timer(self.hwnd, TIMER_REFRESH, REFRESH_MS)
        self.log.info("原生自绘窗体已创建 (hwnd=%s)", self.hwnd)
        self.notify("窗体已启动，工具按钮已就绪")
        if getattr(self, "_relocate_info", None):
            self._show_relocate_dialog()
        else:
            self._maybe_ask_shortcut()

        try:
            win32gui.PumpMessages()
        finally:
            if self.toast:
                self.toast.close()
            if self.tray:
                self.tray.remove()
            if self.manager:
                self.manager.unload_all()
            gdi.shutdown()

    # -------------------------------------------------------- 托盘 ------
    def _apply_window_icon(self) -> None:
        """为窗口和任务栏设置大/小图标。"""
        try:
            big = win32gui.LoadImage(
                0, ICON_PATH, win32con.IMAGE_ICON, 0, 0,
                win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE,
            )
            small = win32gui.LoadImage(
                0, ICON_PATH, win32con.IMAGE_ICON, 16, 16,
                win32con.LR_LOADFROMFILE,
            )
            if big:
                win32gui.SendMessage(self.hwnd, win32con.WM_SETICON, 1, big)
            if small:
                win32gui.SendMessage(self.hwnd, win32con.WM_SETICON, 0, small)
        except Exception:
            self.log.exception("设置窗口图标失败")

    def _show_window(self) -> None:
        if not self.hwnd:
            return
        win32gui.ShowWindow(self.hwnd, win32con.SW_RESTORE)
        win32gui.BringWindowToTop(self.hwnd)
        _try_foreground(self.hwnd)
        win32gui.InvalidateRect(self.hwnd, None, True)

    def _hide_window(self) -> None:
        if self.hwnd:
            win32gui.ShowWindow(self.hwnd, win32con.SW_HIDE)

    def _on_tray_command(self, cmd: int) -> None:
        if cmd == TRAY_SHOW:
            self._show_window()
        elif cmd == TRAY_HIDE:
            self._hide_window()
        elif cmd == TRAY_RESCUE:
            self._rescue_from_tray()
        elif cmd == TRAY_MOUSE_TOGGLE:
            self._toggle_mouse_from_tray()
        elif cmd == TRAY_EXIT:
            # 托盘菜单“退出程序”是明确指令，不再弹确认框
            if self.hwnd:
                self._force_exit = True
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)

    def _rescue_from_tray(self) -> None:
        """托盘入口救援：窗口越界时主界面点不到，必须能从托盘救回。"""
        tool = self.manager.find("窗口布局守护") if self.manager else None
        if tool is None or not hasattr(tool, "rescue_offscreen"):
            self.notify("救援功能不可用", "窗口救援")
            return
        try:
            tool.rescue_offscreen(include_host=True)
        except Exception:
            self.log.exception("托盘救援执行失败")
            self.notify("救援执行失败，详见日志", "窗口救援")
            return
        # 救援后把本程序窗口显示出来，给出可见反馈
        self._show_window()

    def _mouse_lock_tool(self):
        """取鼠标锁定工具实例；未注册时返回 None。"""
        tool = self.manager.find("鼠标屏幕锁定") if self.manager else None
        return tool if tool and hasattr(tool, "toggle") else None

    def _mouse_locked(self) -> bool:
        """供托盘菜单显示正确的锁定/解锁文案。"""
        tool = self._mouse_lock_tool()
        return bool(getattr(tool, "enabled", False)) if tool else False

    def _toggle_mouse_from_tray(self) -> None:
        """托盘入口切换鼠标锁定：复用工具的 toggle，通知也由工具发出。"""
        tool = self._mouse_lock_tool()
        if tool is None:
            self.notify("鼠标锁定功能不可用", "鼠标锁定")
            return
        try:
            tool.toggle()
        except Exception:
            self.log.exception("托盘切换鼠标锁定失败")
            self.notify("操作失败，详见日志", "鼠标锁定")

    # ------------------------------------------------ 关闭确认对话框 ----
    def _ask_close(self) -> None:
        """收到关闭请求时弹出确认框，而不是直接销毁窗口。"""
        if not self.hwnd or self._dialog:
            return
        self._dialog = {"hotspots": []}
        self._show_window()
        self._invalidate()
        self.log.info("用户请求关闭窗口，等待选择隐藏到托盘或退出")

    def _close_dialog(self) -> None:
        if self._dialog is None:
            return
        self._dialog = None
        self._hover = None
        self._pressed = None
        self._invalidate()

    def _exit_app(self) -> None:
        """真正退出：窗口销毁后由 WM_DESTROY 结束消息循环。"""
        self._close_dialog()
        if self.hwnd:
            self.log.info("用户选择退出程序")
            win32gui.DestroyWindow(self.hwnd)

    # -------------------------------------------- 桌面快捷方式询问/设置 --
    def _shortcut_status(self) -> tuple[bool, bool]:
        """返回 (文件存在, 指向当前程序)。短时间缓存，避免每次重绘都读快捷方式。"""
        now = time.time()
        cached = getattr(self, "_shortcut_cache", None)
        if cached and now - cached[0] < 2:
            return cached[1], cached[2]
        exists = shortcut.shortcut_exists()
        current = shortcut.shortcut_is_current() if exists else False
        self._shortcut_cache = (now, exists, current)
        return exists, current

    def _should_ask_shortcut(self) -> bool:
        """已有指向当前程序的快捷方式时不再询问；设置里点过「重新询问」则再问一次。"""
        sc = self.config.get("shortcut", {}) or {}
        if sc.get("force_ask"):
            return True
        _exists, current = self._shortcut_status()
        if current:
            return False
        if sc.get("asked") and sc.get("remember"):
            return False
        return True

    def prepare_relocate_notice(self, folder: str, original: str,
                                removed: bool) -> None:
        """记录本次启动是从杂乱目录搬过来的，窗口打开后再提示。"""
        self._relocate_info = {
            "folder": folder,
            "original": original,
            "removed": bool(removed),
        }

    def _show_relocate_dialog(self) -> None:
        info = getattr(self, "_relocate_info", None) or {}
        self._dialog = {
            "kind": "relocate",
            "hotspots": [],
            "folder": info.get("folder") or "",
            "original": info.get("original") or "",
            "removed": bool(info.get("removed")),
        }
        self._invalidate()
        self.log.info(
            "启动提示：程序已移动，原文件%s",
            "已删除" if info.get("removed") else "未能删除",
        )

    def _dismiss_relocate(self) -> None:
        self._relocate_info = None
        self._close_dialog()
        self._maybe_ask_shortcut()

    def _maybe_ask_shortcut(self) -> None:
        """启动后按需询问是否创建桌面快捷方式。"""
        if not self._should_ask_shortcut():
            _exists, current = self._shortcut_status()
            if current:
                self.log.info("桌面快捷方式已指向当前程序，跳过询问")
            self._maybe_ask_autostart()
            return
        if self._dialog is not None:
            return
        self._dialog = {"kind": "shortcut", "hotspots": [], "keep": False}
        self._invalidate()
        self.log.info("启动询问：是否创建桌面快捷方式")

    def _resolve_shortcut(self, create: bool) -> None:
        """处理快捷方式询问结果，并按勾选状态持久化选择。"""
        dialog = self._dialog or {}
        keep = bool(dialog.get("keep", False))
        sc = dict(self.config.get("shortcut", {}) or {})
        sc["asked"] = True
        sc["create"] = bool(create)
        sc["remember"] = keep
        sc["force_ask"] = False
        self.config.data["shortcut"] = sc
        self.config.save()
        self._close_dialog()
        self.log.info("快捷方式选择：create=%s remember=%s", create, keep)
        if create:
            self._create_shortcut_action()
        if keep:
            self.notify("已记住选择，以后不再询问")
        self._maybe_ask_autostart()  # 询问链：快捷方式 -> 开机自启

    # ---------------------------------------------- 开机自启询问/设置 --
    def _should_ask_autostart(self) -> bool:
        """已记住选择、或自启项已指向当前程序时不再询问。"""
        cfg = self.config.get("autostart", {}) or {}
        if cfg.get("force_ask"):
            return True
        enabled, current = autostart.status()
        if enabled and current:
            return False
        if cfg.get("asked") and cfg.get("remember"):
            return False
        return True

    def _maybe_ask_autostart(self) -> None:
        """快捷方式询问结束后接着询问是否开机自启。"""
        if self._dialog is not None or not self._should_ask_autostart():
            return
        self._dialog = {"kind": "autostart", "hotspots": [], "keep": False}
        self._invalidate()
        self.log.info("启动询问：是否开启开机自启")

    def _resolve_autostart(self, enable: bool) -> None:
        """处理自启询问结果，并按勾选状态持久化选择。"""
        dialog = self._dialog or {}
        keep = bool(dialog.get("keep", False))
        cfg = dict(self.config.get("autostart", {}) or {})
        cfg["asked"] = True
        cfg["enabled"] = bool(enable)
        cfg["remember"] = keep
        cfg["force_ask"] = False
        self.config.data["autostart"] = cfg
        self.config.save()
        self._close_dialog()
        self._apply_autostart_state(enable)
        if keep:
            self.notify("已记住选择，以后不再询问")
        self.log.info("开机自启选择：enable=%s remember=%s", enable, keep)

    def _apply_autostart(self, enable: bool) -> None:
        """设置页手动开关：与首次询问共用同一套写入与反馈。"""
        cfg = dict(self.config.get("autostart", {}) or {})
        cfg["asked"] = True
        cfg["enabled"] = bool(enable)
        cfg["force_ask"] = False
        self.config.data["autostart"] = cfg
        self.config.save()
        self._apply_autostart_state(enable)
        self._invalidate()

    def _apply_autostart_state(self, enable: bool) -> None:
        """真正写注册表并给出悬浮反馈；所有入口都走这里。"""
        result = autostart.set_enabled(enable)
        level = self.log.info if result.get("ok") else self.log.warning
        level("开机自启：%s", result.get("message"))
        self.show_toast(
            result.get("message", "开机自启操作完成"), "开机自启",
            "success" if result.get("ok") else "info",
        )

    def _reset_autostart_prompt(self) -> None:
        cfg = dict(self.config.get("autostart", {}) or {})
        cfg["asked"] = False
        cfg["remember"] = False
        cfg["force_ask"] = True
        self.config.data["autostart"] = cfg
        self.config.save()
        self.notify("下次启动将重新询问是否开机自启")
        self._invalidate()

    def _create_shortcut_action(self) -> None:
        """根据当前程序路径生成桌面快捷方式并反馈结果。"""
        try:
            result = shortcut.create_shortcut()
        except Exception as exc:
            self.log.exception("生成桌面快捷方式失败")
            result = {"ok": False, "message": "生成失败：%s" % exc}
        level = self.log.info if result.get("ok") else self.log.warning
        level("桌面快捷方式：%s", result.get("message"))
        self._shortcut_cache = None
        self.notify(result.get("message", "快捷方式操作完成"))

    def _open_settings(self) -> None:
        """打开设置页，而不是系统弹出菜单。"""
        current = self._selected_tool
        if current and current != SETTINGS_ID:
            self._settings_hotkey_tool = current
            self._settings_return_tool = current
        self._selected_tool = SETTINGS_ID
        self._settings_scroll = 0
        self._invalidate()

    def _reset_shortcut_prompt(self) -> None:
        sc = dict(self.config.get("shortcut", {}) or {})
        sc["asked"] = False
        sc["remember"] = False
        sc["force_ask"] = True
        self.config.data["shortcut"] = sc
        self.config.save()
        self.notify("下次启动将重新询问是否创建桌面快捷方式")
        self._invalidate()

    def _handle_tray_message(self, lparam) -> None:
        """处理托盘回调（双击显示/隐藏，右键弹出菜单）。"""
        event = int(lparam) & 0xFFFF
        if event == win32con.WM_LBUTTONDBLCLK:
            if win32gui.IsWindowVisible(self.hwnd):
                self._hide_window()
            else:
                self._show_window()
        elif event == win32con.WM_RBUTTONUP:
            if self.tray_menu:
                try:
                    self.tray_menu.show(
                        win32gui.IsWindowVisible(self.hwnd), self._mouse_locked()
                    )
                except Exception:
                    self.log.exception("托盘菜单弹出失败")

    # ------------------------------------------------------ 工具数据 ----
    def _alloc_id(self) -> int:
        cid = self._next_id
        self._next_id += 1
        return cid

    def _sync_tools(self) -> None:
        tools = self.manager.describe_tools() if self.manager else []
        self._tool_blocks = []
        for tool in tools:
            buttons = []
            for action in tool["actions"]:
                cid = self._alloc_id()
                self._action_map[cid] = (tool["name"], action["id"])
                buttons.append({
                    "id": action["id"],
                    "tool": tool["name"],
                    "label": action["label"],
                    "base_label": action["label"],
                    "kind": action["kind"],
                    "tone": action["tone"],
                    "active": action["active"],
                    "hint": action["hint"],
                })
            self._tool_blocks.append({
                "name": tool["name"],
                "version": tool["version"],
                "description": tool.get("description") or "",
                "status": tool["status"],
                "sections": tool.get("sections") or [],
                "pinned": bool(tool.get("pinned")),
                "buttons": buttons,
            })

    # ---------------------------------------------------------- 刷新 ----
    def _invalidate(self) -> None:
        if self.hwnd and self._controls_ready:
            try:
                win32gui.InvalidateRect(self.hwnd, None, False)
            except Exception:
                pass

    def _refresh(self, silent: bool = False) -> None:
        try:
            signature = winapi.screen_signature()
            if self._last_signature is None:
                self._last_signature = signature
            elif signature != self._last_signature:
                self._last_signature = signature
                self._schedule_display_change("配置轮询")
            self._notice_lines = [
                {"time": n["time"], "title": n["title"],
                 "message": n["message"]}
                for n in reversed(list(self.notices))
            ]

            virtual = winapi.bounds_to_rect(winapi.get_virtual_bounds())
            lines = []
            for window in winapi.enumerate_windows(self.hwnd):
                rect = window["rect"]
                left, top, right, bottom = rect
                lines.append({
                    "off": winapi.is_offscreen(rect, virtual),
                    "title": window["title"].replace("\n", " ") or "(无标题)",
                    "process": window["process"] or "",
                    "pos": f"{left}, {top}",
                    "size": f"{right - left}×{bottom - top}",
                })
            self._window_lines = lines

            if self.manager:
                for block in self._tool_blocks:
                    tool_obj = self.manager.find(block["name"])
                    if (tool_obj and self._selected_name() == block["name"]
                            and hasattr(tool_obj, "refresh_if_stale")):
                        tool_obj.refresh_if_stale(5)
                descriptions = {t["name"]: t
                                for t in self.manager.describe_tools()}
                for block in self._tool_blocks:
                    tool = descriptions.get(block["name"])
                    if not tool:
                        continue
                    block["status"] = tool["status"]
                    block["sections"] = tool.get("sections") or []
                    active = {a["id"]: a["active"] for a in tool["actions"]
                              if a["kind"] == "toggle"}
                    hints = {a["id"]: a.get("hint", "")
                             for a in tool["actions"]}
                    for button in block["buttons"]:
                        if button["id"] in active:
                            button["active"] = active[button["id"]]
                        button["label"] = button["base_label"]
                        if button["id"] in hints:
                            button["hint"] = hints[button["id"]]
            self._clamp_scroll()
            self._invalidate()
        except Exception:
            self.log.exception("窗体刷新失败")
        finally:
            if not silent:
                pass

    def _clamp_scroll(self) -> None:
        self._notice_scroll = max(0, min(self._notice_scroll,
                                         max(0, len(self._notice_lines) - 1)))
        self._window_scroll = max(0, min(self._window_scroll,
                                         max(0, len(self._window_lines) - 1)))

    # ---------------------------------------------------------- 绘制 ----
    def _paint(self, hwnd: int) -> None:
        try:
            hdc, ps = win32gui.BeginPaint(hwnd)
        except Exception:
            return
        try:
            rect = win32gui.GetClientRect(hwnd)
            phys_w = max(1, rect[2] - rect[0])
            phys_h = max(1, rect[3] - rect[1])
            # Canvas 内部按 scale 放大；这里传逻辑尺寸给布局
            logical_w = max(1, int(phys_w / self.scale))
            logical_h = max(1, int(phys_h / self.scale))
            memdc = win32gui.CreateCompatibleDC(hdc)
            bitmap = win32gui.CreateCompatibleBitmap(hdc, phys_w, phys_h)
            old = win32gui.SelectObject(memdc, bitmap)
            try:
                with gdi.Canvas(memdc, logical_w, logical_h,
                                self.scale) as canvas:
                    self._draw(canvas, logical_w, logical_h)
                win32gui.BitBlt(hdc, 0, 0, phys_w, phys_h, memdc, 0, 0,
                                win32con.SRCCOPY)
            finally:
                win32gui.SelectObject(memdc, old)
                win32gui.DeleteObject(bitmap)
                win32gui.DeleteDC(memdc)
        except Exception:
            self.log.exception("窗体绘制失败")
        finally:
            try:
                win32gui.EndPaint(hwnd, ps)
            except Exception:
                win32gui.EndPaint(hwnd, (hdc, ps))  # pywin32 版本兼容

    def _draw(self, c, w: int, h: int) -> None:
        c.clear(C.BG)
        self._hotspots = []
        self._list_rects = {}
        self._notice_hits = []
        self._pending_tip = None

        # 顶栏：Oink 风格白底 + 汉堡图标 + 应用标识 + 细描边窗口按钮
        c.rect(0, 0, w, TITLE_H, C.HEADER_A)
        c.rect(0, TITLE_H - 1, w, 1, C.HEADER_LINE)
        for line_y in (21, 28, 35):
            c.line(18, line_y, 28, line_y, C.TEXT_SUB, 1.6)
        c.rounded(44, 12, 34, 34, 11, C.PRIMARY, gradient=C.PRIMARY_HOVER)
        c.text("窗", 44, 12, 34, 34, 15, gdi.argb(255, 255, 255), True,
               gdi.ALIGN_CENTER)
        c.text("窗口工具集合", 90, 9, 250, 24, 16, C.TEXT, True)
        c.text("本地工具  ·  无需联网", 90, 32, 250, 17, 10.5,
               C.TEXT_FAINT)

        ctrl = 30
        gap = 4
        close_rect = (w - 14 - ctrl, 14, ctrl, ctrl)
        max_rect = (close_rect[0] - gap - ctrl, 14, ctrl, ctrl)
        min_rect = (max_rect[0] - gap - ctrl, 14, ctrl, ctrl)
        settings_rect = (min_rect[0] - gap - 56, 14, 56, ctrl)
        self._draw_caption_button(c, "settings", settings_rect, "设置", 12)
        self._draw_caption_button(c, "min", min_rect, "–", 16)
        self._draw_caption_button(c, "max", max_rect, "□", 13)
        self._draw_caption_button(c, "close", close_rect, "×", 17)

        body_y = TITLE_H + 12
        body_h = max(120, h - body_y - FOOTER_H - 8)
        gap = 12
        remain = max(360, w - 2 * PAD - NAV_W - gap)
        list_w = max(250, int(remain * 0.46))
        main_w = remain - gap - list_w
        if main_w < 300:
            list_w = max(220, remain - gap - 300)
            main_w = remain - gap - list_w
        main_x = PAD + NAV_W + gap
        list_x = main_x + main_w + gap

        if self._selected_name() == SETTINGS_ID:
            # 设置页独占整个主体区域，采用 Oink 风格的左侧分类 + 右侧开关列表
            self._draw_settings_page(c, PAD, body_y, w - 2 * PAD, body_h)
        else:
            self._draw_nav(c, PAD, body_y, NAV_W, body_h)
            self._draw_tool_page(c, main_x, body_y, main_w, body_h)
            self._draw_lists(c, list_x, body_y, list_w, body_h)

        self._draw_footer(c, w, h)
        self._draw_notice_tip(c, w, h)

        if self._dialog is not None:
            kind = self._dialog.get("kind")
            if kind == "shortcut":
                self._draw_shortcut_dialog(c, w, h)
            elif kind == "autostart":
                self._draw_autostart_dialog(c, w, h)
            elif kind == "relocate":
                self._draw_relocate_dialog(c, w, h)
            elif kind == "hotkey":
                self._draw_hotkey_dialog(c, w, h)
            else:
                self._draw_close_dialog(c, w, h)

    def _draw_caption_button(self, c, kind: str, rect, glyph: str,
                             size: float) -> None:
        x, y, bw, bh = rect
        hovered = self._hover == kind
        pressed = self._pressed == kind
        if kind == "settings":
            c.rounded(x, y, bw, bh, 15,
                      C.PRIMARY_SOFT if hovered or pressed else C.CARD_SOFT,
                      C.PRIMARY if hovered or pressed else C.BORDER)
            color = C.PRIMARY if hovered or pressed else C.TEXT_SUB
        elif kind == "close" and (hovered or pressed):
            c.rounded(x, y, bw, bh, 8, C.DANGER)
            color = gdi.argb(255, 255, 255)
        elif hovered or pressed:
            c.rounded(x, y, bw, bh, 8, C.DIVIDER)
            color = C.TEXT
        else:
            color = C.TEXT_SUB
        c.text(glyph, x, y - 1, bw, bh + 2, size, color,
               kind == "settings", gdi.ALIGN_CENTER)
        self._hotspots.append({"kind": kind, "rect": rect})

    def _footer_text(self) -> str:
        chips = []
        selected = self._selected_name()
        if selected == SETTINGS_ID:
            return "设置  ·  自定义快捷键会停用默认组合，可在此恢复默认"
        for block in self._tool_blocks:
            if selected and block["name"] != selected:
                continue
            for button in block["buttons"]:
                hint = button.get("hint") or ""
                raw = button.get("base_label") or button.get("label")
                label = {
                    "保存当前布局": "保存",
                    "自动还原布局": "还原",
                    "救援越界窗口": "救援",
                    "刷新信息": "刷新",
                }.get(raw, raw)
                if "快捷键" in hint:
                    key = hint.split("快捷键", 1)[1].split("；")[0].strip(" ：")
                    if key and "未注册" not in key:
                        chips.append(f"{label} {key}")
                elif hint.startswith("切换 "):
                    key = hint.split("；")[0].replace("切换 ", "").strip()
                    if key and "未注册" not in key:
                        chips.append(f"锁定 {key}")
                    if "强制解锁" in hint:
                        unlock = hint.split("强制解锁", 1)[1].strip()
                        if unlock:
                            chips.append(f"解锁 {unlock}")
        if not chips:
            return "在左侧选择功能  ·  数据保存在 data 目录"
        return "   ·   ".join(chips)

    def _draw_footer(self, c, w: int, h: int) -> None:
        y = h - FOOTER_H
        c.rect(0, y, w, 1, C.DIVIDER)
        c.rect(0, y + 1, w, FOOTER_H - 1, C.CARD)
        c.text(self._footer_text(), PAD, y + 1, w - 2 * PAD, FOOTER_H - 1,
               11.5, C.TEXT_SUB)

    def _draw_dialog_button(self, c, kind, label, tone, rect) -> None:
        """通用对话框按钮：primary / danger / default。"""
        x, y, bw, bh = rect
        hovered = self._hover == kind
        pressed = self._pressed == kind
        if tone == "primary":
            fill = C.PRIMARY_DARK if pressed else (
                C.PRIMARY_HOVER if hovered else C.PRIMARY)
            border = None
            color = gdi.argb(255, 255, 255)
            bold = True
        elif tone == "danger":
            fill = C.DANGER_SOFT if hovered else C.CARD
            border = C.DANGER
            color = C.DANGER
            bold = True
        else:
            fill = C.PRIMARY_SOFT if hovered else C.CARD_SOFT
            border = C.BORDER
            color = C.TEXT_SUB
            bold = False
        offset = 1 if pressed else 0
        c.rounded(x, y + 2, bw, bh - 2, 10, C.SHADOW)
        c.rounded(x, y + offset, bw, bh - 2, 10, fill, border)
        c.text(label, x, y + offset, bw, bh - 2, 13, color, bold,
               gdi.ALIGN_CENTER)
        self._hotspots.append({"kind": kind, "rect": rect})

    def _draw_relocate_dialog(self, c, w: int, h: int) -> None:
        """搬到独立文件夹后，打开时说明新位置，并告知原文件是否已删除。"""
        c.rect(0, 0, w, h, gdi.argb(15, 23, 42, 120))
        dw, dh = 520, 280
        dx = max(12, (w - dw) // 2)
        dy = max(12, (h - dh) // 2)
        c.rounded(dx + 4, dy + 8, dw, dh, 16, C.SHADOW)
        c.rounded(dx, dy, dw, dh, 16, C.CARD, C.BORDER)

        c.rounded(dx + 24, dy + 24, 44, 44, 12, C.PRIMARY_SOFT)
        c.text("移", dx + 24, dy + 24, 44, 44, 20, C.PRIMARY, True,
               gdi.ALIGN_CENTER)
        c.text("程序已移动", dx + 80, dy + 22, dw - 104, 30, 18,
               C.TEXT, True)
        c.text("所在目录还有其他文件，已移到下面的文件夹。",
               dx + 26, dy + 78, dw - 52, 24, 13, C.TEXT_SUB)
        folder = (self._dialog or {}).get("folder") or ""
        c.text(folder, dx + 26, dy + 108, dw - 52, 24, 12.5, C.TEXT, True,
               ellipsis=True)
        if (self._dialog or {}).get("removed"):
            result = "原来的程序已删除。"
            color = C.TEXT_SUB
        else:
            original = (self._dialog or {}).get("original") or "未知位置"
            result = "原来的程序还在，请手动删除：%s" % original
            color = C.DANGER
        c.text(result, dx + 26, dy + 140, dw - 52, 40, 12.5, color,
               ellipsis=True)

        c.rect(dx + 24, dy + 190, dw - 48, 1, C.DIVIDER)
        self._draw_dialog_button(
            c, "dlg_relocate_ok", "知道了", "primary",
            (dx + 24, dy + 210, dw - 48, 46))

    def _draw_shortcut_dialog(self, c, w: int, h: int) -> None:
        """启动询问框：是否创建桌面快捷方式 + 保持选择勾选。"""
        c.rect(0, 0, w, h, gdi.argb(15, 23, 42, 120))
        dw, dh = 500, 292
        dx = max(12, (w - dw) // 2)
        dy = max(12, (h - dh) // 2)
        c.rounded(dx + 4, dy + 8, dw, dh, 16, C.SHADOW)
        c.rounded(dx, dy, dw, dh, 16, C.CARD, C.BORDER)

        c.rounded(dx + 24, dy + 24, 44, 44, 12, C.PRIMARY_SOFT)
        c.text("捷", dx + 24, dy + 24, 44, 44, 20, C.PRIMARY, True,
               gdi.ALIGN_CENTER)
        c.text("桌面快捷方式", dx + 80, dy + 22, dw - 104, 30, 18,
               C.TEXT, True)
        c.text("是否在桌面创建本程序的快捷方式？",
               dx + 26, dy + 68, dw - 52, 24, 13, C.TEXT_SUB)
        spec = shortcut.launch_spec()
        c.text("目标：%s" % os.path.basename(spec["target"]),
               dx + 26, dy + 94, dw - 52, 22, 11.5, C.TEXT_FAINT)

        # 勾选框：保持当前选择，以后不再询问
        keep = bool(self._dialog.get("keep", False))
        kb_rect = (dx + 26, dy + 132, 20, 20)
        kh = self._hover == "dlg_sc_keep"
        c.rounded(kb_rect[0], kb_rect[1], 20, 20, 6,
                  C.PRIMARY if keep else (C.PRIMARY_SOFT if kh else C.CARD),
                  C.PRIMARY if keep else C.BORDER)
        if keep:
            c.text("✓", kb_rect[0], kb_rect[1] - 2, 20, 22, 15,
                   gdi.argb(255, 255, 255), True, gdi.ALIGN_CENTER)
        c.text("保持当前选择，以后不再询问",
               dx + 54, dy + 130, dw - 80, 24, 12.5, C.TEXT_SUB)
        self._hotspots.append({"kind": "dlg_sc_keep", "rect": kb_rect})
        # 整行可点
        self._hotspots.append({
            "kind": "dlg_sc_keep",
            "rect": (dx + 26, dy + 128, dw - 52, 28),
        })

        c.rect(dx + 24, dy + 168, dw - 48, 1, C.DIVIDER)
        gap = 12
        bw = (dw - 48 - gap) // 2
        by = dy + 192
        bh = 46
        self._draw_dialog_button(
            c, "dlg_sc_skip", "暂不生成", "default",
            (dx + 24, by, bw, bh))
        self._draw_dialog_button(
            c, "dlg_sc_create", "生成快捷方式", "primary",
            (dx + 24 + bw + gap, by, bw, bh))

    def _draw_autostart_dialog(self, c, w: int, h: int) -> None:
        """启动询问框：是否开机自启 + 保持选择勾选。"""
        c.rect(0, 0, w, h, gdi.argb(15, 23, 42, 120))
        dw, dh = 500, 292
        dx = max(12, (w - dw) // 2)
        dy = max(12, (h - dh) // 2)
        c.rounded(dx + 4, dy + 8, dw, dh, 16, C.SHADOW)
        c.rounded(dx, dy, dw, dh, 16, C.CARD, C.BORDER)

        c.rounded(dx + 24, dy + 24, 44, 44, 12, C.PRIMARY_SOFT)
        c.text("启", dx + 24, dy + 24, 44, 44, 20, C.PRIMARY, True,
               gdi.ALIGN_CENTER)
        c.text("开机自启", dx + 80, dy + 22, dw - 104, 30, 18,
               C.TEXT, True)
        c.text("是否让本程序在登录 Windows 后自动运行？",
               dx + 26, dy + 68, dw - 52, 24, 13, C.TEXT_SUB)
        c.text("启动命令：%s" % os.path.basename(shortcut.launch_spec()["target"]),
               dx + 26, dy + 94, dw - 52, 22, 11.5, C.TEXT_FAINT)

        keep = bool(self._dialog.get("keep", False))
        kb_rect = (dx + 26, dy + 132, 20, 20)
        kh = self._hover == "dlg_as_keep"
        c.rounded(kb_rect[0], kb_rect[1], 20, 20, 6,
                  C.PRIMARY if keep else (C.PRIMARY_SOFT if kh else C.CARD),
                  C.PRIMARY if keep else C.BORDER)
        if keep:
            c.text("✓", kb_rect[0], kb_rect[1] - 2, 20, 22, 15,
                   gdi.argb(255, 255, 255), True, gdi.ALIGN_CENTER)
        c.text("保持当前选择，以后不再询问",
               dx + 54, dy + 130, dw - 80, 24, 12.5, C.TEXT_SUB)
        self._hotspots.append({"kind": "dlg_as_keep", "rect": kb_rect})
        self._hotspots.append({
            "kind": "dlg_as_keep",
            "rect": (dx + 26, dy + 128, dw - 52, 28),
        })

        c.rect(dx + 24, dy + 168, dw - 48, 1, C.DIVIDER)
        gap = 12
        bw = (dw - 48 - gap) // 2
        by = dy + 192
        bh = 46
        self._draw_dialog_button(
            c, "dlg_as_skip", "暂不启用", "default",
            (dx + 24, by, bw, bh))
        self._draw_dialog_button(
            c, "dlg_as_enable", "启用开机自启", "primary",
            (dx + 24 + bw + gap, by, bw, bh))

    def _draw_close_dialog(self, c, w: int, h: int) -> None:
        """自绘模态确认框：隐藏到托盘 / 退出程序 / 取消。"""
        # 半透明遮罩，背景仍可辨认但不可交互
        c.rect(0, 0, w, h, gdi.argb(15, 23, 42, 120))

        dw, dh = 470, 252
        dx = max(12, (w - dw) // 2)
        dy = max(12, (h - dh) // 2)
        c.rounded(dx + 4, dy + 8, dw, dh, 16, C.SHADOW)
        c.rounded(dx, dy, dw, dh, 16, C.CARD, C.BORDER)

        c.rounded(dx + 24, dy + 24, 44, 44, 12, C.PRIMARY_SOFT)
        c.text("?", dx + 24, dy + 24, 44, 44, 22, C.PRIMARY, True,
               gdi.ALIGN_CENTER)
        c.text("关闭窗口", dx + 80, dy + 22, dw - 104, 30, 18, C.TEXT, True)
        c.text("隐藏到托盘后程序继续运行，可从任务栏图标唤出",
               dx + 26, dy + 66, dw - 52, 24, 12.5, C.TEXT_SUB)
        c.text("退出程序会关闭全部工具，并释放鼠标锁定",
               dx + 26, dy + 92, dw - 52, 24, 12.5, C.TEXT_SUB)
        c.rect(dx + 24, dy + 126, dw - 48, 1, C.DIVIDER)

        gap = 12
        bw = (dw - 48 - gap * 2) // 3
        by = dy + 150
        bh = 46
        options = (
            ("dlg_cancel", "取消", "default"),
            ("dlg_hide", "隐藏到托盘", "primary"),
            ("dlg_exit", "退出程序", "danger"),
        )
        for index, (kind, label, tone) in enumerate(options):
            bx = dx + 24 + index * (bw + gap)
            self._draw_dialog_button(c, kind, label, tone,
                                     (bx, by, bw, bh))

    def _selected_name(self) -> str:
        if self._selected_tool == SETTINGS_ID:
            return SETTINGS_ID
        names = [block["name"] for block in self._tool_blocks]
        if self._selected_tool in names:
            return self._selected_tool
        return names[0] if names else ""

    def _selected_block(self) -> dict | None:
        name = self._selected_name()
        for block in self._tool_blocks:
            if block["name"] == name:
                return block
        return None

    @staticmethod
    def _clip_rect(rect, bounds):
        x, y, w, h = rect
        bx, by, bw, bh = bounds
        x2, y2 = min(x + w, bx + bw), min(y + h, by + bh)
        x, y = max(x, bx), max(y, by)
        if x2 <= x or y2 <= y:
            return None
        return x, y, x2 - x, y2 - y

    def _draw_nav(self, c, x: int, y: int, w: int, h: int) -> None:
        c.rounded(x, y + 3, w, h, CARD_RADIUS, C.SHADOW)
        c.rounded(x, y, w, h, CARD_RADIUS, C.CARD, C.BORDER)
        scroll_blocks = [b for b in self._tool_blocks if not b.get("pinned")]
        pinned_blocks = [b for b in self._tool_blocks if b.get("pinned")]
        c.text("功能", x + 16, y + 8, w - 70, 28, 14, C.TEXT, True)
        count = str(len(scroll_blocks))
        badge_w = max(28, int(c.measure(count, 11) + 14))
        c.rounded(x + w - badge_w - 14, y + 12, badge_w, 22, 11,
                  C.PRIMARY_SOFT)
        c.text(count, x + w - badge_w - 14, y + 12, badge_w, 22, 11,
               C.PRIMARY, False, gdi.ALIGN_CENTER)
        c.rect(x + 16, y + 40, w - 32, 1, C.DIVIDER)

        footer_rows = len(pinned_blocks) + 1
        list_y = y + 44
        list_h = max(40, h - 56 - footer_rows * NAV_ROW - 8)
        bounds = (x, list_y, w, list_h)
        self._list_rects["nav"] = bounds
        total_h = max(NAV_ROW, len(scroll_blocks) * NAV_ROW)
        max_scroll = max(0, total_h - list_h)
        self._nav_scroll = min(self._nav_scroll, max_scroll)

        palette = [C.PRIMARY, C.WARNING, C.SUCCESS]
        c.clip(x + 8, list_y, w - 16, list_h)
        try:
            for index, block in enumerate(scroll_blocks):
                ry = list_y + index * NAV_ROW - self._nav_scroll
                if ry + NAV_ROW < list_y or ry > list_y + list_h:
                    continue
                accent = palette[index % len(palette)]
                self._draw_nav_tool(c, x, ry, w, block, accent, bounds)
        finally:
            c.reset_clip()
        self._draw_scrollbar(c, x + w - 8, list_y, list_h,
                             self._nav_scroll, max_scroll, list_h, total_h)

        footer_y = y + h - footer_rows * NAV_ROW - 4
        c.rect(x + 16, footer_y - 4, w - 32, 1, C.DIVIDER)
        for index, block in enumerate(pinned_blocks):
            accent = palette[(len(scroll_blocks) + index) % len(palette)]
            self._draw_nav_tool(
                c, x, footer_y + index * NAV_ROW, w, block, accent, None)

        settings_y = footer_y + len(pinned_blocks) * NAV_ROW
        row = (x + 10, settings_y, w - 20, NAV_ROW - 8)
        active = self._selected_name() == SETTINGS_ID
        hovered = self._hover == "nav_settings"
        if active:
            c.rounded(row[0], row[1], row[2], row[3], 10, C.PRIMARY_SOFT)
        elif hovered:
            c.rounded(row[0], row[1], row[2], row[3], 10, C.CARD_SOFT)
        self._draw_icon_tile(
            c, "settings", row[0] + 12, row[1] + 8, 28, C.TEXT)
        c.text("设置", row[0] + 48, row[1], row[2] - 58, row[3], 13,
               C.TEXT, active)
        self._hotspots.append({"kind": "nav_settings", "rect": row})

    def _feature_icon(self, name: str) -> str:
        return {
            "窗口布局守护": "layout",
            "鼠标屏幕锁定": "mouse",
            "本机信息": "device",
        }.get(name, "generic")

    def _draw_icon_tile(self, c, kind: str, x, y, size, fill) -> None:
        c.rounded(x, y, size, size, max(6, size * 0.28), fill)
        if kind == "generic":
            c.text("工", x, y, size, size, size * 0.42,
                   gdi.argb(255, 255, 255), True, gdi.ALIGN_CENTER)
            return
        self._draw_icon_glyph(c, kind, x, y, size)

    def _draw_icon_glyph(self, c, kind: str, x, y, size) -> None:
        white = gdi.argb(255, 255, 255)
        pad = size * 0.22
        inner = size - pad * 2
        origin_x = x + pad
        origin_y = y + pad
        stroke = max(1.25, size * 0.065)

        def at(u, v):
            return origin_x + inner * u / 16, origin_y + inner * v / 16

        def box(u, v, w, h):
            px, py = at(u, v)
            return px, py, inner * w / 16, inner * h / 16

        if kind == "layout":
            for u, v in ((1, 1), (9, 1), (1, 9), (9, 9)):
                px, py, w, h = box(u, v, 6, 6)
                c.rounded(px, py, w, h, max(1.2, size * 0.06), white)
            return
        if kind == "mouse":
            raw = (
                (1.4, 1.0), (1.4, 13.6), (5.3, 9.8),
                (8.2, 14.8), (10.5, 13.6), (7.6, 8.7), (12.4, 8.7),
            )
            c.polygon([at(u, v) for u, v in raw], white)
            return
        if kind == "device":
            px, py, w, h = box(1.2, 1.0, 13.6, 9.2)
            c.stroke_rounded(px, py, w, h, max(1.4, size * 0.08), white, stroke)
            x1, y1 = at(8, 10.4)
            x2, y2 = at(8, 13.0)
            c.line(x1, y1, x2, y2, white, stroke)
            x1, y1 = at(4.6, 13.2)
            x2, y2 = at(11.4, 13.2)
            c.line(x1, y1, x2, y2, white, stroke)
            return
        if kind == "settings":
            rows = ((3.1, 4.6), (8.0, 11.0), (12.9, 7.2))
            knob = max(2.2, inner * 0.16)
            for row_y, knob_x in rows:
                x1, y1 = at(1.4, row_y)
                x2, _ = at(14.6, row_y)
                c.line(x1, y1, x2, y1, white, stroke)
                cx, cy = at(knob_x, row_y)
                c.ellipse(cx - knob / 2, cy - knob / 2, knob, knob, white)
            return

    def _draw_nav_tool(self, c, x, y, w, block, accent, bounds) -> None:
        row = (x + 10, y + 4, w - 20, NAV_ROW - 8)
        active = block["name"] == self._selected_name()
        hovered = self._hover == block["name"]
        if active:
            c.rounded(row[0], row[1], row[2], row[3], 10, C.PRIMARY_SOFT)
            c.rounded(row[0], row[1] + 8, 3, row[3] - 16, 1, accent)
        elif hovered:
            c.rounded(row[0], row[1], row[2], row[3], 10, C.CARD_SOFT)
        self._draw_icon_tile(
            c, self._feature_icon(block["name"]),
            row[0] + 12, row[1] + 8, 28, accent)
        c.text(block["name"], row[0] + 48, row[1] + 1, row[2] - 90,
               22, 13, C.TEXT, active)
        status = " ".join((block.get("status") or "").split())[:18]
        c.text(status, row[0] + 48, row[1] + 23, row[2] - 90, 17,
               10, C.TEXT_FAINT)
        c.ellipse(row[0] + row[2] - 16, row[1] + 19, 7, 7,
                  C.SUCCESS if active else C.SCROLL)
        hit = row if bounds is None else self._clip_rect(row, bounds)
        if hit:
            self._hotspots.append({
                "kind": "tool", "rect": hit, "tool": block["name"],
            })

    def _draw_tool_page(self, c, x: int, y: int, w: int, h: int) -> None:
        c.rounded(x, y + 3, w, h, CARD_RADIUS, C.SHADOW)
        c.rounded(x, y, w, h, CARD_RADIUS, C.CARD, C.BORDER)
        block = self._selected_block()
        if block is None:
            c.text("还没有注册功能", x + 20, y + 24, w - 40, 28, 14, C.TEXT_SUB)
            return

        accent = C.PRIMARY
        for index, item in enumerate(self._tool_blocks):
            if item["name"] == block["name"]:
                accent = [C.PRIMARY, C.WARNING, C.SUCCESS][index % 3]
                break
        self._draw_icon_tile(
            c, self._feature_icon(block["name"]), x + 18, y + 16, 36, accent)
        badge = "本地  v" + block["version"]
        badge_w = max(84, int(c.measure(badge, 11) + 34))
        badge_x = x + w - badge_w - 16
        c.text(block["name"], x + 64, y + 14, w - 84 - badge_w, 24, 16,
               C.TEXT, True)
        c.rounded(badge_x, y + 16, badge_w, 22, 11, C.CARD_SOFT, C.BORDER)
        c.ellipse(badge_x + 10, y + 22, 6, 6, C.SUCCESS)
        c.text(badge, badge_x + 20, y + 16, badge_w - 24, 22, 11,
               C.TEXT_SUB)
        c.text(block.get("description") or "", x + 18, y + 58, w - 36, 22,
               12, C.TEXT_SUB)
        c.text(block["status"], x + 18, y + 80, w - 36, 20, 12, C.TEXT_FAINT)
        c.rect(x + 18, y + 108, w - 36, 1, C.DIVIDER)

        list_y = y + 120
        list_h = h - 132
        bounds = (x, list_y, w, list_h)
        self._list_rects["actions"] = bounds
        buttons = block["buttons"]
        sections = block.get("sections") or []
        hotkeys = self.hotkey_rows(block["name"])
        hk_h = (32 + len(hotkeys) * 44) if hotkeys else 0
        guide_h = 118 if block["name"] == "窗口布局守护" else 0
        row_step = BTN_H + BTN_GAP_Y
        paired = bool(sections) and 1 < len(buttons) <= 3
        button_rows = 1 if paired else len(buttons)
        content_w = w - 32
        section_items, section_h = self._layout_sections(c, content_w, sections)
        section_gap = 8 if section_h else 0
        gap_after = 12 if hk_h or guide_h or section_h else 0
        total_h = (button_rows * row_step + section_gap + section_h
                   + gap_after + hk_h + guide_h)
        max_scroll = max(0, total_h - list_h)
        self._action_scroll = min(max(0, self._action_scroll), max_scroll)

        c.clip(x + 8, list_y, w - 16, list_h)
        try:
            button_top = list_y - self._action_scroll
            if paired:
                gap = 10
                bw = (content_w - gap * (len(buttons) - 1)) // len(buttons)
                for index, button in enumerate(buttons):
                    rect = (x + 16 + index * (bw + gap), button_top, bw, BTN_H)
                    if button_top + BTN_H < list_y or button_top > list_y + list_h:
                        continue
                    self._draw_button(c, button, rect)
                    hit = self._clip_rect(rect, bounds)
                    if hit:
                        self._hotspots.append({
                            "kind": "action", "rect": hit,
                            "tool": block["name"], "action": button["id"],
                        })
            else:
                for index, button in enumerate(buttons):
                    by = button_top + index * row_step
                    if by + BTN_H < list_y or by > list_y + list_h:
                        continue
                    rect = (x + 16, by, content_w, BTN_H)
                    self._draw_button(c, button, rect)
                    hit = self._clip_rect(rect, bounds)
                    if hit:
                        self._hotspots.append({
                            "kind": "action", "rect": hit,
                            "tool": block["name"], "action": button["id"],
                        })
            cursor = button_top + button_rows * row_step + section_gap
            if section_items:
                self._draw_section_items(
                    c, x + 16, cursor, content_w, section_items, list_y, list_h)
                cursor += section_h + gap_after
            else:
                cursor += gap_after
            if hotkeys:
                self._draw_hotkey_block(
                    c, x + 16, cursor, content_w, hotkeys, bounds)
                cursor += hk_h
            if guide_h and cursor + guide_h >= list_y and cursor <= list_y + list_h:
                self._draw_guide(c, x + 16, cursor, content_w, guide_h)
        finally:
            c.reset_clip()
        self._draw_scrollbar(c, x + w - 8, list_y, list_h,
                             self._action_scroll, max_scroll, list_h,
                             max(1, total_h))

    def _layout_sections(self, c, w: int, sections: list) -> tuple:
        items = []
        y = 0
        label_w = 78
        value_w = max(40, w - label_w - 8)
        for section in sections:
            items.append({
                "type": "title", "y": y, "h": 28,
                "text": section.get("title") or "",
            })
            y += 28
            for row in section.get("rows") or []:
                if row.get("kind") == "sub":
                    items.append({
                        "type": "sub", "y": y, "h": 26,
                        "text": row.get("value") or "",
                    })
                    y += 26
                    continue
                lines = self._wrap_lines(
                    c, row.get("value") or "—", 12, value_w) or ["—"]
                height = max(26, 4 + 18 * len(lines))
                items.append({
                    "type": "field", "y": y, "h": height,
                    "label": row.get("label") or "",
                    "lines": lines,
                })
                y += height
            y += 8
        return items, y

    def _draw_section_items(self, c, x, y, w, items, view_y, view_h) -> None:
        label_w = 78
        value_w = max(40, w - label_w - 8)
        for item in items:
            top = y + item["y"]
            if top + item["h"] < view_y or top > view_y + view_h:
                continue
            kind = item["type"]
            if kind == "title":
                c.rounded(x, top + 6, 3, 14, 1, C.PRIMARY)
                c.text(item["text"], x + 10, top, w - 10, 24, 13, C.TEXT, True)
            elif kind == "sub":
                c.text(item["text"], x, top, w, 24, 12.5, C.TEXT, True)
            else:
                c.text(item["label"], x, top, label_w, 22, 12, C.TEXT_FAINT)
                line_y = top
                for line in item["lines"]:
                    c.text(line, x + label_w, line_y, value_w, 18, 12, C.TEXT,
                           False, gdi.ALIGN_LEFT, gdi.VALIGN_MIDDLE, False)
                    line_y += 18

    def _draw_setting_group(self, c, x, y, w, h, glyph, accent, soft,
                            title, state, state_color,
                            primary_kind, primary_label, ask_kind,
                            bounds) -> None:
        """设置项的统一分组条：图标 + 标题 + 彩色状态 + 主次按钮。"""
        c.rounded(x, y, w, h, 10, C.CARD_SOFT, C.BORDER)
        c.rounded(x + 12, y + 12, 36, 36, 10, soft)
        c.text(glyph, x + 12, y + 11, 36, 38, 17, accent, True,
               gdi.ALIGN_CENTER)
        c.text(title, x + 58, y + 10, w - 70, 22, 13.5, C.TEXT, True)
        c.text(state, x + 58, y + 36, w - 70, 20, 11.5, state_color)
        gap = 10
        bw = (w - 24 - gap) // 2
        by = y + h - 38
        self._draw_mini_button(
            c, primary_kind, "", primary_label,
            (x + 12, by, bw, 32), bounds, "primary")
        self._draw_mini_button(
            c, ask_kind, "", "重新询问",
            (x + 12 + bw + gap, by, bw, 32), bounds)

    def _draw_settings_page(self, c, x: int, y: int, w: int, h: int) -> None:
        """设置页：左侧分类导航 + 右侧开关列表（Oink 设置页风格）。"""
        c.rounded(x, y + 3, w, h, CARD_RADIUS, C.SHADOW)
        c.rounded(x, y, w, h, CARD_RADIUS, C.CARD, C.BORDER)

        back = (x + 16, y + 12, 34, 34)
        back_hover = self._hover == "settings_back"
        c.rounded(back[0], back[1], back[2], back[3], 9,
                  C.CARD_SOFT if back_hover else C.CARD, C.BORDER)
        c.text("←", back[0], back[1] - 1, back[2], back[3], 16,
               C.TEXT_SUB, True, gdi.ALIGN_CENTER)
        self._hotspots.append({"kind": "settings_back", "rect": back})
        c.text("设置", x + 62, y + 11, 220, 26, 17, C.TEXT, True)
        c.text("本机偏好设置", x + 62, y + 35, 220, 18, 10.5, C.TEXT_FAINT)
        c.rect(x + 16, y + 60, w - 32, 1, C.DIVIDER)

        nav_w = 210 if w >= 800 else 170
        gap = 18
        nav_x = x + 18
        content_x = nav_x + nav_w + gap
        content_w = x + w - 18 - content_x
        nav_y = y + 72
        nav_h = max(80, h - 88)

        # 搜索框（视觉占位，分类数量很少，不需要真实过滤）
        c.rounded(nav_x, nav_y, nav_w, 36, 18, C.CARD_SOFT, C.BORDER)
        c.text("⌕", nav_x + 12, nav_y + 5, 22, 24, 15, C.TEXT_FAINT)
        c.text("搜索设置", nav_x + 36, nav_y + 6, nav_w - 48, 22,
               11.5, C.TEXT_FAINT)

        categories = (("通用", "通"), ("快捷键", "键"), ("关于", "关"))
        cat_y = nav_y + 50
        row_h = 44
        for index, (label, glyph) in enumerate(categories):
            row = (nav_x, cat_y + index * row_h, nav_w, row_h - 6)
            active = self._settings_category == label
            hovered = self._hover == ("set_cat", label)
            if active:
                c.rounded(row[0], row[1], row[2], row[3], 9, C.PRIMARY_SOFT)
                c.rounded(row[0], row[1] + 8, 3, row[3] - 16, 1, C.PRIMARY)
            elif hovered:
                c.rounded(row[0], row[1], row[2], row[3], 9, C.CARD_SOFT)
            c.rounded(nav_x + 12, row[1] + 6, 26, 26, 7,
                      C.PRIMARY if active else C.TEXT_SUB)
            c.text(glyph, nav_x + 12, row[1] + 6, 26, 26, 12,
                   gdi.argb(255, 255, 255), True, gdi.ALIGN_CENTER)
            c.text(label, nav_x + 48, row[1], row[2] - 62, row[3],
                   12.5, C.TEXT, active)
            self._hotspots.append({
                "kind": "set_cat", "rect": row, "cat": label,
            })
        c.text(f"v{__version__}  ·  本地运行", nav_x + 4, y + h - 36,
               nav_w - 8, 20, 10.5, C.TEXT_FAINT)

        if content_w < 220:
            return
        c.text(self._settings_category, content_x, nav_y + 1, content_w,
               24, 14, C.TEXT, True)
        body_y = nav_y + 34
        body_h = max(80, y + h - 16 - body_y)
        if self._settings_category == "快捷键":
            self._draw_settings_hotkeys(c, content_x, body_y, content_w, body_h)
        elif self._settings_category == "关于":
            self._draw_about_settings(c, content_x, body_y, content_w, body_h)
        else:
            self._draw_general_settings(c, content_x, body_y, content_w, body_h)

    def _draw_general_settings(self, c, x, y, w, h) -> None:
        """通用设置：标题 + 说明 + 右侧开关/按钮，行间细分隔。"""
        c.rounded(x, y, w, h, 12, C.CARD, C.BORDER)
        c.clip(x, y, w, h)
        row_h = 96
        bounds = (x, y, w, h)

        # 桌面快捷方式
        exists, current = self._shortcut_status()
        c.text("桌面快捷方式", x + 22, y + 16, w - 210, 24, 13.5,
               C.TEXT, True)
        state = ("已指向当前程序" if current else
                 "存在但不是当前程序，建议更新" if exists else "桌面还没有快捷方式")
        c.text(state, x + 22, y + 44, w - 210, 20, 11,
               C.SUCCESS if current else C.WARNING)
        c.text("登录后可一键打开工具，快捷方式始终指向程序当前路径",
               x + 22, y + 66, w - 210, 18, 10.5, C.TEXT_FAINT)
        self._draw_mini_button(
            c, "set_make", "", "更新快捷方式" if exists else "生成快捷方式",
            (x + w - 168, y + 22, 146, 34), bounds, "primary")
        self._draw_mini_button(
            c, "set_ask", "", "重新询问",
            (x + w - 168, y + 60, 146, 28), bounds)
        c.rect(x + 22, y + row_h - 1, w - 44, 1, C.DIVIDER)

        # 开机自启
        ry = y + row_h
        enabled, current_as = autostart.status()
        c.text("开机自动启动", x + 22, ry + 16, w - 210, 24, 13.5,
               C.TEXT, True)
        state_as = ("已开启，指向当前程序" if enabled and current_as else
                    "已开启但指向旧路径，点开关更新" if enabled else "未开启")
        c.text(state_as, x + 22, ry + 44, w - 210, 20, 11,
               C.SUCCESS if enabled and current_as else C.WARNING)
        c.text("登录系统后自动打开窗口工具集合，在后台保持运行",
               x + 22, ry + 66, w - 210, 18, 10.5, C.TEXT_FAINT)
        self._draw_switch(c, (x + w - 150, ry + 24, 70, 32),
                          enabled, "set_as_toggle")
        self._draw_mini_button(
            c, "set_as_ask", "", "重新询问",
            (x + w - 168, ry + 60, 146, 28), bounds)
        c.rect(x + 22, ry + row_h - 1, w - 44, 1, C.DIVIDER)

        # 操作反馈
        ry = y + row_h * 2
        c.text("操作反馈", x + 22, ry + 16, w - 210, 24, 13.5,
               C.TEXT, True)
        c.text("所有开关操作都会在屏幕左上角给出结果提示",
               x + 22, ry + 44, w - 210, 20, 11, C.TEXT_SUB)
        c.text("数据保存在程序目录的 data 文件夹，不上传网络",
               x + 22, ry + 66, w - 210, 18, 10.5, C.TEXT_FAINT)
        c.rect(x + 22, ry + row_h - 1, w - 44, 1, C.DIVIDER)

        # 通知级别
        ry = y + row_h * 3
        c.text("通知级别", x + 22, ry + 16, w - 210, 24, 13.5,
               C.TEXT, True)
        c.text("选择本机显示的通知详细程度",
               x + 22, ry + 44, w - 210, 20, 11, C.TEXT_SUB)
        c.text("操作结果与异常会集中提示，避免打扰",
               x + 22, ry + 66, w - 210, 18, 10.5, C.TEXT_FAINT)
        c.rounded(x + w - 150, ry + 24, 110, 32, 16, C.CARD_SOFT, C.BORDER)
        c.text("默认", x + w - 150, ry + 24, 110, 32, 11.5,
               C.TEXT_SUB, False, gdi.ALIGN_CENTER)
        c.rect(x + 22, ry + row_h - 1, w - 44, 1, C.DIVIDER)

        # 快捷键入口
        ry = y + row_h * 4
        c.text("快捷键设置", x + 22, ry + 16, w - 210, 24, 13.5,
               C.TEXT, True)
        c.text("自定义工具的全局热键",
               x + 22, ry + 44, w - 210, 20, 11, C.TEXT_SUB)
        c.text("修改后默认组合不再响应，可随时恢复",
               x + 22, ry + 66, w - 210, 18, 10.5, C.TEXT_FAINT)
        jump = (x + w - 150, ry + 24, 110, 34)
        c.rounded(jump[0], jump[1], jump[2], jump[3], 10,
                  C.PRIMARY_SOFT, C.PRIMARY)
        c.text("前往设置", jump[0], jump[1], jump[2], jump[3], 11.5,
               C.PRIMARY, True, gdi.ALIGN_CENTER)
        self._hotspots.append({
            "kind": "set_cat", "rect": jump, "cat": "快捷键",
        })
        c.reset_clip()

    def _draw_switch(self, c, rect, on: bool, kind: str) -> None:
        x, y, w, h = rect
        hovered = self._hover == kind
        c.rounded(x, y, w, h, h // 2,
                  C.SUCCESS if on else C.SCROLL,
                  None if on else C.BORDER)
        knob = h - 6
        kx = x + w - knob - 3 if on else x + 3
        c.ellipse(kx, y + 3, knob, knob, C.CARD)
        if hovered:
            c.stroke_rounded(x, y, w, h, h // 2, C.PRIMARY, 1.2)
        self._hotspots.append({"kind": kind, "rect": rect})

    def _draw_about_settings(self, c, x, y, w, h) -> None:
        c.rounded(x, y, w, h, 12, C.CARD, C.BORDER)
        c.rounded(x + 22, y + 24, 46, 46, 13, C.PRIMARY,
                  gradient=C.PRIMARY_HOVER)
        c.text("窗", x + 22, y + 24, 46, 46, 21,
               gdi.argb(255, 255, 255), True, gdi.ALIGN_CENTER)
        c.text("窗口工具集合", x + 84, y + 24, w - 110, 26, 16,
               C.TEXT, True)
        c.text(f"版本 {__version__}  ·  本机自绘界面，无需浏览器",
               x + 84, y + 52, w - 110, 20, 11, C.TEXT_SUB)
        c.rect(x + 22, y + 94, w - 44, 1, C.DIVIDER)
        lines = (
            "窗口布局守护：保存并自动还原窗口位置",
            "鼠标屏幕锁定：把光标限制在当前显示器",
            "本机信息：查看系统、硬件与网卡地址",
            "桌面快捷方式与开机自启：首次启动询问，可随时修改",
        )
        for index, text in enumerate(lines):
            c.ellipse(x + 26, y + 118 + index * 32, 6, 6, C.PRIMARY)
            c.text(text, x + 42, y + 110 + index * 32, w - 70, 22,
                   11.5, C.TEXT_SUB)

    def _hotkey_groups(self) -> List[dict]:
        groups = []
        for index, block in enumerate(self._tool_blocks):
            rows = self.hotkey_rows(block["name"])
            if not rows:
                continue
            groups.append({
                "name": block["name"],
                "rows": rows,
                "index": index,
            })
        names = {group["name"] for group in groups}
        if groups and self._settings_hotkey_tool not in names:
            self._settings_hotkey_tool = groups[0]["name"]
        return groups

    def _draw_settings_hotkeys(self, c, x, y, w, h) -> None:
        """左边选功能，右边只显示该功能的快捷键，新增功能只增加一行。"""
        groups = self._hotkey_groups()
        nav_w = 168 if w >= 430 else 148
        gap = 12
        c.rounded(x, y, nav_w, h, 10, C.CARD_SOFT)
        detail_x = x + nav_w + gap
        detail_w = max(120, w - nav_w - gap)
        c.rounded(detail_x, y, detail_w, h, 10, C.CARD, C.BORDER)
        if not groups:
            c.text("还没有可设置的快捷键", x + 12, y + 16, nav_w - 24, 28,
                   12, C.TEXT_FAINT)
            return

        list_y = y + 8
        list_h = h - 16
        bounds = (x, list_y, nav_w, list_h)
        self._list_rects["settings_nav"] = bounds
        total_h = len(groups) * SETTINGS_TOOL_ROW
        max_scroll = max(0, total_h - list_h)
        self._settings_nav_scroll = min(
            max(0, self._settings_nav_scroll), max_scroll)
        palette = [C.PRIMARY, C.WARNING, C.SUCCESS]
        c.clip(x + 4, list_y, nav_w - 8, list_h)
        try:
            for index, group in enumerate(groups):
                ry = list_y + index * SETTINGS_TOOL_ROW - self._settings_nav_scroll
                if ry + SETTINGS_TOOL_ROW < list_y or ry > list_y + list_h:
                    continue
                name = group["name"]
                active = name == self._settings_hotkey_tool
                hovered = self._hover == ("set_hk_tool", name)
                row = (x + 6, ry + 2, nav_w - 16, SETTINGS_TOOL_ROW - 6)
                accent = palette[group["index"] % len(palette)]
                if active:
                    c.rounded(row[0], row[1], row[2], row[3], 8, C.PRIMARY_SOFT)
                    c.rounded(row[0], row[1] + 8, 3, row[3] - 16, 1, accent)
                elif hovered:
                    c.rounded(row[0], row[1], row[2], row[3], 8, C.CARD)
                c.text(name, row[0] + 12, row[1], row[2] - 42, row[3], 12.5,
                       C.TEXT, active)
                count = str(len(group["rows"]))
                c.text(count, row[0] + row[2] - 28, row[1], 22, row[3], 11,
                       C.PRIMARY if active else C.TEXT_FAINT, False,
                       gdi.ALIGN_CENTER)
                hit = self._clip_rect(row, bounds)
                if hit:
                    self._hotspots.append({
                        "kind": "set_hk_tool", "rect": hit, "tool": name,
                    })
        finally:
            c.reset_clip()
        self._draw_scrollbar(
            c, x + nav_w - 8, list_y, list_h, self._settings_nav_scroll,
            max_scroll, list_h, max(1, total_h))

        selected = next(
            (group for group in groups
             if group["name"] == self._settings_hotkey_tool),
            groups[0])
        self._draw_settings_key_list(
            c, detail_x + 12, y + 8, detail_w - 24, h - 16, selected)

    def _draw_settings_key_list(self, c, x, y, w, h, group: dict) -> None:
        name = group["name"]
        rows = group["rows"]
        c.text(name, x, y, w, 24, 14, C.TEXT, True)
        c.text("改过之后，默认组合不再响应",
               x, y + 24, w, 18, 11, C.TEXT_FAINT)
        list_y = y + 50
        list_h = max(36, h - 50)
        bounds = (x, list_y, w, list_h)
        self._list_rects["settings_keys"] = bounds
        total_h = max(SETTINGS_KEY_ROW, len(rows) * SETTINGS_KEY_ROW)
        max_scroll = max(0, total_h - list_h)
        self._settings_scroll = min(max(0, self._settings_scroll), max_scroll)
        c.clip(x, list_y, w, list_h)
        try:
            for index, row in enumerate(rows):
                ry = list_y + index * SETTINGS_KEY_ROW - self._settings_scroll
                if ry + SETTINGS_KEY_ROW < list_y or ry > list_y + list_h:
                    continue
                if index:
                    c.rect(x, ry, w - 8, 1, C.DIVIDER)
                c.text(row["title"], x, ry + 4, w - 8, 22, 13, C.TEXT, True)
                self._draw_key_controls(c, x, ry + 28, w - 8, row, bounds)
        finally:
            c.reset_clip()
        self._draw_scrollbar(
            c, x + w - 4, list_y, list_h, self._settings_scroll, max_scroll,
            list_h, total_h)

    def _draw_key_controls(self, c, x, y, w, row, bounds) -> None:
        reset_w = 64 if row["custom"] else 0
        edit_w = 56
        gap = 8
        edit_x = x + w - edit_w - (gap + reset_w if reset_w else 0)
        chip_w = max(88, int(c.measure(row["label"], 11.5) + 16))
        chip_w = min(chip_w, max(72, edit_x - x - gap))
        c.rounded(x, y, chip_w, 28, 8, C.PRIMARY_SOFT)
        c.text(row["label"], x, y, chip_w, 28, 11.5, C.PRIMARY, True,
               gdi.ALIGN_CENTER)
        edit = (edit_x, y - 2, edit_w, 32)
        self._draw_mini_button(c, "hk_edit", row["id"], "修改", edit, bounds)
        if reset_w:
            reset = (edit_x + edit_w + gap, y - 2, reset_w, 32)
            self._draw_mini_button(
                c, "hk_reset", row["id"], "默认", reset, bounds)

    def _draw_hotkey_block(self, c, x, y, w, rows, bounds, heading=True) -> None:
        offset = 0
        if heading:
            c.text("快捷键", x, y, w - 180, 24, 13, C.TEXT, True)
            c.text("点「修改」后按下新组合，可恢复默认", x + w - 230, y, 230, 24,
                   11, C.TEXT_FAINT, False, gdi.ALIGN_RIGHT)
            offset = 30
        for index, row in enumerate(rows):
            ry = y + offset + index * 44
            c.text(row["title"], x, ry, w * 0.38, 36, 12.5, C.TEXT, True)
            reset_w = 64 if row["custom"] else 0
            edit = (x + w - reset_w - (8 if reset_w else 0) - 56, ry + 2, 56, 32)
            chip_w = max(88, int(c.measure(row["label"], 11.5) + 16))
            chip_w = min(chip_w, max(88, edit[0] - x - int(w * 0.38) - 12))
            chip_x = edit[0] - 8 - chip_w
            c.rounded(chip_x, ry + 4, chip_w, 28, 8, C.PRIMARY_SOFT)
            c.text(row["label"], chip_x, ry + 4, chip_w, 28, 11.5, C.PRIMARY,
                   True, gdi.ALIGN_CENTER)
            self._draw_mini_button(c, "hk_edit", row["id"], "修改", edit, bounds)
            if reset_w:
                reset = (edit[0] + edit[2] + 8, ry + 2, reset_w, 32)
                self._draw_mini_button(
                    c, "hk_reset", row["id"], "默认", reset, bounds)

    def _draw_mini_button(self, c, kind, binding_id, label, rect, bounds,
                          tone: str = "default") -> None:
        key = kind if not binding_id else (kind, binding_id)
        hovered = self._hover == key
        pressed = self._pressed == key
        x, y, w, h = rect
        if tone == "primary":
            fill = C.PRIMARY_DARK if pressed else (
                C.PRIMARY_HOVER if hovered else C.PRIMARY)
            border = None
            color = gdi.argb(255, 255, 255)
            bold = True
        else:
            fill = C.PRIMARY_SOFT if hovered else C.CARD
            border = C.PRIMARY if hovered else C.SCROLL
            color = C.TEXT
            bold = False
        c.rounded(x, y + (1 if pressed else 0), w, h, 8, fill, border)
        c.text(label, x, y + (1 if pressed else 0), w, h, 12, color, bold,
               gdi.ALIGN_CENTER)
        hit = self._clip_rect(rect, bounds)
        if hit:
            self._hotspots.append({
                "kind": kind, "rect": hit, "id": binding_id,
            })

    def _draw_hotkey_dialog(self, c, w: int, h: int) -> None:
        binding_id = self._dialog.get("id")
        binding = self._bindings.get(binding_id) or {}
        c.rect(0, 0, w, h, gdi.argb(15, 23, 42, 120))
        dw, dh = 460, 268
        dx = max(12, (w - dw) // 2)
        dy = max(12, (h - dh) // 2)
        c.rounded(dx + 4, dy + 8, dw, dh, 16, C.SHADOW)
        c.rounded(dx, dy, dw, dh, 16, C.CARD, C.BORDER)
        c.rounded(dx + 24, dy + 22, 44, 44, 12, C.PRIMARY_SOFT)
        c.text("键", dx + 24, dy + 22, 44, 44, 20, C.PRIMARY, True,
               gdi.ALIGN_CENTER)
        c.text("设置快捷键", dx + 80, dy + 20, dw - 104, 26, 18, C.TEXT, True)
        c.text(binding.get("title") or "", dx + 80, dy + 46, dw - 104, 20,
               12, C.TEXT_SUB)
        current = binding.get("label") or "未注册"
        default = ""
        if binding.get("defaults"):
            default = format_hotkey(*binding["defaults"][0])
        c.text("当前  %s" % current, dx + 26, dy + 84, dw - 52, 22, 13, C.TEXT)
        c.text("默认  %s" % (default or "无"), dx + 26, dy + 108, dw - 52, 22,
               12.5, C.TEXT_SUB)
        c.text("直接按下新的组合键。需要同时包含 Ctrl、Alt 或 Win。",
               dx + 26, dy + 140, dw - 52, 36, 12.5, C.TEXT_SUB, False,
               gdi.ALIGN_LEFT, gdi.VALIGN_TOP, False)
        custom = self._saved_hotkey(binding_id) is not None
        if custom:
            self._draw_dialog_button(
                c, "dlg_hk_reset", "恢复默认", "default",
                (dx + 24, dy + 196, 140, 46))
            self._draw_dialog_button(
                c, "dlg_hk_cancel", "取消", "primary",
                (dx + 176, dy + 196, dw - 200, 46))
        else:
            self._draw_dialog_button(
                c, "dlg_hk_cancel", "取消", "default",
                (dx + 24, dy + 196, dw - 48, 46))

    def _draw_guide(self, c, x: int, y: int, w: int, h: int) -> None:
        c.text("快速上手", x, y, w, 22, 13, C.TEXT, True)
        steps = [
            "先点「保存当前布局」，记下窗口位置与大小",
            "切换显示器模式后，布局会按比例自动还原",
            "窗口伸出屏幕时，用救援把它拉回工作区",
        ]
        for index, step in enumerate(steps):
            offset = y + 28 + index * 28
            c.rounded(x, offset + 2, 20, 20, 10, C.PRIMARY_SOFT)
            c.text(str(index + 1), x, offset + 2, 20, 20, 11, C.PRIMARY,
                   True, gdi.ALIGN_CENTER)
            c.text(step, x + 28, offset, w - 28, 24, 12, C.TEXT_SUB)

    @staticmethod
    def _compact_hint(hint: str) -> str:
        if not hint:
            return ""
        if "快捷键" in hint:
            key = hint.split("快捷键", 1)[1].split("；")[0].strip(" ：")
            return "" if not key or "未注册" in key else key
        if hint.startswith("切换 "):
            key = hint.split("；")[0].replace("切换 ", "").strip()
            return "" if not key or "未注册" in key else key
        return hint.split("；")[0].strip()

    def _draw_button(self, c, button: dict, rect) -> None:
        x, y, w, h = rect
        key = (button["tool"], button["id"])
        hovered = self._hover == key
        pressed = self._pressed == key
        active = button.get("active", False)
        tone = button.get("tone", "default")
        toggle = button.get("kind") == "toggle"
        hint = self._compact_hint(button.get("hint", ""))

        if active:
            fill = gdi.argb(209, 250, 229) if (hovered or pressed) else C.SUCCESS_SOFT
            border = C.SUCCESS
            text = C.SUCCESS
            hint_color = C.TEXT_SUB
            bold = True
        elif tone == "primary":
            fill = C.PRIMARY_DARK if pressed else (
                C.PRIMARY_HOVER if hovered else C.PRIMARY)
            border = None
            text = gdi.argb(255, 255, 255)
            hint_color = gdi.argb(255, 255, 255, 210)
            bold = True
        elif tone == "warning":
            fill = gdi.argb(255, 237, 213) if hovered else C.WARNING_SOFT
            border = C.WARNING
            text = C.WARNING
            hint_color = C.TEXT_SUB
            bold = True
        else:
            fill = C.PRIMARY_SOFT if hovered else C.CARD
            border = C.PRIMARY if hovered else C.BORDER
            text = C.TEXT
            hint_color = C.TEXT_SUB
            bold = False
        offset = 1 if pressed else 0
        radius = 12
        c.rounded(x, y + 2, w, h - 2, radius, C.SHADOW)
        c.rounded(x, y + offset, w, h - 2, radius, fill, border)

        if toggle:
            pill = "开" if active else "关"
            pill_w = 32
            c.text(button["label"], x + 12, y + offset + 3, w - pill_w - 28,
                   22, 13, text, bold)
            px = x + w - pill_w - 10
            py = y + offset + 6
            if active:
                pill_fill = C.SUCCESS
                pill_border = None
                pill_text = gdi.argb(255, 255, 255)
            else:
                pill_fill = C.CARD_SOFT
                pill_border = C.BORDER
                pill_text = C.TEXT_SUB
            c.rounded(px, py, pill_w, 20, 10, pill_fill, pill_border)
            c.text(pill, px, py, pill_w, 20, 11, pill_text, True,
                   gdi.ALIGN_CENTER)
            if hint:
                c.text(hint, x + 12, y + offset + 26, w - 24, 16, 10.5,
                       hint_color)
        elif hint:
            c.text(button["label"], x + 8, y + offset + 3, w - 16, 22, 13,
                   text, bold, gdi.ALIGN_CENTER)
            c.text(hint, x + 8, y + offset + 25, w - 16, 16, 10.5,
                   hint_color, False, gdi.ALIGN_CENTER)
        else:
            c.text(button["label"], x + 8, y + offset, w - 16, h - 2, 13,
                   text, bold, gdi.ALIGN_CENTER)

    def _draw_lists(self, c, x: int, y: int, w: int, h: int) -> None:
        gap = 16
        notice_h = max(140, int((h - gap) * 0.32))
        window_h = max(160, h - gap - notice_h)
        self._draw_list_card(c, x, y, w, notice_h, "最近通知",
                             self._notice_lines, LIST_ROW_NOTICE,
                             self._notice_scroll, "notice")
        self._draw_list_card(c, x, y + notice_h + gap, w, window_h,
                             "当前窗口", self._window_lines, LIST_ROW_WINDOW,
                             self._window_scroll, "window")

    def _draw_scrollbar(self, c, x, y, track_h, scroll, max_scroll,
                        view_h, total_h) -> None:
        if max_scroll <= 0 or track_h <= 8 or total_h <= 0:
            return
        c.rounded(x, y, 4, track_h, 2, C.DIVIDER)
        thumb_h = max(28, int(track_h * min(1.0, view_h / total_h)))
        thumb_h = min(thumb_h, track_h)
        thumb_y = y + int((track_h - thumb_h) * scroll / max_scroll)
        c.rounded(x, thumb_y, 4, thumb_h, 2, C.SCROLL)

    def _draw_list_card(self, c, x, y, w, h, title, lines, row_h,
                        scroll, which) -> None:
        accent = C.PRIMARY if which == "notice" else C.SUCCESS
        c.rounded(x, y + 3, w, h, CARD_RADIUS, C.SHADOW)
        c.rounded(x, y, w, h, CARD_RADIUS, C.CARD, C.BORDER)
        c.rounded(x + 16, y + 16, 3, 16, 1, accent)
        c.text(title, x + 28, y + 8, w - 110, 28, 14, C.TEXT, True)

        off_count = sum(1 for row in lines if row.get("off"))
        if which == "window" and off_count:
            badge = f"{off_count} 越界"
            badge_fill, badge_border, badge_color = (
                C.DANGER_SOFT, C.DANGER, C.DANGER)
        else:
            badge = str(len(lines)) if lines else "0"
            badge_fill, badge_border, badge_color = (
                C.CARD_SOFT, C.BORDER, C.TEXT_SUB)
        badge_w = max(28, int(c.measure(badge, 11) + 16))
        c.rounded(x + w - badge_w - 14, y + 12, badge_w, 22, 11,
                  badge_fill, badge_border)
        c.text(badge, x + w - badge_w - 14, y + 12, badge_w, 22, 11,
               badge_color, which == "window" and off_count > 0, gdi.ALIGN_CENTER)

        c.rect(x + 16, y + 42, w - 32, 1, C.DIVIDER)
        list_y = y + 48
        list_h = h - 58
        self._list_rects[which] = (x, list_y, w, list_h)

        visible = max(1, list_h // row_h)
        max_scroll = max(0, len(lines) - visible)
        scroll = max(0, min(scroll, max_scroll))
        if which == "notice":
            self._notice_scroll = scroll
        else:
            self._window_scroll = scroll

        c.clip(x + 8, list_y, w - 16, list_h)
        try:
            shown = range(scroll, min(len(lines), scroll + visible + 1))
            for index in shown:
                row_y = list_y + (index - scroll) * row_h
                if index > scroll:
                    c.rect(x + 16, row_y, w - 36, 1, C.DIVIDER)
                row = lines[index]
                if which == "notice":
                    self._draw_notice_row(c, x, row_y, w, row_h, row)
                else:
                    self._draw_window_row(c, x, row_y, w, row_h, row)
        finally:
            c.reset_clip()

        if not lines:
            empty = "还没有通知" if which == "notice" else "没有可管理的窗口"
            c.text(empty, x + 18, list_y + 8, w - 40, 28, 12, C.TEXT_FAINT)

        self._draw_scrollbar(
            c, x + w - 10, list_y, list_h - 4, scroll, max_scroll,
            visible * row_h, max(1, len(lines)) * row_h)

    def _draw_notice_row(self, c, x, y, w, h, row: dict) -> None:
        title = row.get("title", "") or ""
        message = row.get("message", "") or ""
        title_w = max(1, w - 112)
        message_w = max(1, w - 40)
        truncated = (
            c.measure(title, 12, True) > title_w
            or c.measure(message, 11.5) > message_w
        )
        rect = (x + 8, y, max(1, w - 22), h)
        self._notice_hits.append({
            "rect": rect,
            "truncated": truncated,
            "time": row.get("time", ""),
            "title": title,
            "message": message,
        })
        px, py = self._pointer if self._pointer else (-1, -1)
        rx, ry, rw, rh = rect
        if (truncated and self._dialog is None
                and rx <= px <= rx + rw and ry <= py <= ry + rh):
            c.rounded(rx, ry + 2, rw, rh - 4, 8, C.CARD_SOFT)
            self._pending_tip = {
                "anchor": rect,
                "time": row.get("time", ""),
                "title": title,
                "message": message,
            }
        c.text(row.get("time", ""), x + 16, y + 2, 72, 20, 11, C.TEXT_FAINT)
        c.text(title, x + 88, y + 2, title_w, 20, 12, C.TEXT, True)
        c.text(message, x + 16, y + 22, message_w, 18, 11.5, C.TEXT_SUB)

    def _wrap_lines(self, c, text: str, size: float, width: float) -> List[str]:
        text = " ".join((text or "").split())
        if not text:
            return []
        if c.measure(text, size) <= width:
            return [text]
        lines: List[str] = []
        current = ""
        for ch in text:
            trial = current + ch
            if current and c.measure(trial, size) > width:
                lines.append(current)
                current = "" if ch == " " else ch
            else:
                current = trial
        if current:
            lines.append(current)
        return lines

    def _draw_notice_tip(self, c, w: int, h: int) -> None:
        tip = self._pending_tip
        if not tip or self._dialog is not None:
            return
        pad_x, pad_y = 14, 10
        max_inner = min(400, max(180, w - 24 - pad_x * 2))
        message_lines = self._wrap_lines(c, tip["message"], 12, max_inner)
        title = tip["title"] or ""
        when = tip["time"] or ""
        head_w = c.measure(when, 11) + (8 if when and title else 0) + c.measure(
            title, 12.5, True)
        body_w = max((c.measure(line, 12) for line in message_lines), default=0)
        inner = min(max_inner, max(160, head_w, body_w))
        if body_w > inner:
            message_lines = self._wrap_lines(c, tip["message"], 12, inner)
        tip_w = int(inner + pad_x * 2)
        tip_h = pad_y * 2 + 22 + max(1, len(message_lines)) * 20
        ax, ay, aw, ah = tip["anchor"]
        tx = ax
        ty = ay + ah + 4
        if ty + tip_h > h - 8:
            ty = ay - tip_h - 4
        if tx + tip_w > w - 8:
            tx = w - 8 - tip_w
        tx = max(8, tx)
        ty = max(8, min(ty, h - tip_h - 8))
        c.rounded(tx + 2, ty + 3, tip_w, tip_h, 10, C.SHADOW)
        c.rounded(tx, ty, tip_w, tip_h, 10, C.CARD, C.PRIMARY)
        cursor = tx + pad_x
        if when:
            time_w = int(c.measure(when, 11) + 8)
            c.text(when, cursor, ty + pad_y, time_w, 22, 11, C.TEXT_FAINT)
            cursor += time_w
        c.text(title, cursor, ty + pad_y, tx + tip_w - pad_x - cursor, 22,
               12.5, C.TEXT, True)
        line_y = ty + pad_y + 22
        for line in message_lines or [""]:
            c.text(line, tx + pad_x, line_y, inner, 20, 12, C.TEXT_SUB,
                   False, gdi.ALIGN_LEFT, gdi.VALIGN_MIDDLE, False)
            line_y += 20

    def _draw_window_row(self, c, x, y, w, h, row: dict) -> None:
        """窗口行：借鉴 Oink 设备卡片的缩略图 + 名称标签样式。"""
        off = bool(row.get("off"))
        tx, ty, tw, th = x + 10, y + 9, 40, 28
        if off:
            c.rounded(tx, ty, tw, th, 6, C.DANGER_SOFT, C.DANGER)
            c.text("!", tx, ty, tw, th, 15, C.DANGER, True, gdi.ALIGN_CENTER)
        else:
            c.rounded(tx, ty, tw, th, 6,
                      gdi.argb(219, 234, 254), gdi.argb(191, 219, 254))
            c.rounded(tx + 4, ty + 4, 14, 20, 3, gdi.argb(59, 130, 246))
            c.rounded(tx + 21, ty + 4, 15, 8, 2, gdi.argb(147, 197, 253))
            c.rounded(tx + 21, ty + 16, 15, 8, 2, gdi.argb(147, 197, 253))
        title_color = C.DANGER if off else C.TEXT
        c.text(row.get("title", ""), x + 58, y + 3, w - 76, 22, 12.5,
               title_color, True)
        process = row.get("process") or ""
        sub = ("越界" + (f"  ·  {process}" if process else "")) if off else process
        meta = f"{row.get('pos', '')}  {row.get('size', '')}"
        c.text(sub, x + 58, y + 24, int(w * 0.42), 18, 11, C.TEXT_FAINT)
        c.text(meta, x + 58, y + 24, w - 76, 18, 11,
               C.DANGER if off else C.TEXT_SUB, False, gdi.ALIGN_RIGHT)

    # -------------------------------------------------------- 交互处理 --
    def _hit_test(self, x: int, y: int, buttons_only: bool = False):
        for spot in self._hotspots:
            kind = spot["kind"]
            is_dialog = kind.startswith("dlg_")
            # 对话框出现时屏蔽背景交互，反之屏蔽对话框残留热点
            if is_dialog != (self._dialog is not None):
                continue
            rx, ry, rw, rh = spot["rect"]
            if rx <= x <= rx + rw and ry <= y <= ry + rh:
                if kind == "action":
                    return ("action", (spot["tool"], spot["action"]), spot)
                if kind == "tool":
                    return ("tool", spot["tool"], spot)
                if kind in ("hk_edit", "hk_reset"):
                    return (kind, (kind, spot["id"]), spot)
                if kind in ("set_make", "set_ask"):
                    return (kind, kind, spot)
                if kind == "set_hk_tool":
                    return (kind, ("set_hk_tool", spot["tool"]), spot)
                if not buttons_only:
                    return (kind, kind, spot)
        return None

    def _notice_tip_key(self):
        """当前指针下需要展开全文的通知；没有则返回 None。"""
        if not self._pointer or self._dialog is not None:
            return None
        px, py = self._pointer
        for index, hit in enumerate(self._notice_hits):
            if not hit.get("truncated"):
                continue
            rx, ry, rw, rh = hit["rect"]
            if rx <= px <= rx + rw and ry <= py <= ry + rh:
                return index
        return None

    def _in_rect(self, x: int, y: int, which: str) -> bool:
        rect = self._list_rects.get(which)
        if not rect:
            return False
        rx, ry, rw, rh = rect
        return rx <= x <= rx + rw and ry <= y <= ry + rh

    def _run_action(self, tool_name: str, action_id: str) -> None:
        tool = self.manager.find(tool_name) if self.manager else None
        if not tool:
            return
        try:
            result = tool.handle_action(action_id)
            self.log.info("窗体动作 %s/%s -> %s", tool_name, action_id, result)
            self._refresh()
        except Exception:
            self.log.exception("窗体动作执行失败 %s/%s", tool_name, action_id)

    @staticmethod
    def _safe_call(callback) -> None:
        try:
            callback()
        except Exception:
            import logging
            logging.getLogger("window_tools").exception("热键回调执行失败")

    def _apply_scroll(self, x: int, y: int, delta: int) -> None:
        if self._in_rect(x, y, "notice"):
            self._notice_scroll = max(0, self._notice_scroll - delta)
        elif self._in_rect(x, y, "window"):
            self._window_scroll = max(0, self._window_scroll - delta)
        elif self._in_rect(x, y, "nav"):
            self._nav_scroll = max(0, self._nav_scroll - delta * NAV_ROW)
        elif self._in_rect(x, y, "actions"):
            self._action_scroll = max(0, self._action_scroll - delta * (BTN_H + BTN_GAP_Y))
        elif self._in_rect(x, y, "settings_nav"):
            self._settings_nav_scroll = max(
                0, self._settings_nav_scroll - delta * SETTINGS_TOOL_ROW)
        elif self._in_rect(x, y, "settings_keys"):
            self._settings_scroll = max(
                0, self._settings_scroll - delta * SETTINGS_KEY_ROW)
        self._invalidate()

    @staticmethod
    def _screen_pos(lparam) -> tuple[int, int]:
        """解析 WM_NCHITTEST / WM_MOUSEWHEEL 的屏幕坐标（支持负数）。"""
        x = lparam & 0xFFFF
        y = (lparam >> 16) & 0xFFFF
        if x & 0x8000:
            x -= 0x10000
        if y & 0x8000:
            y -= 0x10000
        return x, y

    @staticmethod
    def _nonclient_hit(px: int, py: int, width: int, height: int,
                       edge: int = 8) -> int:
        """只有四边用于系统缩放，其余区域必须返回 HTCLIENT。

        早期版本把整个客户区返回 HTCAPTION 以支持拖拽，结果是 Windows 把
        左键消息改派为 WM_NCLBUTTONDOWN/UP，自绘按钮永远收不到点击。
        """
        left = px < edge
        right = px > width - edge
        top = py < edge
        bottom = py > height - edge
        if left and top:
            return win32con.HTTOPLEFT
        if right and top:
            return win32con.HTTOPRIGHT
        if left and bottom:
            return win32con.HTBOTTOMLEFT
        if right and bottom:
            return win32con.HTBOTTOMRIGHT
        if left:
            return win32con.HTLEFT
        if right:
            return win32con.HTRIGHT
        if top:
            return win32con.HTTOP
        if bottom:
            return win32con.HTBOTTOM
        return win32con.HTCLIENT

    @staticmethod
    def _start_title_drag(hwnd: int) -> None:
        """标题栏空白区拖拽：显式交给系统处理，不影响客户区按钮点击。"""
        try:
            win32gui.ReleaseCapture()
            win32gui.SendMessage(hwnd, win32con.WM_NCLBUTTONDOWN,
                                 win32con.HTCAPTION, 0)
        except Exception:
            pass

    def _logical_pos(self, lparam) -> tuple[int, int]:
        """把鼠标消息中的物理像素坐标换算为布局使用的逻辑坐标。"""
        x, y = self._screen_pos(lparam)
        return int(x / self.scale), int(y / self.scale)

    # -------------------------------------------------------- 消息处理 --
    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAYICON:
                self._handle_tray_message(lparam)
                return 0

            if msg == WM_SHOWME:
                # 第二实例（快捷方式/exe）请求唤醒隐藏在托盘的窗口
                self.log.info("收到唤醒请求，显示已有窗口")
                self._show_window()
                self.notify("程序已在运行，窗口已显示")
                return 0

            if msg == win32con.WM_PAINT:
                self._paint(hwnd)
                return 0

            if msg == win32con.WM_ERASEBKGND:
                return 1

            if msg == win32con.WM_NCHITTEST:
                sx, sy = self._screen_pos(lparam)
                px, py = win32gui.ScreenToClient(hwnd, (sx, sy))
                rect = win32gui.GetClientRect(hwnd)
                edge = max(6, int(8 * self.scale))
                return self._nonclient_hit(px, py, rect[2], rect[3], edge)

            if msg == win32con.WM_SETCURSOR:
                cursor = win32con.IDC_HAND if self._hover else \
                    win32con.IDC_ARROW
                win32gui.SetCursor(win32gui.LoadCursor(0, cursor))
                return 1

            if msg == win32con.WM_MOUSEMOVE:
                x, y = self._logical_pos(lparam)
                if not self._tracking_leave:
                    _track_mouse_leave(hwnd)
                    self._tracking_leave = True
                previous = self._notice_tip_key()
                self._pointer = (x, y)
                hit = self._hit_test(x, y)
                new_hover = hit[1] if hit else None
                if new_hover != self._hover or previous != self._notice_tip_key():
                    self._hover = new_hover
                    self._invalidate()
                return 0

            if msg == WM_MOUSELEAVE:
                self._tracking_leave = False
                self._pointer = None
                if self._hover is not None or self._pending_tip is not None:
                    self._hover = None
                    self._invalidate()
                return 0

            if msg == win32con.WM_LBUTTONDOWN:
                x, y = self._logical_pos(lparam)
                hit = self._hit_test(x, y)
                if hit:
                    self._pressed = hit[1]
                    self._invalidate()
                    return 0
                # 标题栏空白处拖动窗口；按钮区域不交给系统，确保点击能触发
                if 0 <= y < TITLE_H:
                    self._start_title_drag(hwnd)
                return 0

            if msg == win32con.WM_LBUTTONUP:
                x, y = self._logical_pos(lparam)
                hit = self._hit_test(x, y)
                previous = self._pressed
                self._pressed = None
                self._invalidate()
                if hit and previous == hit[1]:
                    kind, key, spot = hit
                    if kind == "dlg_cancel":
                        self._close_dialog()
                    elif kind == "dlg_hide":
                        self._close_dialog()
                        self._hide_window()
                    elif kind == "dlg_exit":
                        self._exit_app()
                    elif kind == "dlg_sc_keep":
                        self._dialog["keep"] = not self._dialog.get(
                            "keep", False)
                        self._invalidate()
                    elif kind == "dlg_sc_create":
                        self._resolve_shortcut(True)
                    elif kind == "dlg_sc_skip":
                        self._resolve_shortcut(False)
                    elif kind == "dlg_as_keep":
                        self._dialog["keep"] = not self._dialog.get(
                            "keep", False)
                        self._invalidate()
                    elif kind == "dlg_as_enable":
                        self._resolve_autostart(True)
                    elif kind == "dlg_as_skip":
                        self._resolve_autostart(False)
                    elif kind == "dlg_relocate_ok":
                        self._dismiss_relocate()
                    elif kind == "dlg_hk_cancel":
                        self._close_dialog()
                    elif kind == "dlg_hk_reset":
                        self.reset_hotkey(self._dialog.get("id"))
                        self._close_dialog()
                    elif kind == "hk_edit":
                        self._dialog = {
                            "kind": "hotkey", "id": spot["id"], "hotspots": [],
                        }
                        self._invalidate()
                    elif kind == "hk_reset":
                        self.reset_hotkey(spot["id"])
                    elif kind in ("settings", "nav_settings"):
                        self._open_settings()
                    elif kind == "settings_back":
                        target = self._settings_return_tool
                        names = [b["name"] for b in self._tool_blocks]
                        self._selected_tool = (target if target in names
                                               else (names[0] if names else ""))
                        self._invalidate()
                    elif kind == "set_cat":
                        self._settings_category = spot["cat"]
                        self._invalidate()
                    elif kind == "set_make":
                        self._create_shortcut_action()
                        self._invalidate()
                    elif kind == "set_ask":
                        self._reset_shortcut_prompt()
                    elif kind == "set_as_toggle":
                        enabled, _current = autostart.status()
                        self._apply_autostart(not enabled)
                    elif kind == "set_as_ask":
                        self._reset_autostart_prompt()
                    elif kind == "set_hk_tool":
                        if self._settings_hotkey_tool != spot["tool"]:
                            self._settings_hotkey_tool = spot["tool"]
                            self._settings_scroll = 0
                        self._invalidate()
                    elif kind == "tool":
                        if self._selected_tool != spot["tool"]:
                            self._selected_tool = spot["tool"]
                            self._action_scroll = 0
                        self._invalidate()
                    elif kind == "action":
                        threading.Thread(
                            target=self._run_action,
                            args=(spot["tool"], spot["action"]), daemon=True,
                        ).start()
                    elif kind == "min":
                        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                    elif kind == "max":
                        action = (win32con.SW_RESTORE if win32gui.IsZoomed(hwnd)
                                  else win32con.SW_MAXIMIZE)
                        win32gui.ShowWindow(hwnd, action)
                    elif kind == "close":
                        self._ask_close()
                return 0

            if msg == win32con.WM_MOUSEWHEEL:
                if self._dialog is not None:
                    return 0
                delta = (int(wparam) >> 16) & 0xFFFF
                if delta >= 0x8000:
                    delta -= 0x10000
                sx, sy = self._screen_pos(lparam)
                cx, cy = win32gui.ScreenToClient(hwnd, (sx, sy))
                x, y = int(cx / self.scale), int(cy / self.scale)
                self._apply_scroll(x, y, 1 if delta > 0 else -1)
                return 0

            if msg == win32con.WM_HOTKEY:
                callback = self._hotkey_callbacks.get(int(wparam))
                if callback:
                    threading.Thread(
                        target=self._safe_call, args=(callback,), daemon=True
                    ).start()
                return 0

            if msg == win32con.WM_TIMER:
                timer_id = int(wparam)
                if timer_id == TIMER_REFRESH:
                    self._refresh()
                elif timer_id == TIMER_DISPLAY:
                    _kill_timer(hwnd, TIMER_DISPLAY)
                    self.on_display_change()
                return 0

            if msg == win32con.WM_DISPLAYCHANGE:
                self._schedule_display_change("系统消息")
                return 0

            if (msg == win32con.WM_SYSCOMMAND
                    and (int(wparam) & 0xFFF0) == SC_MONITORPOWER):
                self._schedule_display_change("显示器电源状态")
                # 必须继续交给 DefWindowProc，否则会阻止系统关屏/唤醒

            if msg == win32con.WM_SIZE:
                self._clamp_scroll()
                self._invalidate()
                return 0

            if msg in (win32con.WM_KEYDOWN, win32con.WM_SYSKEYDOWN):
                if (self._dialog or {}).get("kind") == "hotkey":
                    if int(wparam) == win32con.VK_ESCAPE:
                        self._close_dialog()
                    else:
                        self._capture_hotkey(int(wparam))
                    return 0

            if msg == win32con.WM_KEYDOWN and wparam == win32con.VK_ESCAPE:
                if (self._dialog or {}).get("kind") == "relocate":
                    self._dismiss_relocate()
                    return 0
                if self._dialog is not None:
                    self._close_dialog()
                    return 0

            if msg == win32con.WM_CLOSE:
                if self._force_exit:
                    self._force_exit = False
                    self._exit_app()
                else:
                    self._ask_close()
                return 0

            if msg == win32con.WM_DESTROY:
                _kill_timer(hwnd, TIMER_REFRESH)
                _kill_timer(hwnd, TIMER_DISPLAY)
                win32gui.PostQuitMessage(0)
                return 0
        except Exception:
            self.log.exception("窗体消息处理异常 msg=%s", msg)
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)
