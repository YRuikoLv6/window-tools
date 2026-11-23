# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件原生窗体 exe，数据写 exe 同级目录。"""
import os
import sys

datas = [("app.ico", ".")]

# conda 环境把 libffi 放在 Library\bin，PyInstaller 默认找不到，缺了 ctypes 无法启动。
_conda_bin = os.path.join(os.path.dirname(sys.executable), "Library", "bin")
binaries = []
for _dll in ("ffi-8.dll", "libbz2.dll", "liblzma.dll"):
    _path = os.path.join(_conda_bin, _dll)
    if os.path.isfile(_path):
        binaries.append((_path, "."))

hiddenimports = [
    "win32api",
    "win32con",
    "win32gui",
    "win32process",
    "win32file",
    "win32clipboard",
    "pywintypes",
    "win32com",
    "win32com.client",
    "pythoncom",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WindowTools",
    icon="app.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
