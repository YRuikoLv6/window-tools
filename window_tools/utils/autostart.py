"""开机自启：通过注册表 HKCU Run 项让程序随 Windows 登录自动运行。

- 启动命令复用 shortcut.launch_spec()，保证自启目标始终指向程序当前实际路径；
  程序被移动或重新打包后，设置里点一次「开启」即可覆盖旧路径。
- 只写当前用户（HKEY_CURRENT_USER），不需要管理员权限。
"""
from __future__ import annotations

import os

import win32api
import win32con

from . import shortcut

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "WindowTools"


def _open_key():
    return win32api.RegOpenKeyEx(
        win32con.HKEY_CURRENT_USER, RUN_KEY, 0, win32con.KEY_ALL_ACCESS
    )


def command() -> str:
    """构造注册表 Run 值：与桌面快捷方式使用同一套启动参数。"""
    spec = shortcut.launch_spec()
    target = spec["target"]
    if " " in target and not target.startswith('"'):
        target = '"%s"' % target
    parts = [target]
    if spec.get("arguments"):
        parts.append(spec["arguments"])
    return " ".join(parts)


def _normalize(text: str) -> str:
    """规范化命令用于比较：去引号/空白，统一路径大小写与分隔符。"""
    text = (text or "").strip().replace('"', "")
    if not text:
        return ""
    return os.path.normcase(os.path.normpath(text))


def read_value() -> str:
    """读取当前注册表值；读不到一律返回空串。"""
    try:
        key = _open_key()
        try:
            value, _kind = win32api.RegQueryValueEx(key, RUN_NAME)
            return value or ""
        finally:
            win32api.RegCloseKey(key)
    except Exception:
        return ""


def status() -> tuple[bool, bool]:
    """返回 (是否已设置开机自启, 是否指向当前程序)。"""
    value = read_value()
    if not value:
        return False, False
    return True, _normalize(value) == _normalize(command())


def set_enabled(enabled: bool) -> dict:
    """开启或关闭开机自启，返回 {"ok": bool, "message": str}。"""
    try:
        key = _open_key()
        try:
            if enabled:
                win32api.RegSetValueEx(
                    key, RUN_NAME, 0, win32con.REG_SZ, command())
                message = "已开启开机自启，登录 Windows 后自动运行本程序"
            else:
                try:
                    win32api.RegDeleteValue(key, RUN_NAME)
                except win32api.error:
                    # 值本来就不存在，视为关闭成功
                    pass
                message = "已关闭开机自启"
        finally:
            win32api.RegCloseKey(key)
        return {"ok": True, "message": message}
    except Exception as exc:
        action = "开启" if enabled else "关闭"
        return {"ok": False, "message": "%s开机自启失败：%s" % (action, exc)}
