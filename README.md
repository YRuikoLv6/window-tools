# 窗口工具集合（WindowTools）

解决 Windows 双模显示器（如 4K ↔ 1K）切换后，窗口位置/大小错乱、跑到屏幕外无法操作的问题。采用原生 Win32 自绘窗体（GDI+），不依赖浏览器，单文件 exe 免 Python 环境即可运行。

## 功能

- **自动还原**：切换显示器模式后，按「切换前快照」自动按比例还原窗口位置与大小
- **手动兜底**：保存当前布局 / 自动还原 / 精确还原（原始像素）
- **越界救援**：把跑出屏幕的窗口一键拉回可见区域
- **鼠标屏幕锁定**：把光标限制在当前显示器，防止误入其他屏幕；关闭/开启显示器或增删显示器时自动解锁
- **实时列表**：显示当前窗口，越界窗口红色标记
- **全局热键**：Ctrl+Alt+S 保存、Ctrl+Alt+R 还原、Ctrl+Alt+M 救援、Ctrl+Alt+L 锁定/解锁鼠标、Ctrl+Alt+U 强制解锁（被占用时自动降级到备用组合）
- **可扩展**：插件式架构，新增工具无需改界面代码

## 快速开始

### 直接使用（推荐）

双击 `WindowTools.exe`，程序会自动弹出窗体。首次使用先点「保存当前布局」，之后切换显示器模式即可自动还原。

- 配置、布局快照、日志写在 exe 同级的 `data/` 目录
- 若 exe 放在只读目录（如 Program Files），自动回退到 `%APPDATA%\WindowTools`
- 重复双击不会启动第二个实例，会直接唤起已运行的窗口

### 源码运行

```bash
pip install -r requirements.txt
python main.py
python main.py --selftest   # 核心自检
```

## 项目结构

```
main.py                     入口（含单实例保护、自检）
WindowTools.spec            PyInstaller 打包配置
window_tools/
  core/
    desktop_app.py          原生自绘窗体宿主（GDI+ 界面、消息循环、交互）
    base_tool.py            工具基类 / WebAction 定义
    tool_manager.py         工具注册表与事件分发
  tools/
    window_layout.py        窗口布局守护（核心功能）
    mouse_lock.py           鼠标屏幕锁定（ClipCursor + 热键解锁）
  ui/
    gdi.py                  GDI+ 绘制封装（圆角/渐变/中文/阴影）
  utils/
    winapi.py               Win32 API 封装（窗口枚举/屏幕信息）
    config.py               JSON 配置持久化
    logger.py               日志
dist/WindowTools.exe        单文件可执行程序
窗口工具集合-便携版.zip       exe + 源码的便携包
```

## 扩展新工具

1. 在 `window_tools/tools/` 新建模块，继承 `BaseTool`
2. 实现 `handle_action(action_id)` 与 `get_ui_actions()`（默认复用 `get_web_actions()`）
3. 在 `main.py` 中 `manager.register(YourTool)` 注册一行

宿主从 `ToolManager.describe_tools()` 动态生成工具卡片与按钮，无需改动窗体代码。

## 打包

```bash
python -m PyInstaller --clean --noconfirm WindowTools.spec
```

产物为 `dist/WindowTools.exe`（约 8.3 MB）。

## 已知限制

- 仅支持 Windows x64
- 仅管理带标题、非最小化的顶层窗口
- 自动还原需程序保持运行
- 鼠标锁定在极少数全屏游戏或系统级弹窗中可能被临时释放，工具会自动尝试恢复
- 热键可能被其他软件占用，已内置候选组合自动降级

## 环境

Python 3.13 + pywin32；运行时无需 Python（exe 已打包）。
