"""窗口工具集合 - 原生桌面窗体入口。

运行: python main.py
自检: python main.py --selftest
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import tempfile

from window_tools import __version__
from window_tools.core.desktop_app import DesktopApp
from window_tools.core.tool_manager import ToolManager
from window_tools.paths import CONFIG_PATH, LOG_DIR
from window_tools.tools.window_layout import WindowLayoutTool
from window_tools.utils import winapi
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

    ratio = winapi.visible_ratio((-500, 0, -100, 400), (0, 0, 1920, 1080))
    checks.append(("可见面积计算", ratio == 0.0, f"{ratio:.2f}"))

    with tempfile.TemporaryDirectory() as folder:
        store = JsonStore(os.path.join(folder, "test.json"), {"a": 1, "b": {"c": 2}})
        store.data["b"]["c"] = 9
        ok = store.save()
        reloaded = JsonStore(store.path, {"a": 0, "b": {"c": 0}})
        checks.append(("配置持久化", ok and reloaded.get("b", {}).get("c") == 9, "OK"))

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
    args = parser.parse_args()

    log = setup_logger(LOG_DIR)
    config = JsonStore(CONFIG_PATH, {
        "general": {"auto_detect": True},
    })

    if args.selftest:
        return selftest(log)

    if not acquire_single_instance():
        log.info("已有实例在运行，直接退出")
        return 0

    log.info("窗口工具集合 v%s 启动中", __version__)
    app = DesktopApp(log, config)
    manager = ToolManager(app)
    manager.register(WindowLayoutTool)
    app.set_manager(manager)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
