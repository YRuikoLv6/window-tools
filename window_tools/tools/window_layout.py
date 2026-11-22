"""窗口布局守护：双模显示器切换后自动还原窗口位置与大小。

设计要点：
1. 后台每 1.5 秒滚动采样窗口布局（含屏幕指纹），保留最近 60 条；
2. 收到 WM_DISPLAYCHANGE 后，取「屏幕指纹与当前不同」的最新快照作为切换前状态；
3. 还原时按显示器索引做比例缩放 + 工作区钳制，确保窗口始终可见可操作；
4. 提供手动保存/精确还原/越界救援作为兜底手段。
"""
from __future__ import annotations

import copy
import json
import threading
import time
from collections import deque
from typing import Optional

import win32con
import win32gui
import win32process

from ..core.base_tool import BaseTool, MenuItem, WebAction
from ..paths import LAYOUT_PATH
from ..utils import winapi
from ..utils.config import JsonStore

SAMPLE_INTERVAL = 1.5
HISTORY_LIMIT = 60
QUIET_AFTER_CHANGE = 6.0
PICK_TTL = 120.0


class WindowLayoutTool(BaseTool):
    name = "窗口布局守护"
    description = "显示器模式切换后恢复窗口位置，并提供越界窗口救援"
    version = "1.0.0"

    def __init__(self, app):
        super().__init__(app)
        self.store = JsonStore(LAYOUT_PATH, {
            "profile": None,
            "history": [],
        })
        self.history = deque(self.store.get("history", []) or [], maxlen=HISTORY_LIMIT)
        self.auto_enabled = bool(
            self.app.config.get("general", {}).get("auto_detect", True)
        )
        self._name_cache: dict = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_fp = self._fingerprint(self.history[-1]) if self.history else ""
        self._dirty = False
        self._save_hotkey_label = ""
        self._restore_hotkey_label = ""
        self._rescue_hotkey_label = ""

    # ------------------------------------------------------------ 生命周期 --
    def on_load(self) -> None:
        ca = win32con.MOD_CONTROL | win32con.MOD_ALT
        cs = win32con.MOD_CONTROL | win32con.MOD_SHIFT
        cas = ca | win32con.MOD_SHIFT
        self._save_hotkey_label = self._register_with_fallback(
            "layout.save", "S", self.save_manual,
            [(ca, "Ctrl+Alt+S"), (cs, "Ctrl+Shift+S")],
        )
        self._restore_hotkey_label = self._register_with_fallback(
            "layout.restore", "R", self.restore_auto,
            [(ca, "Ctrl+Alt+R"), (cs, "Ctrl+Shift+R")],
        )
        self._rescue_hotkey_label = self._register_with_fallback(
            "layout.rescue", "M", self.rescue_offscreen,
            [(ca, "Ctrl+Alt+M"), (cas, "Ctrl+Alt+Shift+M"), (cs, "Ctrl+Shift+M")],
        )
        self._thread = threading.Thread(
            target=self._sampler_loop, daemon=True, name="layout-sampler"
        )
        self._thread.start()
        self.log.info(
            "窗口布局守护已启动：自动检测=%s，历史快照=%d，快捷键 保存=%s 还原=%s 救援=%s",
            self.auto_enabled, len(self.history),
            self._save_hotkey_label or "未注册",
            self._restore_hotkey_label or "未注册",
            self._rescue_hotkey_label or "未注册",
        )

    def _register_with_fallback(self, key: str, letter: str, callback,
                                combos: list) -> str:
        """热门组合键常被占用，逐个尝试直到注册成功。"""
        for modifiers, label in combos:
            if self.app.register_hotkey(key, modifiers, ord(letter), callback):
                return label
        self.log.warning("所有候选热键均被占用: %s", key)
        return ""

    def on_unload(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        with self._lock:
            self.store.data["history"] = list(self.history)
        self.store.save()

    def on_display_change(self, signature: dict) -> None:
        if not self.auto_enabled:
            self.log.info("自动检测已关闭，跳过还原")
            return
        source = self._pick_prechange_snapshot(signature)
        if source is None:
            self.log.info("未找到切换前快照，跳过自动还原")
            return
        result = self._restore(source, adaptive=True)
        self.log.info("自动还原完成: %s", result)
        self.app.notify(
            "已恢复 {moved} 个窗口（离线 {missing}）".format(**result),
            "窗口布局已还原",
        )
        # 记录还原后的状态，作为下一次切换的基准
        self._record_snapshot(force=True)

    # -------------------------------------------------------------- 菜单 ----
    def get_menu_items(self) -> list:
        return [
            MenuItem("保存当前布局 (Ctrl+Alt+S)", self.save_manual),
            MenuItem("还原窗口布局 - 自动 (Ctrl+Alt+R)", self.restore_auto),
            MenuItem("精确还原到保存时像素", self.restore_exact),
            MenuItem("救援越界窗口 (Ctrl+Alt+M)", self.rescue_offscreen),
            MenuItem(
                "自动检测显示器切换", self.toggle_auto, checked=self.auto_enabled
            ),
        ]

    def get_status(self) -> str:
        with self._lock:
            count = len(self.history)
        has_profile = bool(self.store.get("profile"))
        return (
            f"快照 {count} 条 · 布局 {'已保存' if has_profile else '未保存'}"
            f" · 自动检测 {'开' if self.auto_enabled else '关'}"
        )

    # ------------------------------------------------------------ 用户操作 --
    def toggle_auto(self) -> None:
        self.auto_enabled = not self.auto_enabled
        self.app.config.data.setdefault("general", {})["auto_detect"] = self.auto_enabled
        if not self.app.config.save():
            self.log.warning("自动检测开关写入配置失败")
        self.app.notify(f"自动检测已{'开启' if self.auto_enabled else '关闭'}")

    def save_manual(self) -> None:
        snapshot = self._take_snapshot()
        with self._lock:
            self.store.data["profile"] = copy.deepcopy(snapshot)
            self.store.data["history"] = list(self.history)
            ok = self.store.save()
        count = len(snapshot["windows"])
        self.log.info("手动保存布局: %d 个窗口, 写入=%s", count, ok)
        self.app.notify(
            f"已保存 {count} 个窗口的位置与大小" if ok else "保存失败，请检查 data 目录权限",
            "保存布局",
        )

    def restore_auto(self) -> None:
        source = self._pick_prechange_snapshot(winapi.screen_signature())
        if source is None:
            self.app.notify("暂无可用快照，请先切换一次显示器模式")
            return
        result = self._restore(source, adaptive=True)
        self.app.notify(
            "已还原 {moved} 个窗口（离线 {missing}）".format(**result),
            "窗口布局已还原",
        )

    def restore_exact(self) -> None:
        with self._lock:
            source = copy.deepcopy(self.store.get("profile"))
        if not source:
            self.app.notify("请先使用「保存当前布局」保存一份布局")
            return
        result = self._restore(source, adaptive=False)
        self.app.notify(
            "已精确还原 {moved} 个窗口（离线 {missing}）".format(**result),
            "窗口布局已还原",
        )

    def rescue_offscreen(self) -> None:
        """紧急兜底：把所有大部分不可见的窗口拉回屏幕中央。"""
        virtual = winapi.bounds_to_rect(winapi.get_virtual_bounds())
        monitors = winapi.get_monitors()
        monitor_rects = [m["rect"] for m in monitors]
        moved = 0

        for window in winapi.enumerate_windows(
            self.app.hwnd, self._name_cache
        ):
            rect = window["rect"]
            if winapi.visible_ratio(rect, virtual) >= 0.35:
                continue
            index = winapi.monitor_index_for_rect(rect, monitor_rects)
            monitor = monitors[index % len(monitors)] if monitors else None
            bounds = (monitor.get("work") or monitor["rect"]) if monitor else virtual
            bw = max(1, bounds[2] - bounds[0])
            bh = max(1, bounds[3] - bounds[1])
            width = min(max(1, rect[2] - rect[0]), max(1, bw - 48))
            height = min(max(1, rect[3] - rect[1]), max(1, bh - 48))
            left = bounds[0] + (bw - width) // 2
            top = bounds[1] + (bh - height) // 2
            try:
                winapi.move_window(window["hwnd"], (left, top, left + width, top + height))
                moved += 1
            except Exception as exc:
                self.log.debug("救援窗口失败 %s: %s", window["title"], exc)

        self.log.info("越界救援完成: %d 个窗口", moved)
        self.app.notify(f"已拉回 {moved} 个越界窗口", "窗口救援")

    # ------------------------------------------------------------ 快照逻辑 --
    def _take_snapshot(self) -> dict:
        return {
            "ts": time.time(),
            "screen": winapi.screen_signature(),
            "monitors": [list(m["rect"]) for m in winapi.get_monitors()],
            "windows": winapi.enumerate_windows(self.app.hwnd, self._name_cache),
        }

    @staticmethod
    def _fingerprint(snapshot: dict) -> str:
        payload = {
            "screen": snapshot.get("screen"),
            "windows": [
                [w.get("hwnd"), w.get("class"), w.get("pid"), w.get("rect")]
                for w in snapshot.get("windows", [])
            ],
        }
        try:
            return json.dumps(payload, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""

    def _record_snapshot(self, force: bool = False) -> None:
        snapshot = self._take_snapshot()
        fingerprint = self._fingerprint(snapshot)
        with self._lock:
            if not force and self.history and fingerprint == self._last_fp:
                return
            self.history.append(snapshot)
            self._last_fp = fingerprint
            self.store.data["history"] = list(self.history)
            self._dirty = True

    def _sampler_loop(self) -> None:
        last_save = time.time()
        while not self._stop.wait(SAMPLE_INTERVAL):
            try:
                # 切换过程中不采样，避免把错乱状态写成基准
                if time.time() - self.app.last_display_change < QUIET_AFTER_CHANGE:
                    continue
                self._record_snapshot()
                now = time.time()
                with self._lock:
                    dirty = self._dirty
                if dirty and now - last_save >= 10:
                    self.store.save()
                    with self._lock:
                        self._dirty = False
                    last_save = now
            except Exception:
                self.log.exception("快照采样线程异常")

    def _pick_prechange_snapshot(self, current: dict) -> Optional[dict]:
        now = time.time()
        with self._lock:
            snapshots = list(self.history)
        for snapshot in reversed(snapshots):
            if (snapshot.get("screen") != current
                    and now - snapshot.get("ts", 0) <= PICK_TTL):
                return snapshot
        for snapshot in reversed(snapshots):
            if snapshot.get("screen") != current:
                return snapshot
        return snapshots[-1] if snapshots else None

    # ------------------------------------------------------------ 还原逻辑 --
    def _find_window(self, saved: dict) -> Optional[int]:
        """HWND 可能因应用重启而失效，用 类名+PID+进程名+标题 兜底匹配。"""
        hwnd = int(saved.get("hwnd", 0))
        if hwnd and win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd):
            try:
                cls = win32gui.GetClassName(hwnd)
                _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
                if cls == saved.get("class") and (
                    not saved.get("pid") or pid == saved.get("pid")
                ):
                    return hwnd
            except Exception:
                pass

        current = winapi.enumerate_windows(self.app.hwnd, self._name_cache)
        best, best_score = None, -1
        saved_cls = saved.get("class", "")
        saved_pid = saved.get("pid", 0)
        saved_proc = saved.get("process", "")
        saved_title = saved.get("title", "")
        for item in current:
            score = 0
            if item["class"] == saved_cls:
                score += 30
            if saved_pid and item["pid"] == saved_pid:
                score += 30
            if saved_proc and item["process"] == saved_proc:
                score += 20
            if saved_title and item["title"] == saved_title:
                score += 20
            if score > best_score:
                best, best_score = item, score
            if best_score >= 100:
                break
        return best["hwnd"] if best and best_score >= 50 else None

    def _restore(self, source: dict, adaptive: bool = True) -> dict:
        saved_monitors = source.get("monitors") or []
        current_monitors = winapi.get_monitors()
        virtual = winapi.bounds_to_rect(winapi.get_virtual_bounds())
        moved, missing, skipped = 0, 0, 0

        for saved in source.get("windows", []):
            hwnd = self._find_window(saved)
            if not hwnd or not win32gui.IsWindow(hwnd):
                missing += 1
                continue
            if win32gui.IsIconic(hwnd):
                skipped += 1
                continue
            rect = [int(v) for v in saved.get("rect", [])]
            if len(rect) != 4:
                continue

            target = winapi.clamp_rect(rect, virtual)
            if adaptive and saved_monitors:
                index = winapi.monitor_index_for_rect(rect, saved_monitors)
                sm = saved_monitors[index % len(saved_monitors)]
                cm = (
                    current_monitors[index % len(current_monitors)]
                    if current_monitors else None
                )
                if cm:
                    sl, st, sr, sb = sm
                    cl, ct, cr, cb = cm["rect"]
                    scale = (cr - cl) / max(1, sr - sl)
                    scale = min(max(scale, 0.35), 3.0)
                    rel_x = (rect[0] - sl) / max(1, sr - sl)
                    rel_y = (rect[1] - st) / max(1, sb - st)
                    width = max(160, int((rect[2] - rect[0]) * scale))
                    height = max(100, int((rect[3] - rect[1]) * scale))
                    left = int(cl + rel_x * (cr - cl))
                    top = int(ct + rel_y * (cb - ct))
                    bounds = cm.get("work") or cm["rect"]
                    target = winapi.clamp_rect(
                        (left, top, left + width, top + height), bounds
                    )

            try:
                winapi.move_window(hwnd, target)
                moved += 1
            except Exception as exc:
                self.log.debug("还原窗口失败 %s: %s", saved.get("title"), exc)

        return {"moved": moved, "missing": missing, "skipped": skipped}

    # ---------------------------------------------------------- 网页动作 --
    def get_web_actions(self) -> list:
        return [
            WebAction(id="save", label="保存当前布局",
                      hint="记录所有窗口的位置与大小；快捷键 "
                           + (self._save_hotkey_label or "未注册"), tone="primary"),
            WebAction(id="restore", label="自动还原布局",
                      hint="显示器切换后按比例还原窗口；快捷键 "
                           + (self._restore_hotkey_label or "未注册"), tone="primary"),
            WebAction(id="restore_exact", label="精确还原", hint="按保存时的原始像素还原",
                      tone="default"),
            WebAction(id="rescue", label="救援越界窗口",
                      hint="把跑出屏幕的窗口拉回可见区域；快捷键 "
                           + (self._rescue_hotkey_label or "未注册"),
                      tone="warning"),
            WebAction(id="toggle_auto", label="自动检测切换", kind="toggle",
                      hint="切换显示器模式后自动还原", active=self.auto_enabled,
                      tone="default"),
        ]

    def handle_action(self, action_id: str, payload: dict | None = None) -> dict:
        handlers = {
            "save": self.save_manual,
            "restore": self.restore_auto,
            "restore_exact": self.restore_exact,
            "rescue": self.rescue_offscreen,
            "toggle_auto": self.toggle_auto,
        }
        handler = handlers.get(action_id)
        if handler is None:
            return {"ok": False, "message": f"未知操作: {action_id}"}
        handler()
        return {"ok": True, "message": "操作已执行，详见右侧通知"}
