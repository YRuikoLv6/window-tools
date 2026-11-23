"""Windows API 封装：DPI 感知、显示器/窗口枚举、几何计算。

所有坐标均为物理像素（本进程设为 Per-Monitor DPI Aware），
避免 4K/1K 切换时逻辑像素与物理像素混用导致定位错误。
"""
from __future__ import annotations

import ctypes
from typing import Dict, List, Optional, Sequence, Tuple

import win32api
import win32con
import win32gui
import win32process

Rect = Tuple[int, int, int, int]          # (left, top, right, bottom)
Bounds = Tuple[int, int, int, int]        # (x, y, width, height)

_PRIMARY_FLAG = getattr(win32con, "MONITORINFOF_PRIMARY", 1)


# ---------------------------------------------------------------- DPI -----
def enable_dpi_awareness() -> str:
    """进程启动后尽早调用，返回实际生效的 DPI 模式。"""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return "PER_MONITOR_AWARE_V2"
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "PER_MONITOR_AWARE"
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "SYSTEM_DPI_AWARE"
    except Exception:
        return "UNAWARE"


def get_system_dpi() -> Tuple[int, int]:
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
        if dpi > 0:
            return dpi, dpi
    except Exception:
        pass
    try:
        hdc = win32gui.GetDC(0)
        x = win32gui.GetDeviceCaps(hdc, win32con.LOGPIXELSX)
        y = win32gui.GetDeviceCaps(hdc, win32con.LOGPIXELSY)
        win32gui.ReleaseDC(0, hdc)
        return int(x), int(y)
    except Exception:
        return 96, 96


# ------------------------------------------------------------- screens ----
def get_virtual_bounds() -> Bounds:
    return (
        int(win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)),
        int(win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)),
        int(win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)),
        int(win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)),
    )


def bounds_to_rect(bounds: Bounds) -> Rect:
    x, y, w, h = bounds
    return x, y, x + w, y + h


def rect_from_bounds(bounds: Sequence[int]) -> Rect:
    return bounds_to_rect(tuple(bounds))


def get_monitors() -> List[dict]:
    """按「主屏优先 → 上到下 → 左到右」稳定排序的显示器列表。"""
    items: List[dict] = []
    for hmonitor, _hdc, raw_rect in win32api.EnumDisplayMonitors(None, None):
        try:
            info = win32api.GetMonitorInfo(hmonitor)
        except Exception:
            continue
        l, t, r, b = (int(v) for v in raw_rect)
        flags = int(info.get("Flags", 0) or 0)
        primary = bool(flags & _PRIMARY_FLAG) or (l <= 0 <= r and t <= 0 <= b)
        work = tuple(int(v) for v in info.get("Work", raw_rect))
        items.append({
            "handle": hmonitor,
            "rect": (l, t, r, b),
            "work": work,
            "primary": primary,
            "order": (0 if primary else 1, t, l),
        })
    items.sort(key=lambda m: m["order"])
    return items


def screen_signature() -> dict:
    """用于判断「显示器模式是否发生变化」的屏幕指纹。"""
    vx, vy, vw, vh = get_virtual_bounds()
    dx, dy = get_system_dpi()
    return {
        "virtual": [vx, vy, vw, vh],
        "monitors": [
            [m["rect"][0], m["rect"][1], m["rect"][2], m["rect"][3]]
            for m in get_monitors()
        ],
        "dpi": [dx, dy],
        "count": int(win32api.GetSystemMetrics(win32con.SM_CMONITORS)),
    }


# ------------------------------------------------------------ windows -----
def _process_name(pid: int, cache: Dict[int, str]) -> str:
    if pid in cache:
        return cache[pid]
    name = ""
    handle = None
    try:
        handle = win32process.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            path = win32api.QueryFullProcessImageName(handle, 0)
            name = str(path).replace("/", "\\").split("\\")[-1]
    except Exception:
        pass
    finally:
        if handle:
            try:
                win32process.CloseHandle(handle)
            except Exception:
                pass
    cache[pid] = name
    return name


def window_identity(window: dict) -> Tuple[str, int, str]:
    return window.get("class", ""), window.get("pid", 0), window.get("process", "")


def enumerate_windows(
    ignored_hwnd: int = 0, name_cache: Optional[Dict[int, str]] = None
) -> List[dict]:
    """枚举可操作的顶层窗口。跳过最小化、无标题、工具窗口和自身宿主窗口。"""
    results: List[dict] = []
    cache: Dict[int, str] = name_cache if name_cache is not None else {}

    def callback(hwnd, _param):
        hwnd = int(hwnd)
        if hwnd == ignored_hwnd:
            return True
        try:
            if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
                return True
            if win32gui.IsIconic(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd) or ""
            if not title.strip():
                return True
            try:
                ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            except Exception:
                ex_style = 0
            if ex_style & win32con.WS_EX_TOOLWINDOW:
                return True
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            return True

        l, t, r, b = (int(v) for v in rect)
        if r - l < 80 or b - t < 50:
            return True

        try:
            cls = win32gui.GetClassName(hwnd)
        except Exception:
            cls = ""
        try:
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            pid = int(pid)
        except Exception:
            pid = 0

        results.append({
            "hwnd": hwnd,
            "class": cls,
            "title": title,
            "pid": pid,
            "process": _process_name(pid, cache),
            "rect": [l, t, r, b],
        })
        return True

    win32gui.EnumWindows(callback, None)
    return results


def move_window(hwnd: int, rect: Sequence[int]) -> None:
    l, t, r, b = (int(v) for v in rect)
    win32gui.SetWindowPos(
        hwnd,
        0,
        l, t, max(1, r - l), max(1, b - t),
        win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
    )


# ----------------------------------------------------------- geometry -----
def rect_center(rect: Sequence[int]) -> Tuple[float, float]:
    l, t, r, b = rect
    return (l + r) / 2.0, (t + b) / 2.0


def clamp_rect(rect: Sequence[int], bounds: Sequence[int], margin: int = 8) -> Rect:
    """把窗口矩形钳制到指定区域内，必要时缩小窗口。"""
    l, t, r, b = (int(v) for v in rect)
    bl, bt, br, bb = (int(v) for v in bounds)
    avail_w = max(1, br - bl - 2 * margin)
    avail_h = max(1, bb - bt - 2 * margin)
    w = max(1, min(r - l, avail_w))
    h = max(1, min(b - t, avail_h))
    nl = max(bl + margin, min(l, br - margin - w))
    nt = max(bt + margin, min(t, bb - margin - h))
    return nl, nt, nl + w, nt + h


def monitor_index_for_rect(rect: Sequence[int], monitors: Sequence[Sequence[int]]) -> int:
    """找到矩形中心所在显示器；不在任何显示器内时取中心距离最近的。"""
    if not monitors:
        return 0
    cx, cy = rect_center(rect)
    for i, m in enumerate(monitors):
        l, t, r, b = m
        if l <= cx <= r and t <= cy <= b:
            return i
    best_i, best_d = 0, float("inf")
    for i, m in enumerate(monitors):
        l, t, r, b = m
        d = (cx - (l + r) / 2.0) ** 2 + (cy - (t + b) / 2.0) ** 2
        if d < best_d:
            best_i, best_d = i, d
    return best_i


def visible_ratio(rect: Sequence[int], bounds: Sequence[int]) -> float:
    """窗口在指定区域内的可见面积占比。"""
    l, t, r, b = rect
    bl, bt, br, bb = bounds
    iw = max(0, min(r, br) - max(l, bl))
    ih = max(0, min(b, bb) - max(t, bt))
    area = max(1, (r - l) * (b - t))
    return (iw * ih) / area


def is_offscreen(rect: Sequence[int], bounds: Sequence[int],
                 margin: int = 16) -> bool:
    """窗口是否伸出可见区域。

    只忽略十几像素的边框溢出，避免把最大化窗口当成越界。
    原先用可见面积低于 35% 才算越界：单显示器上窗口往往还露着一大块，
    但右侧或底部已经到了屏幕外，救援会被直接跳过。
    """
    l, t, r, b = (int(v) for v in rect)
    bl, bt, br, bb = (int(v) for v in bounds)
    if r - l <= 1 or b - t <= 1:
        return False
    return (
        l < bl - margin or t < bt - margin
        or r > br + margin or b > bb + margin
    )


def is_zoomed(hwnd: int) -> bool:
    try:
        return bool(ctypes.windll.user32.IsZoomed(int(hwnd)))
    except Exception:
        return False
