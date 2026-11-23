"""所有工具的基类。新增功能只需继承 BaseTool 并在 main.py 注册。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional


@dataclass
class MenuItem:
    label: str
    callback: Optional[Callable[[], None]] = None
    enabled: bool = True
    checked: bool = False
    separator_before: bool = False


@dataclass
class WebAction:
    """网页控制台中的一个操作按钮。kind: button / toggle。"""
    id: str
    label: str
    kind: str = "button"
    hint: str = ""
    active: bool = False
    tone: str = "primary"


class BaseTool:
    name = "未命名工具"
    description = ""
    version = "1.0.0"
    nav_pinned = False

    def __init__(self, app):
        self.app = app
        self.log = app.log

    # 生命周期 -------------------------------------------------------
    def on_load(self) -> None:
        """工具被加载时调用，注册热键/启动后台线程等。"""

    def on_unload(self) -> None:
        """宿主退出时调用，务必释放资源。"""

    def on_display_change(self, signature: dict) -> None:
        """显示器模式变化（宿主已去抖），signature 为变化后的屏幕指纹。"""

    # 托盘集成 -------------------------------------------------------
    def get_menu_items(self) -> List[MenuItem]:
        """返回该工具在托盘右键菜单中的条目。"""
        return []

    def get_status(self) -> str:
        """一行状态文本，供网页状态卡片展示。"""
        return self.name

    # 网页控制台 ---------------------------------------------------
    def get_web_actions(self) -> List[WebAction]:
        """返回该工具暴露的按钮定义（兼容网页/桌面两种宿主）。"""
        return []

    def get_ui_actions(self) -> List[WebAction]:
        """桌面窗体按钮定义，默认复用 get_web_actions。"""
        return self.get_web_actions()

    def handle_action(self, action_id: str, payload: dict | None = None) -> dict:
        """执行网页按钮动作，返回 {"ok": bool, "message": str}。"""
        return {"ok": False, "message": f"工具 {self.name} 不支持该操作"}
