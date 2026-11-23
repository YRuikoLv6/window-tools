"""工具注册表：生命周期、事件分发、菜单聚合。"""
from __future__ import annotations

from typing import List, Type

from .base_tool import BaseTool, MenuItem, WebAction


class ToolManager:
    def __init__(self, app):
        self.app = app
        self.tools: List[BaseTool] = []

    def register(self, tool_class: Type[BaseTool]) -> BaseTool:
        tool = tool_class(self.app)
        self.tools.append(tool)
        self.app.log.info("已加载工具: %s v%s", tool.name, tool.version)
        return tool

    def load_all(self) -> None:
        for tool in self.tools:
            try:
                tool.on_load()
            except Exception:
                self.app.log.exception("工具加载失败: %s", tool.name)

    def unload_all(self) -> None:
        for tool in reversed(self.tools):
            try:
                tool.on_unload()
            except Exception:
                self.app.log.exception("工具卸载失败: %s", tool.name)

    def on_display_change(self, signature: dict) -> None:
        for tool in self.tools:
            try:
                tool.on_display_change(signature)
            except Exception:
                self.app.log.exception("工具处理显示器变化失败: %s", tool.name)

    def get_menu_items(self) -> List[MenuItem]:
        items: List[MenuItem] = []
        for tool in self.tools:
            tool_items = tool.get_menu_items()
            if not tool_items:
                continue
            items.append(MenuItem(f"{tool.name}  [{tool.version}]", enabled=False))
            items.extend(tool_items)
        return items

    def get_status_lines(self) -> List[str]:
        lines = []
        for tool in self.tools:
            try:
                lines.append(f"{tool.name}: {tool.get_status()}")
            except Exception:
                lines.append(f"{tool.name}: 状态获取失败")
        return lines

    def find(self, name: str) -> BaseTool | None:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

    def describe_tools(self) -> List[dict]:
        """给网页端的工具清单（含动态按钮）。"""
        result = []
        for tool in self.tools:
            try:
                actions: List[WebAction] = tool.get_ui_actions()
                sections = []
                getter = getattr(tool, "get_detail_sections", None)
                if getter:
                    sections = getter() or []
                result.append({
                    "name": tool.name,
                    "version": tool.version,
                    "description": tool.description,
                    "status": tool.get_status(),
                    "sections": sections,
                    "pinned": bool(getattr(tool, "nav_pinned", False)),
                    "actions": [
                        {
                            "id": a.id, "label": a.label, "kind": a.kind,
                            "hint": a.hint, "active": a.active, "tone": a.tone,
                        }
                        for a in actions
                    ],
                })
            except Exception:
                self.app.log.exception("工具描述生成失败: %s", tool.name)
        return result
