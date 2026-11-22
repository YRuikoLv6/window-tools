"""鼠标屏幕锁定：把光标限制在当前显示器，防止误入其他屏幕。

实现要点：
1. 启用时按光标位置定位显示器，用 ClipCursor 把光标限制在该显示器矩形内；
2. 单一后台线程独占 ClipCursor 调用，定期校验并在被其他程序改写时恢复；
3. 显示器模式切换后自动重新定位并重新锁定；
4. 提供界面按钮与全局热键（切换 / 强制解锁），退出时一定释放光标。
"""
from __future__ import annotations

import atexit
import ctypes
import threading
from typing import Optional, Tuple

import win32api
import win32con

from ..core.base_tool import BaseTool, MenuItem, WebAction
from ..utils import winapi

Rect = Tuple[int, int, int, int]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


_user32 = ctypes.windll.user32
_user32.ClipCursor.argtypes = [ctypes.POINTER(_RECT)]
_user32.ClipCursor.restype = ctypes.c_int
_user32.GetClipCursor.argtypes = [ctypes.POINTER(_RECT)]
_user32.GetClipCursor.restype = ctypes.c_int


class MouseLockTool(BaseTool):
    name = "鼠标屏幕锁定"
    description = "把鼠标限制在当前显示器，防止误入其他屏幕"
    version = "1.0.0"
    CHECK_INTERVAL = 0.4

    def __init__(self, app):
        super().__init__(app)
        self._guard = threading.RLock()
        self.enabled = bool(
            self.app.config.get("mouse", {}).get("enabled", False)
        )
        self._monitor_index = -1
        self._target_rect: Optional[Rect] = None
        self._toggle_hotkey_label = ""
        self._unlock_hotkey_label = ""
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._clip_active = False

    # ------------------------------------------------------------ 生命周期 --
    def on_load(self) -> None:
        # 进程正常退出路径（含未捕获异常）兜底释放裁剪区
        atexit.register(self._emergency_release)
        ca = win32con.MOD_CONTROL | win32con.MOD_ALT
        cs = win32con.MOD_CONTROL | win32con.MOD_SHIFT
        cas = ca | win32con.MOD_SHIFT
        self._toggle_hotkey_label = self._register_with_fallback(
            "mouse.toggle", "L", self.toggle,
            [(ca, "Ctrl+Alt+L"), (cs, "Ctrl+Shift+L"),
             (cas, "Ctrl+Alt+Shift+L")],
        )
        self._unlock_hotkey_label = self._register_with_fallback(
            "mouse.unlock", "U", self.unlock,
            [(ca, "Ctrl+Alt+U"), (cs, "Ctrl+Shift+U"),
             (cas, "Ctrl+Alt+Shift+U")],
        )
        self._thread = threading.Thread(
            target=self._manager_loop, daemon=True, name="mouse-lock",
        )
        self._thread.start()
        if self.enabled:
            self._wake.set()
            self.app.notify(
                "已按上次设置恢复鼠标锁定，可用热键解锁", self.name
            )
        self.log.info(
            "鼠标屏幕锁定已启动：状态=%s，切换=%s，解锁=%s",
            "已锁定" if self.enabled else "未锁定",
            self._toggle_hotkey_label or "未注册",
            self._unlock_hotkey_label or "未注册",
        )

    def _register_with_fallback(self, key: str, letter: str, callback,
                                combos: list) -> str:
        for modifiers, label in combos:
            if self.app.register_hotkey(key, modifiers, ord(letter), callback):
                return label
        self.log.warning("所有候选热键均被占用: %s", key)
        return ""

    def on_unload(self) -> None:
        """退出时释放光标，并写回关闭状态，避免下次启动又自动锁定。"""
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._force_release()

    def _force_release(self) -> None:
        """释放裁剪区并把启用状态写回为关闭（幂等）。"""
        with self._guard:
            was_enabled = self.enabled
            self.enabled = False
            self._target_rect = None
        self._apply_clip(None)
        self._clip_active = False
        # 关键：退出即视为解锁，必须持久化，否则下次启动会自动重新锁定
        if was_enabled or self.app.config.get("mouse", {}).get("enabled", False):
            self.app.config.data.setdefault("mouse", {})["enabled"] = False
            self.app.config.save()
        self.log.info("程序退出，鼠标锁定已释放")

    def _emergency_release(self) -> None:
        """进程任意退出路径（含异常/强制关闭）的兜底释放。
        atexit 不覆盖 os._exit 与进程被 kill，那两种情况由 Windows
        在线程终止时自动释放 ClipCursor，不会把鼠标卡死。"""
        try:
            self._force_release()
        except Exception:
            try:
                self._apply_clip(None)
            except Exception:
                pass

    def on_display_change(self, signature: dict) -> None:
        """显示器开关/增减后自动解锁，避免光标被锁在已失效的屏幕上。"""
        with self._guard:
            was_enabled = self.enabled
        if not was_enabled:
            return
        self.disable(notify=False)
        self.app.notify("检测到显示器变化，已自动解锁鼠标", self.name)
        self.log.info("显示器变化，鼠标已自动解锁")

    # ------------------------------------------------------------ 用户操作 --
    def toggle(self) -> None:
        with self._guard:
            should_enable = not self.enabled
        if should_enable:
            self.enable()
        else:
            self.disable()

    def enable(self, persist: bool = True, notify: bool = True) -> None:
        rect = self._resolve_rect(force=True)
        if not rect:
            if notify:
                self.app.notify("未检测到可用显示器，锁定失败", self.name)
            return
        with self._guard:
            was_enabled = self.enabled
            self.enabled = True
        self._wake.set()
        if persist:
            self._persist()
        if notify:
            suffix = "" if was_enabled else f"（显示器 {self._monitor_index + 1}）"
            hint = self._toggle_hotkey_label or self._unlock_hotkey_label
            if hint:
                suffix += f"，按 {hint} 可解锁"
            self.app.notify("鼠标已锁定到当前屏幕" + suffix, self.name)

    def disable(self, persist: bool = True, notify: bool = True) -> None:
        with self._guard:
            was_enabled = self.enabled
            self.enabled = False
            self._target_rect = None
        self._wake.set()
        if persist:
            self._persist()
        if notify and was_enabled:
            self.app.notify("鼠标已解锁，可自由移动到其他屏幕", self.name)

    def unlock(self) -> None:
        """强制解锁：即使当前状态显示未锁定，也确保 ClipCursor 被释放。"""
        self.disable()

    def _persist(self) -> None:
        self.app.config.data.setdefault("mouse", {})["enabled"] = self.enabled
        if not self.app.config.save():
            self.log.warning("鼠标锁定状态写入配置失败")

    # ------------------------------------------------------------ 几何计算 --
    @staticmethod
    def _locate_monitor(x: int, y: int, monitors: list) -> Tuple[Optional[int], Optional[dict]]:
        """定位光标所在显示器；不在任何显示器内时退化为最近的显示器。"""
        best = None
        for index, monitor in enumerate(monitors):
            left, top, right, bottom = monitor["rect"]
            if left <= x < right and top <= y < bottom:
                return index, monitor
            cx = (left + right) / 2
            cy = (top + bottom) / 2
            distance = (x - cx) ** 2 + (y - cy) ** 2
            if best is None or distance < best[0]:
                best = (distance, index, monitor)
        if best is None:
            return None, None
        return best[1], best[2]

    def _resolve_rect(self, force: bool = False) -> Optional[Rect]:
        with self._guard:
            if not force and self._target_rect:
                return self._target_rect
        monitors = winapi.get_monitors()
        if not monitors:
            return None
        x, y = win32api.GetCursorPos()
        index, monitor = self._locate_monitor(x, y, monitors)
        if monitor is None:
            return None
        rect = tuple(int(v) for v in monitor["rect"])
        with self._guard:
            self._monitor_index = index if index is not None else -1
            self._target_rect = rect
        return rect

    # ------------------------------------------------------------ 光标裁剪 --
    @staticmethod
    def _apply_clip(rect: Optional[Rect]) -> None:
        if rect is None:
            _user32.ClipCursor(None)
            return
        value = _RECT(*[int(v) for v in rect])
        _user32.ClipCursor(ctypes.byref(value))

    @staticmethod
    def _current_clip() -> Optional[Rect]:
        value = _RECT()
        if _user32.GetClipCursor(ctypes.byref(value)):
            return (value.left, value.top, value.right, value.bottom)
        return None

    def _manager_loop(self) -> None:
        try:
            while not self._stop.is_set():
                self._wake.wait(self.CHECK_INTERVAL)
                self._wake.clear()
                if self._stop.is_set():
                    break
                with self._guard:
                    enabled = self.enabled
                if not enabled:
                    if self._clip_active:
                        self._apply_clip(None)
                        self._clip_active = False
                    continue
                rect = self._resolve_rect()
                if not rect:
                    continue
                if self._current_clip() != rect:
                    self._apply_clip(rect)
                    self._clip_active = True
        except Exception:
            self.log.exception("鼠标锁定维护线程异常")
        finally:
            self._apply_clip(None)
            self._clip_active = False

    # ------------------------------------------------------------ UI 元数据 --
    def get_menu_items(self) -> list:
        return [
            MenuItem("锁定鼠标到当前屏幕", self.enable,
                     checked=self.enabled),
            MenuItem("解锁鼠标", self.unlock, enabled=self.enabled),
        ]

    def get_status(self) -> str:
        with self._guard:
            enabled = self.enabled
            index = self._monitor_index
        state = "已锁定" if enabled else "未锁定"
        target = f"屏幕 {index + 1}" if index >= 0 else "-"
        switch = self._toggle_hotkey_label or "未注册"
        unlock = self._unlock_hotkey_label or switch
        return f"鼠标 {state} · 目标 {target} · 切换 {switch} / 解锁 {unlock}"

    def get_web_actions(self) -> list:
        hint = f"切换 {self._toggle_hotkey_label or '未注册'}"
        if self._unlock_hotkey_label:
            hint += f"；强制解锁 {self._unlock_hotkey_label}"
        return [
            WebAction(
                id="toggle_lock", label="锁定鼠标到当前屏幕", kind="toggle",
                hint=hint, active=self.enabled, tone="primary",
            ),
            WebAction(
                id="unlock", label="立即解锁鼠标",
                hint="恢复鼠标自由移动到所有屏幕", tone="warning",
            ),
        ]

    def handle_action(self, action_id: str, payload: dict | None = None) -> dict:
        handlers = {
            "toggle_lock": self.toggle,
            "unlock": self.unlock,
        }
        handler = handlers.get(action_id)
        if handler is None:
            return {"ok": False, "message": f"未知操作: {action_id}"}
        handler()
        return {"ok": True, "message": "操作已执行，详见右侧通知"}
