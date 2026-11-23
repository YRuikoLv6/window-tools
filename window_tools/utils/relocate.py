"""把散落在杂乱目录里的 exe 挪到自己的文件夹。

只在冻结后的第一次启动时生效：当前目录里除了程序本身还有其他文件，
并且还没有 data 目录。源码运行不会移动任何文件。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

APP_DIR_NAME = "窗口工具集合"
_CREATE_NO_WINDOW = 0x08000000


def directory_needs_own_folder(folder: str, exe_name: str) -> bool:
    """目录里已经有别的文件，且程序还没在这里保存过数据。"""
    try:
        entries = os.listdir(folder)
    except OSError:
        return False
    others = []
    for name in entries:
        if name.lower() == exe_name.lower():
            continue
        if os.path.isfile(os.path.join(folder, name)):
            others.append(name)
    if not others:
        return False
    if os.path.isdir(os.path.join(folder, "data")):
        return False
    return True


def pick_app_folder(parent: str) -> str:
    """在当前目录下找一个空的「窗口工具集合」文件夹。"""
    for index in range(1, 100):
        name = APP_DIR_NAME if index == 1 else "%s %d" % (APP_DIR_NAME, index)
        candidate = os.path.join(parent, name)
        if not os.path.exists(candidate):
            return candidate
        if os.path.isdir(candidate) and not os.listdir(candidate):
            return candidate
    return os.path.join(parent, "%s 100" % APP_DIR_NAME)


def relocate_frozen_exe() -> None:
    """需要挪动时复制到新文件夹、启动新进程，并在退出后删除原来的 exe。"""
    if not getattr(sys, "frozen", False):
        return
    if "--selftest" in sys.argv:
        return
    exe = os.path.abspath(sys.executable)
    folder = os.path.dirname(exe)
    if not directory_needs_own_folder(folder, os.path.basename(exe)):
        return
    dest_dir = pick_app_folder(folder)
    dest_exe = os.path.join(dest_dir, os.path.basename(exe))
    try:
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copy2(exe, dest_exe)
    except OSError:
        return
    subprocess.Popen([dest_exe, "--relocated"], cwd=dest_dir, close_fds=True)
    subprocess.Popen(
        ["cmd", "/c", 'ping 127.0.0.1 -n 4 >nul & del /f /q "%s"' % exe],
        creationflags=_CREATE_NO_WINDOW,
        close_fds=True,
        cwd=os.environ.get("TEMP") or folder,
    )
    os._exit(0)
