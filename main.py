"""窗口工具集合 - 原生桌面窗体入口。

运行: python main.py
自检: python main.py --selftest
"""
from __future__ import annotations

import os
import sys

# 必须在导入 paths 之前执行。paths 被导入时会在 exe 旁边创建 data 目录。
if __name__ == "__main__":
    from window_tools.utils.relocate import relocate_frozen_exe
    relocate_frozen_exe()

import argparse
import ctypes
import struct
import tempfile

import win32con

from window_tools import __version__
from window_tools.core.desktop_app import DesktopApp, wake_existing_window
from window_tools.core.tool_manager import ToolManager
from window_tools.paths import CONFIG_PATH, ICON_PATH, LOG_DIR
from window_tools.tools.mouse_lock import MouseLockTool
from window_tools.tools.system_info import SystemInfoTool, collect_system_info
from window_tools.tools.window_layout import WindowLayoutTool
from window_tools.utils import winapi
from window_tools.utils.relocate import (
    directory_needs_own_folder,
    pick_app_folder,
    remove_original_exe,
)
from window_tools.utils.config import JsonStore
from window_tools.utils.logger import setup_logger


_instance_mutex = None


def acquire_single_instance() -> bool:
    """命名互斥锁防止双开；返回 False 表示已有实例在运行。"""
    global _instance_mutex
    kernel32 = ctypes.windll.kernel32
    kernel32.GetLastError.restype = ctypes.c_uint32
    handle = kernel32.CreateMutexW(None, False, "Local\\WindowToolsSingleInstance")
    if not handle:
        return True
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _instance_mutex = handle  # 保持句柄，避免被 GC 释放
    return True


def selftest(log) -> int:
    checks = []

    dpi = winapi.enable_dpi_awareness()
    checks.append(("DPI 感知", dpi != "UNAWARE", dpi))

    signature = winapi.screen_signature()
    checks.append(("屏幕指纹", signature["virtual"][2] > 0, str(signature["virtual"])))
    checks.append(("显示器枚举", len(signature["monitors"]) >= 1,
                   f"{len(signature['monitors'])} 台"))

    windows = winapi.enumerate_windows()
    checks.append(("窗口枚举", isinstance(windows, list), f"{len(windows)} 个窗口"))

    clamped = winapi.clamp_rect((-100, -50, 200, 200), (0, 0, 800, 600), 8)
    checks.append(("越界钳制", clamped[0] >= 8 and clamped[1] >= 8, str(clamped)))

    from window_tools.utils.hotkeys import MOD_ALT, MOD_CONTROL, format_hotkey
    label = format_hotkey(MOD_CONTROL | MOD_ALT, ord("S"))
    checks.append(("快捷键文字", label == "Ctrl+Alt+S", label))

    ratio = winapi.visible_ratio((-500, 0, -100, 400), (0, 0, 1920, 1080))
    checks.append(("可见面积计算", ratio == 0.0, f"{ratio:.2f}"))
    partial = winapi.is_offscreen((830, 189, 2013, 857), (0, 0, 1920, 1080))
    maximized = winapi.is_offscreen((-8, -8, 1928, 1040), (0, 0, 1920, 1080))
    inside = winapi.is_offscreen((100, 100, 900, 700), (0, 0, 1920, 1080))
    checks.append(("越界判定", partial and not maximized and not inside,
                   f"partial={partial} max={maximized} in={inside}"))

    client_hit = DesktopApp._nonclient_hit(400, 180, 800, 600)
    checks.append(("客户区鼠标命中", client_hit == win32con.HTCLIENT,
                   f"HT={client_hit}"))
    corner_hit = DesktopApp._nonclient_hit(2, 2, 800, 600)
    checks.append(("边缘缩放命中", corner_hit == win32con.HTTOPLEFT,
                   f"HT={corner_hit}"))

    monitors = [{"rect": (0, 0, 1920, 1080)},
                {"rect": (1920, 0, 3840, 1080)}]
    monitor_index, _ = MouseLockTool._locate_monitor(2000, 500, monitors)
    checks.append(("鼠标屏幕定位", monitor_index == 1,
                   f"index={monitor_index}"))

    class _MouseLockAppStub:
        def __init__(self, store):
            self.log = log
            self.config = store
            self.notices = []

        def notify(self, message, title=None):
            self.notices.append(message)

    with tempfile.TemporaryDirectory() as folder:
        store = JsonStore(os.path.join(folder, "test.json"), {"a": 1, "b": {"c": 2}})
        store.data["b"]["c"] = 9
        ok = store.save()
        reloaded = JsonStore(store.path, {"a": 0, "b": {"c": 0}})
        checks.append(("配置持久化", ok and reloaded.get("b", {}).get("c") == 9, "OK"))

        lock_store = JsonStore(
            os.path.join(folder, "mouse.json"), {"mouse": {"enabled": True}}
        )
        lock_app = _MouseLockAppStub(lock_store)
        lock_tool = MouseLockTool(lock_app)
        lock_tool.on_display_change({})
        auto_unlocked = (
            not lock_tool.enabled
            and lock_store.get("mouse", {}).get("enabled") is False
            and bool(lock_app.notices)
        )
        checks.append(("显示器变化自动解锁", auto_unlocked, "OK"))
        lock_tool.on_unload()

        exit_store = JsonStore(
            os.path.join(folder, "exit.json"), {"mouse": {"enabled": True}}
        )
        exit_app = _MouseLockAppStub(exit_store)
        exit_tool = MouseLockTool(exit_app)
        exit_tool.on_unload()
        exit_unlocked = (
            not exit_tool.enabled
            and exit_store.get("mouse", {}).get("enabled") is False
        )
        checks.append(("退出自动解锁", exit_unlocked, "OK"))

    icon_ok = os.path.isfile(ICON_PATH) and open(ICON_PATH, "rb").read(6) == \
        struct.pack("<HHH", 0, 1, 7)
    checks.append(("应用图标资源", icon_ok, os.path.basename(ICON_PATH)))

    # 桌面快捷方式：写入临时目录验证 .lnk 生成与启动参数
    from window_tools.utils import shortcut as sc_module
    spec = sc_module.launch_spec()
    spec_ok = bool(spec.get("target")) and os.path.isabs(spec["target"])
    checks.append(("快捷方式启动参数", spec_ok,
                   os.path.basename(spec.get("target", ""))))
    with tempfile.TemporaryDirectory() as folder:
        lnk = os.path.join(folder, "test.lnk")
        made = sc_module.create_shortcut(lnk)
        valid = (made.get("ok") and os.path.isfile(lnk)
                 and open(lnk, "rb").read(4) == b"\x4c\x00\x00\x00"
                 and sc_module.shortcut_is_current(lnk))
        checks.append(("快捷方式生成", bool(valid), "OK"))

    # 关闭确认框：出现时屏蔽背景点击，消失后恢复普通热点
    probe = DesktopApp.__new__(DesktopApp)
    probe._dialog = {"hotspots": []}
    probe._hotspots = [
        {"kind": "action", "rect": (0, 0, 20, 20),
         "tool": "t", "action": "a"},
        {"kind": "dlg_hide", "rect": (30, 0, 20, 20)},
    ]
    dialog_hit = probe._hit_test(40, 10)
    blocked_hit = probe._hit_test(10, 10)
    probe._dialog = None
    normal_hit = probe._hit_test(10, 10)
    dialog_ok = (dialog_hit is not None and dialog_hit[0] == "dlg_hide"
                 and blocked_hit is None and normal_hit is not None)
    checks.append(("关闭确认模态命中", dialog_ok, "OK"))

    # 托盘“退出程序”：带直通标志时关闭请求不弹确认框
    probe.hwnd = 0
    probe.log = log
    probe._dialog = {"hotspots": []}
    probe._force_exit = True
    probe._wnd_proc(0, win32con.WM_CLOSE, 0, 0)
    force_exit_ok = probe._dialog is None and probe._force_exit is False
    checks.append(("托盘退出直通", force_exit_ok, "OK"))

    # 单实例唤醒：类名固定且不随 PID 变化，唤醒消息不与托盘消息冲突
    from window_tools.core import desktop_app as app_module
    from window_tools.ui import tray as tray_module
    from window_tools.ui.tray import WM_TRAYICON
    wake_protocol_ok = (
        app_module.WINDOW_CLASS == "WindowToolsDesktop"
        and "{" not in app_module.WINDOW_CLASS
        and app_module.WM_SHOWME != WM_TRAYICON
        and app_module.WINDOW_TITLE.endswith(f"v{__version__}")
    )
    checks.append(("单实例唤醒协议", wake_protocol_ok, app_module.WINDOW_CLASS))

    # 托盘救援：命令 ID 不与现有项冲突，且几何钳制能把越界矩形拉回工作区
    tray_ids = [tray_module.TRAY_SHOW, tray_module.TRAY_HIDE,
                tray_module.TRAY_EXIT, tray_module.TRAY_RESCUE]
    rescue_wired = (
        len(set(tray_ids)) == len(tray_ids)
        and WindowLayoutTool._clamp_to_monitor(
            (830, 189, 2013, 857), (0, 0, 1920, 1080),
            [(0, 0, 1920, 1080)], [{"rect": (0, 0, 1920, 1080),
                                    "work": (0, 0, 1920, 1040)}],
        )[2] <= 1920
    )
    checks.append(("托盘救援接线", rescue_wired, f"TRAY_RESCUE={tray_module.TRAY_RESCUE}"))

    snapshot = collect_system_info()
    titles = [section["title"] for section in snapshot["sections"]]
    labels = {
        row.get("label")
        for section in snapshot["sections"]
        for row in section["rows"]
    }
    model = ""
    for section in snapshot["sections"]:
        for row in section["rows"]:
            if row.get("label") == "电脑型号":
                model = row.get("value") or ""
    info_ok = (
        titles == ["系统", "硬件", "网络"]
        and "外网 IP" in labels
        and bool(model)
        and model != "未读取到"
    )
    checks.append(("本机信息", info_ok, model or "空"))

    with tempfile.TemporaryDirectory() as folder:
        exe_name = "WindowTools.exe"
        open(os.path.join(folder, exe_name), "wb").close()
        alone = not directory_needs_own_folder(folder, exe_name)
        open(os.path.join(folder, "说明.txt"), "w", encoding="utf-8").close()
        crowded = directory_needs_own_folder(folder, exe_name)
        os.makedirs(os.path.join(folder, "data"))
        used = not directory_needs_own_folder(folder, exe_name)
        picked = os.path.basename(pick_app_folder(folder))
        relocate_ok = alone and crowded and used and picked == "窗口工具集合"
        old_exe = os.path.join(folder, exe_name)
        current_exe = os.path.join(folder, "窗口工具集合", exe_name)
        removed = remove_original_exe(old_exe, current_exe)
        note = os.path.join(folder, "说明.txt")
        kept_note = os.path.isfile(note)
        refused = not remove_original_exe(note, current_exe)
        relocate_ok = relocate_ok and removed and kept_note and refused
        relocate_ok = relocate_ok and not os.path.exists(old_exe)
        checks.append(("首次启动独立文件夹", relocate_ok, picked))

    # GDI+ 文字测量：GdipMeasureString 参数声明不完整会导致启动绘制时崩溃
    try:
        import win32gui
        from window_tools.ui import gdi
        gdi.startup()
        screen = win32gui.GetDC(0)
        memdc = win32gui.CreateCompatibleDC(screen)
        bitmap = win32gui.CreateCompatibleBitmap(screen, 64, 64)
        old = win32gui.SelectObject(memdc, bitmap)
        try:
            with gdi.Canvas(memdc, 64, 64, 1.0) as canvas:
                measured = canvas.measure("0", 11)
        finally:
            win32gui.SelectObject(memdc, old)
            win32gui.DeleteObject(bitmap)
            win32gui.DeleteDC(memdc)
            win32gui.ReleaseDC(0, screen)
        checks.append(("GDI 文字测量", measured > 0, f"{measured:.1f}px"))
    except Exception as exc:
        checks.append(("GDI 文字测量", False, str(exc)))

    failed = [name for name, passed, _ in checks if not passed]
    for name, passed, detail in checks:
        log.info("%s %s - %s", "PASS" if passed else "FAIL", name, detail)
    summary = f"自检: {len(checks) - len(failed)}/{len(checks)} 通过"
    log.info(summary)
    try:
        print(summary)  # 冻结版无控制台时 stdout 可能为 None
    except Exception:
        pass
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="窗口工具集合（原生桌面窗体）")
    parser.add_argument("--selftest", action="store_true", help="运行核心自检后退出")
    parser.add_argument(
        "--relocated", nargs="?", const="", default=None, help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    log = setup_logger(LOG_DIR)
    config = JsonStore(CONFIG_PATH, {
        "general": {"auto_detect": True},
        "mouse": {"enabled": False},
        "shortcut": {"asked": False, "remember": False, "create": False},
        "hotkeys": {},
    })

    if args.selftest:
        return selftest(log)

    if not acquire_single_instance():
        # 隐藏到托盘后再次启动：唤醒已有窗口，而不是静默退出
        if wake_existing_window():
            log.info("已有实例在运行，已通知其显示窗口")
        else:
            log.info("已有实例在运行，但未找到其窗口")
        return 0

    log.info("窗口工具集合 v%s 启动中", __version__)
    app = DesktopApp(log, config)
    if args.relocated is not None:
        folder = os.path.dirname(os.path.abspath(sys.executable))
        removed = remove_original_exe(args.relocated, sys.executable)
        if removed:
            message = "程序已移到 %s，原来的文件已删除" % folder
        else:
            message = "程序已移到 %s。原来的文件还在，请手动删除：%s" % (
                folder, args.relocated or "未知位置",
            )
        log.info(message)
        app.notify(message, "程序已移动")
        app.prepare_relocate_notice(folder, args.relocated or "", removed)
    manager = ToolManager(app)
    manager.register(WindowLayoutTool)
    manager.register(MouseLockTool)
    manager.register(SystemInfoTool)
    app.set_manager(manager)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
