# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件原生窗体 exe，数据写 exe 同级目录。"""

datas = [("app.ico", ".")]

hiddenimports = [
    "win32api",
    "win32con",
    "win32gui",
    "win32process",
    "pywintypes",
    "win32com",
    "win32com.client",
    "pythoncom",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
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
