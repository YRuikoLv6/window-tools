"""全局热键的显示与按键解析。默认组合由各工具声明，自定义值另存配置。"""
from __future__ import annotations

import ctypes

import win32con

MOD_ALT = win32con.MOD_ALT
MOD_CONTROL = win32con.MOD_CONTROL
MOD_SHIFT = win32con.MOD_SHIFT
MOD_WIN = getattr(win32con, "MOD_WIN", 0x0008)
MOD_NOREPEAT = getattr(win32con, "MOD_NOREPEAT", 0x4000)

_MOD_ORDER = (
    (MOD_CONTROL, "Ctrl"),
    (MOD_ALT, "Alt"),
    (MOD_SHIFT, "Shift"),
    (MOD_WIN, "Win"),
)

# 这些键只表示修饰，不能单独作为快捷键的主键
_MODIFIER_VKS = {
    win32con.VK_SHIFT, win32con.VK_LSHIFT, win32con.VK_RSHIFT,
    win32con.VK_CONTROL, win32con.VK_LCONTROL, win32con.VK_RCONTROL,
    win32con.VK_MENU, win32con.VK_LMENU, win32con.VK_RMENU,
    0x5B, 0x5C,  # VK_LWIN, VK_RWIN
}

_VK_NAMES = {
    win32con.VK_SPACE: "Space",
    win32con.VK_TAB: "Tab",
    win32con.VK_RETURN: "Enter",
    win32con.VK_BACK: "Backspace",
    win32con.VK_DELETE: "Delete",
    win32con.VK_INSERT: "Insert",
    win32con.VK_HOME: "Home",
    win32con.VK_END: "End",
    win32con.VK_PRIOR: "PageUp",
    win32con.VK_NEXT: "PageDown",
    win32con.VK_LEFT: "Left",
    win32con.VK_RIGHT: "Right",
    win32con.VK_UP: "Up",
    win32con.VK_DOWN: "Down",
    0xBA: ";",
    0xBB: "=",
    0xBC: ",",
    0xBD: "-",
    0xBE: ".",
    0xBF: "/",
    0xC0: "`",
    0xDB: "[",
    0xDC: "\\",
    0xDD: "]",
    0xDE: "'",
}


def format_hotkey(modifiers: int, vk: int) -> str:
    """把修饰键和虚拟键格式化成 Ctrl+Alt+S 这种文字。"""
    modifiers = int(modifiers) & ~MOD_NOREPEAT
    parts = [name for flag, name in _MOD_ORDER if modifiers & flag]
    parts.append(vk_name(int(vk)))
    return "+".join(parts)


def vk_name(vk: int) -> str:
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return "F%d" % (vk - 0x6F)
    return _VK_NAMES.get(vk, "键%02X" % vk)


def pressed_modifiers() -> int:
    """读取当前按住的 Ctrl / Alt / Shift / Win。"""
    user32 = ctypes.windll.user32

    def down(vk: int) -> bool:
        return bool(user32.GetKeyState(vk) & 0x8000)

    mods = 0
    if down(win32con.VK_CONTROL):
        mods |= MOD_CONTROL
    if down(win32con.VK_MENU):
        mods |= MOD_ALT
    if down(win32con.VK_SHIFT):
        mods |= MOD_SHIFT
    if down(0x5B) or down(0x5C):
        mods |= MOD_WIN
    return mods


def is_modifier_vk(vk: int) -> bool:
    return int(vk) in _MODIFIER_VKS


def reject_reason(modifiers: int, vk: int) -> str:
    """不能作为自定义快捷键时返回原因，否则返回空字符串。"""
    if is_modifier_vk(vk) or int(vk) in (win32con.VK_ESCAPE, 0):
        return "请按下字母、数字或功能键"
    if not (int(modifiers) & (MOD_CONTROL | MOD_ALT | MOD_WIN)):
        return "快捷键需要同时包含 Ctrl、Alt 或 Win"
    return ""
