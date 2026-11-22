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
from ..ui.tray import (
    TRAY_EXIT, TRAY_HIDE, TRAY_SHOW, TrayIcon, TrayMenu, WM_TRAYICON,
)
from ..utils import shortcut, winapi

ID_BASE = 100
TIMER_REFRESH = 1
TIMER_DISPLAY = 2
REFRESH_MS = 800
DISPLAY_DEBOUNCE_MS = 1500
SC_MONITORPOWER = getattr(win32con, "SC_MONITORPOWER", 0xF170)

TITLE_H = 64
PAD = 22
INFO_H = 100
CARD_RADIUS = 14
BTN_H = 46
BTN_GAP_X = 12
BTN_GAP_Y = 12
LIST_ROW_NOTICE = 32
LIST_ROW_WINDOW = 30
FOOTER_H = 28

_user32 = ctypes.windll.user32
_user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                             ctypes.c_void_p]
_user32.SetTimer.restype = ctypes.c_uint
_user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_user32.KillTimer.restype = ctypes.c_bool


def _set_timer(hwnd: int, timer_id: int, milliseconds: int) -> None:
    _user32.SetTimer(hwnd, timer_id, milliseconds, None)


def _kill_timer(hwnd: int, timer_id: int) -> None:
    _user32.KillTimer(hwnd, timer_id)


def _try_foreground(hwnd: int) -> None:
    """尽力把窗口置前；Windows 前台锁定可能导致失败，忽略即可。"""
    try:
        _user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
    except Exception:
        pass


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

        self._tool_blocks: List[dict] = []
        self._hotspots: List[dict] = []
        self._list_rects: dict = {}
        self._hover = None
        self._pressed = None
        self._left_scroll = 0
        self._notice_scroll = 0
        self._window_scroll = 0
        self._notice_lines: List[str] = []
        self._window_lines: List[str] = []
        self._metrics: List[Tuple[str, str, int]] = []

        self.tray: TrayIcon | None = None
        self.tray_menu: TrayMenu | None = None

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
            win32gui.RegisterHotKey(self.hwnd, hid, modifiers, vk)
        except Exception as exc:
            self.log.warning("热键注册失败 key=%s: %s", key, exc)
            return False
        self._next_hotkey += 1
        self._hotkey_ids[key] = hid
        self._hotkey_callbacks[hid] = callback
        return True

    def notify(self, message: str, title: str | None = None) -> None:
        title = title or self.title
        self.notices.append({
            "time": time.strftime("%H:%M:%S"),
            "title": title,
            "message": str(message),
        })
        self.log.info("[通知] %s - %s", title, message)
        self._invalidate()

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

        class_name = f"WindowToolsDesktop_{os.getpid()}"
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
        self._maybe_ask_shortcut()

        try:
            win32gui.PumpMessages()
        finally:
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
        elif cmd == TRAY_EXIT:
            # 托盘菜单“退出程序”是明确指令，不再弹确认框
            if self.hwnd:
                self._force_exit = True
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)

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
    def _maybe_ask_shortcut(self) -> None:
        """启动后按需询问是否创建桌面快捷方式。"""
        sc = self.config.get("shortcut", {}) or {}
        if sc.get("asked") and sc.get("remember"):
            return  # 用户已选择“保持选择”，不再询问
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
        self.config.data["shortcut"] = sc
        self.config.save()
        self._close_dialog()
        self.log.info("快捷方式选择：create=%s remember=%s", create, keep)
        if create:
            self._create_shortcut_action()
        if keep:
            self.notify("已记住选择，以后不再询问")

    def _create_shortcut_action(self) -> None:
        """根据当前程序路径生成桌面快捷方式并反馈结果。"""
        try:
            result = shortcut.create_shortcut()
        except Exception as exc:
            self.log.exception("生成桌面快捷方式失败")
            result = {"ok": False, "message": "生成失败：%s" % exc}
        level = self.log.info if result.get("ok") else self.log.warning
        level("桌面快捷方式：%s", result.get("message"))
        self.notify(result.get("message", "快捷方式操作完成"))

    def _show_settings_menu(self) -> None:
        """标题栏设置按钮：弹出菜单（非卡片形式）。"""
        menu = win32gui.CreatePopupMenu()
        try:
            exists = shortcut.shortcut_exists()
            label = "更新桌面快捷方式" if exists else "生成桌面快捷方式"
            win32gui.AppendMenu(menu, win32con.MF_STRING, 3001, label)
            win32gui.AppendMenu(menu, win32con.MF_STRING, 3002,
                                "重新询问快捷方式选择")
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
            if cmd == 3001:
                self._create_shortcut_action()
            elif cmd == 3002:
                sc = dict(self.config.get("shortcut", {}) or {})
                sc["asked"] = False
                sc["remember"] = False
                self.config.data["shortcut"] = sc
                self.config.save()
                self.notify("下次启动将重新询问快捷方式")
        finally:
            win32gui.DestroyMenu(menu)

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
                    self.tray_menu.show(win32gui.IsWindowVisible(self.hwnd))
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
                "status": tool["status"],
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
            vx, vy, vw, vh = signature["virtual"]
            monitors = signature["monitors"]
            primary = monitors[0] if monitors else [0, 0, 0, 0]
            dpi_label = {
                "PER_MONITOR_AWARE_V2": "Per-Monitor V2",
                "PER_MONITOR_AWARE": "Per-Monitor",
                "SYSTEM_AWARE": "System",
                "UNAWARE": "Unaware",
            }.get(self.dpi_mode, self.dpi_mode)
            self._metrics = [
                ("虚拟桌面", f"{vw}×{vh}", C.PRIMARY),
                ("显示器", f"{signature['count']} 台", C.SUCCESS),
                ("主屏", f"{primary[2]}×{primary[3]}", C.WARNING),
                ("DPI 模式", dpi_label, C.PRIMARY),
            ]

            self._notice_lines = [
                f"{n['time']}  {n['title']} · {n['message']}"
                for n in reversed(list(self.notices))
            ]

            virtual = winapi.bounds_to_rect(winapi.get_virtual_bounds())
            lines = []
            for window in winapi.enumerate_windows(self.hwnd):
                rect = window["rect"]
                off = winapi.visible_ratio(rect, virtual) < 0.35
                prefix = "[越界] " if off else ""
                title = window["title"].replace("\n", " ")
                left, top, right, bottom = rect
                size = f"{right - left}×{bottom - top}"
                head = f"{prefix}{title}"
                process = window["process"]
                if process:
                    head += f"  —  {process}"
                lines.append(f"{head}  ·  {left},{top}  {size}")
            self._window_lines = lines

            if self.manager:
                descriptions = {t["name"]: t
                                for t in self.manager.describe_tools()}
                for block in self._tool_blocks:
                    tool = descriptions.get(block["name"])
                    if not tool:
                        continue
                    block["status"] = tool["status"]
                    active = {a["id"]: a["active"] for a in tool["actions"]
                              if a["kind"] == "toggle"}
                    for button in block["buttons"]:
                        if button["id"] in active:
                            button["active"] = active[button["id"]]
                            button["label"] = button["base_label"] + \
                                ("  [开]" if button["active"] else "  [关]")
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

        # 标题栏
        c.rounded(0, 0, w, TITLE_H, 1, C.HEADER_A, gradient=C.HEADER_B)
        c.rounded(18, 16, 34, 34, 9, gdi.argb(255, 255, 255, 42))
        c.text("窗", 18, 16, 34, 34, 17, gdi.argb(255, 255, 255), True,
               gdi.ALIGN_CENTER)
        c.text("窗口工具集合", 62, 10, 240, 28, 19,
               gdi.argb(255, 255, 255), True)
        c.text("多显示器窗口布局守护 · 自绘界面", 62, 37, 280, 20, 11.5,
               gdi.argb(255, 255, 255, 195))

        settings_rect = (w - 120, 17, 32, 32)
        min_rect = (w - 82, 17, 32, 32)
        close_rect = (w - 44, 17, 32, 32)
        # 设置按钮（弹出菜单，不做卡片）
        st_hover = self._hover == "settings"
        st_press = self._pressed == "settings"
        st_fill = gdi.argb(255, 255, 255, 58 if st_press else
                           (42 if st_hover else 0))
        if st_fill:
            c.rounded(settings_rect[0], settings_rect[1], 32, 32, 7, st_fill)
        c.text("设", settings_rect[0], settings_rect[1] - 1, 32, 34, 13,
               gdi.argb(255, 255, 255), False, gdi.ALIGN_CENTER)
        self._hotspots.append({"kind": "settings", "rect": settings_rect})

        for kind, rect, glyph in (("min", min_rect, "–"),
                                  ("close", close_rect, "×")):
            hovered = self._hover == kind
            pressed = self._pressed == kind
            fill = gdi.argb(255, 255, 255, 58 if pressed else
                            (42 if hovered else 0))
            if fill:
                c.rounded(rect[0], rect[1], rect[2], rect[3], 7, fill)
            color = gdi.argb(255, 255, 255) if kind != "close" or not hovered \
                else gdi.argb(255, 255, 225, 225)
            c.text(glyph, rect[0], rect[1] - 1, rect[2], rect[3] + 2, 18,
                   color, False, gdi.ALIGN_CENTER)
            self._hotspots.append({"kind": kind, "rect": rect})

        # 指标卡片
        info_y = TITLE_H + 16
        card_w = w - 2 * PAD
        c.rounded(PAD, info_y + 3, card_w, INFO_H, CARD_RADIUS, C.SHADOW)
        c.rounded(PAD, info_y, card_w, INFO_H, CARD_RADIUS, C.CARD, C.BORDER)
        cell_w = (card_w - 32) // max(1, len(self._metrics))
        for index, (label, value, accent) in enumerate(self._metrics):
            x = PAD + 18 + index * cell_w
            c.rounded(x, info_y + 24, 4, 52, 2, accent)
            c.text(label, x + 14, info_y + 16, cell_w - 22, 24, 11.5,
                   C.TEXT_SUB)
            c.text(value, x + 14, info_y + 42, cell_w - 22, 32, 17,
                   C.TEXT, True)

        body_y = info_y + INFO_H + 16
        body_h = max(120, h - body_y - FOOTER_H - 16)
        left_w = max(380, int((w - 2 * PAD - 18) * 0.58))
        right_x = PAD + left_w + 18
        right_w = max(280, w - PAD - right_x)

        self._list_rects["tools"] = (PAD, body_y, left_w, body_h)
        self._draw_tools(c, PAD, body_y, left_w, body_h)
        self._draw_lists(c, right_x, body_y, right_w, body_h)

        footer_y = h - FOOTER_H - 4
        c.text("所有操作都在左侧工具卡片 · 全局热键见状态行 · 数据保存在 data 目录",
               PAD, footer_y, w - 2 * PAD, FOOTER_H, 11.5, C.TEXT_FAINT)

        if self._dialog is not None:
            if self._dialog.get("kind") == "shortcut":
                self._draw_shortcut_dialog(c, w, h)
            else:
                self._draw_close_dialog(c, w, h)

    def _draw_dialog_button(self, c, kind, label, tone, rect) -> None:
        """通用对话框按钮：primary / danger / default。"""
        x, y, bw, bh = rect
        hovered = self._hover == kind
        pressed = self._pressed == kind
        if tone == "primary":
            fill = C.PRIMARY_DARK if pressed else (
                C.PRIMARY if hovered else C.PRIMARY)
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
        c.rounded(x + 1, y + 2, bw - 2, bh - 2, 9, C.SHADOW)
        c.rounded(x, y + offset, bw, bh - 2, 9, fill, border)
        c.text(label, x, y + offset, bw, bh - 2, 13, color, bold,
               gdi.ALIGN_CENTER)
        self._hotspots.append({"kind": kind, "rect": rect})

    def _draw_shortcut_dialog(self, c, w: int, h: int) -> None:
        """启动询问框：是否创建桌面快捷方式 + 保持选择勾选。"""
        c.rect(0, 0, w, h, gdi.argb(130, 14, 22, 40))
        dw, dh = 500, 292
        dx = max(12, (w - dw) // 2)
        dy = max(12, (h - dh) // 2)
        c.rounded(dx, dy + 6, dw, dh, 18, gdi.argb(48, 12, 22, 42))
        c.rounded(dx, dy, dw, dh, 18, C.CARD, C.BORDER)

        c.rounded(dx + 24, dy + 24, 44, 44, 12, C.PRIMARY_SOFT)
        c.text("捷", dx + 24, dy + 23, 44, 46, 22, C.PRIMARY, True,
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
        c.rounded(kb_rect[0], kb_rect[1], 20, 20, 5,
                  C.PRIMARY if keep else (C.CARD_SOFT if kh else C.CARD),
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

    def _draw_close_dialog(self, c, w: int, h: int) -> None:
        """自绘模态确认框：隐藏到托盘 / 退出程序 / 取消。"""
        # 半透明遮罩，背景仍可辨认但不可交互
        c.rect(0, 0, w, h, gdi.argb(130, 14, 22, 40))

        dw, dh = 470, 252
        dx = max(12, (w - dw) // 2)
        dy = max(12, (h - dh) // 2)
        c.rounded(dx, dy + 6, dw, dh, 18, gdi.argb(48, 12, 22, 42))
        c.rounded(dx, dy, dw, dh, 18, C.CARD, C.BORDER)

        c.rounded(dx + 24, dy + 24, 44, 44, 12, C.PRIMARY_SOFT)
        c.text("?", dx + 24, dy + 23, 44, 46, 25, C.PRIMARY, True,
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

    def _draw_tools(self, c, x: int, y: int, w: int, h: int) -> None:
        # 先测量总高度以支持滚动
        cards = []
        cur_y = y
        for block in self._tool_blocks:
            per_row = max(1, (w - 36 + BTN_GAP_X) // 170)
            buttons = block["buttons"]
            rows = max(1, (len(buttons) + per_row - 1) // per_row)
            card_h = 16 + 28 + 26 + rows * BTN_H + \
                (rows - 1) * BTN_GAP_Y + 14
            cards.append((block, cur_y, card_h, per_row))
            cur_y += card_h + 16
        guide_h = 126
        total_h = max(0, cur_y - y - 16) + guide_h + 16
        max_scroll = max(0, total_h - h)
        self._left_scroll = min(self._left_scroll, max_scroll)

        palette = [C.PRIMARY, C.WARNING, C.SUCCESS]
        c.clip(x, y, w, h)
        try:
            for index, (block, card_y, card_h, per_row) in enumerate(cards):
                top = card_y - self._left_scroll
                if top + card_h < y or top > y + h:
                    continue
                accent = palette[index % len(palette)]
                c.rounded(x, top + 3, w, card_h, CARD_RADIUS, C.SHADOW)
                c.rounded(x, top, w, card_h, CARD_RADIUS, C.CARD, C.BORDER)

                # 工具图标提升可辨识度，颜色随工具轮换
                c.rounded(x + 18, top + 14, 30, 30, 9, accent)
                glyph = block["name"][:1] or "工"
                c.text(glyph, x + 18, top + 14, 30, 30, 14,
                       gdi.argb(255, 255, 255), True, gdi.ALIGN_CENTER)
                c.text(block["name"], x + 58, top + 12, w - 160, 28, 15,
                       C.TEXT, True)
                badge_w = 64
                c.rounded(x + w - badge_w - 18, top + 14, badge_w, 24, 12,
                          C.PRIMARY_SOFT)
                c.text("v" + block["version"], x + w - badge_w - 18,
                       top + 14, badge_w, 24, 10.5, C.PRIMARY, False,
                       gdi.ALIGN_CENTER)

                status = block["status"]
                active = ("开" in status or "已锁定" in status
                          or "已保存" in status)
                c.ellipse(x + 20, top + 50, 8, 8,
                          C.SUCCESS if active else accent)
                c.text(status, x + 36, top + 44, w - 54, 26, 12,
                       C.TEXT_SUB)

                btn_w = (w - 36 - (per_row - 1) * BTN_GAP_X) // per_row
                for btn_index, button in enumerate(block["buttons"]):
                    row, col = divmod(btn_index, per_row)
                    bx = x + 18 + col * (btn_w + BTN_GAP_X)
                    by = top + 78 + row * (BTN_H + BTN_GAP_Y)
                    rect = (bx, by, btn_w, BTN_H)
                    self._draw_button(c, button, rect)
                    self._hotspots.append({
                        "kind": "action", "rect": rect,
                        "tool": block["name"], "action": button["id"],
                    })

            # 快速上手卡片（宿主级提示，不随工具增减变化）
            guide_top = cur_y - self._left_scroll
            if guide_top + guide_h >= y and guide_top <= y + h:
                c.rounded(x, guide_top + 3, w, guide_h, CARD_RADIUS, C.SHADOW)
                c.rounded(x, guide_top, w, guide_h, CARD_RADIUS,
                          C.CARD_SOFT, C.BORDER)
                c.text("快速上手", x + 18, guide_top + 10, w - 36, 26,
                       15, C.TEXT, True)
                c.rect(x + 18, guide_top + 38, w - 36, 1, C.DIVIDER)
                steps = [
                    "1  先点「保存当前布局」，记录窗口位置与大小",
                    "2  切换显示器模式（如 4K ↔ 1K），布局自动按比例还原",
                    "3  窗口越界可救援，鼠标可用锁定工具固定在当前屏幕",
                ]
                for step_index, step in enumerate(steps):
                    offset = guide_top + 42 + step_index * 25
                    c.ellipse(x + 20, offset + 6, 8, 8,
                              C.PRIMARY if step_index == 0 else C.TEXT_FAINT)
                    c.text(step, x + 38, offset, w - 56, 24, 12.5,
                           C.TEXT_SUB)
        finally:
            c.reset_clip()

        if max_scroll > 0:
            track_x, track_y = x + w - 5, y + 4
            track_h = h - 8
            c.rounded(track_x, track_y, 3, track_h, 1, C.DIVIDER)
            thumb_h = max(30, int(track_h * h / total_h))
            thumb_y = track_y + int((track_h - thumb_h) *
                                    self._left_scroll / max_scroll)
            c.rounded(track_x, thumb_y, 3, thumb_h, 1, C.PRIMARY)

    def _draw_button(self, c, button: dict, rect) -> None:
        x, y, w, h = rect
        key = (button["tool"], button["id"])
        hovered = self._hover == key
        pressed = self._pressed == key
        active = button.get("active", False)
        tone = button.get("tone", "default")

        if active:
            fill = C.SUCCESS_SOFT
            border = C.SUCCESS
            text = C.SUCCESS
            bold = True
        elif tone == "primary":
            fill = C.PRIMARY_DARK if pressed else (
                C.PRIMARY if hovered else C.PRIMARY)
            border = None
            text = gdi.argb(255, 255, 255)
            bold = True
        elif tone == "warning":
            fill = C.WARNING_SOFT if not hovered else gdi.argb(255, 255, 236, 205)
            border = C.WARNING
            text = C.WARNING
            bold = True
        else:
            fill = C.PRIMARY_SOFT if hovered else C.CARD
            border = C.BORDER
            text = C.TEXT
            bold = False
        offset = 1 if pressed else 0
        c.rounded(x + 1, y + 2, w - 2, h - 2, 9, C.SHADOW)
        c.rounded(x, y + offset, w, h - 2, 9, fill, border)
        c.text(button["label"], x + 10, y + offset, w - 20, h - 2, 13,
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

    def _draw_list_card(self, c, x, y, w, h, title, lines, row_h,
                        scroll, which) -> None:
        accent = C.PRIMARY if which == "notice" else C.SUCCESS
        c.rounded(x, y + 3, w, h, CARD_RADIUS, C.SHADOW)
        c.rounded(x, y, w, h, CARD_RADIUS, C.CARD, C.BORDER)
        c.rounded(x + 18, y + 13, 3, 20, 1, accent)
        c.text(title, x + 30, y + 10, w - 48, 28, 14.5, C.TEXT, True)
        c.rect(x + 18, y + 40, w - 36, 1, C.DIVIDER)
        list_y = y + 48
        list_h = h - 56
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
            for index in range(scroll, min(len(lines), scroll + visible + 1)):
                row_y = list_y + (index - scroll) * row_h
                line = lines[index]
                offscreen = line.startswith("[越界]")
                if offscreen:
                    c.rounded(x + 14, row_y + 4, 5, row_h - 8, 2, C.DANGER)
                    color = C.DANGER
                elif which == "notice" and " · " in line:
                    color = C.TEXT_SUB
                else:
                    color = C.TEXT
                c.text(line, x + 26, row_y, w - 42, row_h, 12, color)
        finally:
            c.reset_clip()

        if not lines:
            c.text("暂无内容", x + 22, list_y, w - 44, 32, 12,
                   C.TEXT_FAINT)

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
                if not buttons_only:
                    return (kind, kind, spot)
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
        elif self._in_rect(x, y, "tools"):
            self._left_scroll = max(0, self._left_scroll - delta)
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
                hit = self._hit_test(x, y)
                new_hover = hit[1] if hit else None
                if new_hover != self._hover:
                    self._hover = new_hover
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
                    elif kind == "settings":
                        self._show_settings_menu()
                    elif kind == "action":
                        threading.Thread(
                            target=self._run_action,
                            args=(spot["tool"], spot["action"]), daemon=True,
                        ).start()
                    elif kind == "min":
                        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
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

            if msg == win32con.WM_KEYDOWN and wparam == win32con.VK_ESCAPE:
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
