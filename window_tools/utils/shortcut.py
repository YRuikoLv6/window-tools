"""桌面快捷方式工具：根据程序当前运行路径生成 .lnk。

- 冻结 exe：直接指向 sys.executable，工作目录为 exe 所在目录，图标用 exe 自身。
- 源码运行：指向 pythonw.exe，参数为 main.py 绝对路径，避免黑色控制台窗口。
- 通过 pywin32 的 WScript.Shell 创建快捷方式（项目已依赖 pywin32）。
"""
from __future__ import annotations

import ctypes
import os
import sys

from ..paths import FROZEN, ICON_PATH, PROJECT_DIR

SHORTCUT_NAME = "窗口工具集合.lnk"
CSIDL_DESKTOPDIRECTORY = 0x0010
SHGFP_TYPE_CURRENT = 0


def desktop_path() -> str:
    """获取真实桌面路径（兼容重定向/中文用户名）。"""
    buffer = ctypes.create_unicode_buffer(260)
    try:
        shell32 = ctypes.windll.shell32
        shell32.SHGetFolderPathW.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
            ctypes.c_int, ctypes.c_wchar_p,
        ]
        shell32.SHGetFolderPathW.restype = ctypes.c_long
        result = shell32.SHGetFolderPathW(
            None, CSIDL_DESKTOPDIRECTORY, None, SHGFP_TYPE_CURRENT, buffer)
        if result == 0 and buffer.value:
            return buffer.value
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def shortcut_path() -> str:
    return os.path.join(desktop_path(), SHORTCUT_NAME)


def shortcut_exists() -> bool:
    return os.path.isfile(shortcut_path())


def _same_path(left: str, right: str) -> bool:
    left = (left or "").strip().strip('"')
    right = (right or "").strip().strip('"')
    if not left or not right:
        return False
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(
        os.path.normpath(right))


def shortcut_is_current(path: str | None = None) -> bool:
    """桌面快捷方式已存在，且目标就是当前这次运行的程序。"""
    target_path = path or shortcut_path()
    if not os.path.isfile(target_path):
        return False
    try:
        import win32com.client
        link = win32com.client.Dispatch("WScript.Shell").CreateShortcut(target_path)
        spec = launch_spec()
        if not _same_path(link.TargetPath, spec["target"]):
            return False
        if spec["arguments"] and not _same_path(link.Arguments, spec["arguments"]):
            return False
        return True
    except Exception:
        return False


def launch_spec() -> dict:
    """返回当前程序的启动参数：目标、命令行、工作目录、图标、描述。"""
    if FROZEN:
        exe = os.path.abspath(sys.executable)
        return {
            "target": exe,
            "arguments": "",
            "workdir": os.path.dirname(exe),
            "icon": exe,
            "description": "窗口工具集合 - 多显示器窗口布局守护",
        }
    # 源码运行：优先 pythonw.exe 避免控制台黑框
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    target = pythonw if os.path.isfile(pythonw) else sys.executable
    main_py = os.path.join(PROJECT_DIR, "main.py")
    return {
        "target": target,
        "arguments": '"%s"' % main_py,
        "workdir": PROJECT_DIR,
        "icon": ICON_PATH,
        "description": "窗口工具集合 - 多显示器窗口布局守护（源码运行）",
    }


def create_shortcut(path: str | None = None) -> dict:
    """在桌面创建快捷方式，返回 {"ok": bool, "path": str, "message": str}。

    path 可选，用于自检写入临时目录，避免污染真实桌面。
    """
    target_path = path or shortcut_path()
    try:
        spec = launch_spec()
        folder = os.path.dirname(target_path)
        if folder:
            os.makedirs(folder, exist_ok=True)

        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        link = shell.CreateShortcut(target_path)
        link.TargetPath = spec["target"]
        link.Arguments = spec["arguments"]
        link.WorkingDirectory = spec["workdir"]
        link.IconLocation = spec["icon"]
        link.Description = spec["description"]
        link.Save()

        if not os.path.isfile(target_path):
            return _fail(target_path, "快捷方式文件未生成")
        return {
            "ok": True,
            "path": target_path,
            "message": "已在桌面生成快捷方式：%s" % os.path.basename(target_path),
        }
    except Exception as exc:
        return _fail(target_path, "生成快捷方式异常：%s" % exc)


def _fail(path: str, message: str) -> dict:
    return {"ok": False, "path": path, "message": message}
