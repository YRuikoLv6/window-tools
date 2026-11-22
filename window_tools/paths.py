"""统一路径定义，兼容源码运行与 PyInstaller 冻结运行。

源码运行：数据写入项目根目录的 data/
冻结运行：数据写入 exe 同级的 data/，网页资源从 _MEIPASS 内嵌目录读取
"""
import os
import sys

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RESOURCE_ROOT = getattr(sys, "_MEIPASS", PACKAGE_DIR)
    WEB_DIR = os.path.join(RESOURCE_ROOT, "window_tools", "web")
else:
    BASE_DIR = os.path.dirname(PACKAGE_DIR)
    WEB_DIR = os.path.join(PACKAGE_DIR, "web")

PROJECT_DIR = BASE_DIR
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
LAYOUT_PATH = os.path.join(DATA_DIR, "layout.json")

def _prepare_data_dirs() -> None:
    """优先写 exe 同级；不可写时回退到用户目录，避免只读安装位置导致崩溃。"""
    global DATA_DIR, LOG_DIR, CONFIG_PATH, LAYOUT_PATH
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        probe = os.path.join(DATA_DIR, ".write_test")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("")
        os.remove(probe)
        return
    except Exception:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        user_root = os.path.join(base, "WindowTools")
        DATA_DIR = os.path.join(user_root, "data")
        LOG_DIR = os.path.join(DATA_DIR, "logs")
        CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
        LAYOUT_PATH = os.path.join(DATA_DIR, "layout.json")
        os.makedirs(LOG_DIR, exist_ok=True)


_prepare_data_dirs()
