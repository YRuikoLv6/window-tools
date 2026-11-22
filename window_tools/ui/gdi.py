"""轻量 GDI+ 封装，仅使用系统 gdiplus.dll，无第三方依赖。

用于原生窗体的现代化自绘：圆角卡片、抗锯齿中文、渐变、阴影与状态色。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

_gdi = ctypes.WinDLL("gdiplus.dll")
P = ctypes.c_void_p
UINT = wintypes.UINT


class _StartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", wintypes.UINT),
        ("DebugEventCallback", P),
        ("SuppressBackgroundThread", wintypes.BOOL),
        ("SuppressExternalCodecs", wintypes.BOOL),
    ]


class RectF(ctypes.Structure):
    _fields_ = [
        ("X", ctypes.c_float), ("Y", ctypes.c_float),
        ("Width", ctypes.c_float), ("Height", ctypes.c_float),
    ]


def _api(name, argtypes, restype=ctypes.c_int):
    func = getattr(_gdi, name)
    func.argtypes = argtypes
    func.restype = restype
    return func


_gdi.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_size_t),
                                ctypes.POINTER(_StartupInput), P]
_gdi.GdiplusStartup.restype = ctypes.c_int
_gdi.GdiplusShutdown.argtypes = [ctypes.c_size_t]
_gdi.GdiplusShutdown.restype = None

_create_graphics = _api("GdipCreateFromHDC", [wintypes.HDC, ctypes.POINTER(P)])
_delete_graphics = _api("GdipDeleteGraphics", [P])
_set_smooth = _api("GdipSetSmoothingMode", [P, ctypes.c_int])
_set_text_hint = _api("GdipSetTextRenderingHint", [P, ctypes.c_int])

_create_solid = _api("GdipCreateSolidFill", [UINT, ctypes.POINTER(P)])
_delete_brush = _api("GdipDeleteBrush", [P])
_create_pen = _api("GdipCreatePen1", [UINT, ctypes.c_float, ctypes.c_int,
                                     ctypes.POINTER(P)])
_delete_pen = _api("GdipDeletePen", [P])

_create_path = _api("GdipCreatePath", [ctypes.c_int, ctypes.POINTER(P)])
_add_arc = _api("GdipAddPathArc", [P, ctypes.c_float, ctypes.c_float,
                                  ctypes.c_float, ctypes.c_float,
                                  ctypes.c_float, ctypes.c_float])
_close_figure = _api("GdipClosePathFigure", [P])
_delete_path = _api("GdipDeletePath", [P])
_set_clip = _api("GdipSetClipRect", [P, ctypes.c_float, ctypes.c_float,
                                    ctypes.c_float, ctypes.c_float,
                                    ctypes.c_int])
_reset_clip = _api("GdipResetClip", [P])
_fill_path = _api("GdipFillPath", [P, P, P])
_draw_path = _api("GdipDrawPath", [P, P, P])

_fill_rect = _api("GdipFillRectangle", [P, P, ctypes.c_float, ctypes.c_float,
                                        ctypes.c_float, ctypes.c_float])
_draw_rect = _api("GdipDrawRectangle", [P, P, ctypes.c_float, ctypes.c_float,
                                        ctypes.c_float, ctypes.c_float])
_fill_ellipse = _api("GdipFillEllipse", [P, P, ctypes.c_float, ctypes.c_float,
                                         ctypes.c_float, ctypes.c_float])
_create_gradient = _api("GdipCreateLineBrush", [ctypes.POINTER(RectF),
                                                ctypes.POINTER(RectF), UINT,
                                                UINT, ctypes.c_int,
                                                ctypes.POINTER(P)])

_create_family = _api("GdipCreateFontFamilyFromName",
                      [wintypes.LPCWSTR, P, ctypes.POINTER(P)])
_create_font = _api("GdipCreateFont", [P, ctypes.c_float, ctypes.c_int,
                                      ctypes.c_int, ctypes.POINTER(P)])
_delete_font = _api("GdipDeleteFont", [P])
_create_sf = _api("GdipCreateStringFormat", [ctypes.c_int, wintypes.WORD,
                                            ctypes.POINTER(P)])
_sf_align = _api("GdipSetStringFormatAlign", [P, ctypes.c_int])
_sf_line_align = _api("GdipSetStringFormatLineAlign", [P, ctypes.c_int])
_sf_trimming = _api("GdipSetStringFormatTrimming", [P, ctypes.c_int])
_delete_sf = _api("GdipDeleteStringFormat", [P])
_draw_string = _api("GdipDrawString", [P, wintypes.LPCWSTR, ctypes.c_int, P,
                                      ctypes.POINTER(RectF), P, P])
_measure_string = _api("GdipMeasureString", [P, wintypes.LPCWSTR, ctypes.c_int,
                                            P, ctypes.POINTER(RectF), P,
                                            ctypes.POINTER(RectF),
                                            ctypes.POINTER(ctypes.c_int)])
_delete_family = _api("GdipDeleteFontFamily", [P])

_TOKEN = ctypes.c_size_t(0)
_FAMILY = P()
_FONTS: dict[tuple, int] = {}
_SF: dict[tuple, int] = {}


def startup() -> bool:
    global _TOKEN, _FAMILY
    if _TOKEN.value:
        return True
    inp = _StartupInput(1, None, 0, 0)
    status = _gdi.GdiplusStartup(ctypes.byref(_TOKEN), ctypes.byref(inp), None)
    if status != 0:
        return False
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"):
        family = P()
        if _create_family(name, None, ctypes.byref(family)) == 0 and family.value:
            _FAMILY = family
            break
    return True


def shutdown() -> None:
    global _FAMILY, _TOKEN
    for font in _FONTS.values():
        _delete_font(P(font))
    _FONTS.clear()
    for sf in _SF.values():
        _delete_sf(P(sf))
    _SF.clear()
    if _FAMILY.value:
        _delete_family(_FAMILY)
        _FAMILY = P()
    if _TOKEN.value:
        _gdi.GdiplusShutdown(_TOKEN.value)
        _TOKEN = ctypes.c_size_t(0)


def argb(red: int, green: int, blue: int, alpha: int = 255) -> int:
    """GDI+ 使用 0xAARRGGBB 字节序。"""
    return ((alpha & 0xFF) << 24) | ((red & 0xFF) << 16) | \
           ((green & 0xFF) << 8) | (blue & 0xFF)


class C:
    """主题配色（亮色、克制的高饱和点缀）。"""
    BG = argb(244, 246, 250)
    CARD = argb(255, 255, 255)
    CARD_SOFT = argb(249, 250, 253)
    BORDER = argb(226, 232, 240)
    SHADOW = argb(20, 30, 54, 22)
    TEXT = argb(26, 35, 53)
    TEXT_SUB = argb(100, 112, 133)
    TEXT_FAINT = argb(152, 162, 179)
    PRIMARY = argb(54, 105, 233)
    PRIMARY_DARK = argb(40, 84, 196)
    PRIMARY_SOFT = argb(235, 242, 255)
    SUCCESS = argb(16, 168, 106)
    SUCCESS_SOFT = argb(228, 249, 239)
    WARNING = argb(232, 145, 31)
    WARNING_SOFT = argb(255, 245, 229)
    DANGER = argb(226, 67, 67)
    DANGER_SOFT = argb(255, 236, 236)
    DIVIDER = argb(238, 242, 248)
    HEADER_A = argb(28, 50, 92)
    HEADER_B = argb(46, 82, 152)


ALIGN_LEFT = 0
ALIGN_CENTER = 1
ALIGN_RIGHT = 2
VALIGN_TOP = 0
VALIGN_MIDDLE = 1

# 全局 UI 缩放（按主屏 DPI 设置，逻辑坐标统一乘此值绘制）
UI_SCALE = 1.0


def set_scale(scale: float) -> None:
    global UI_SCALE
    UI_SCALE = max(0.8, min(2.5, float(scale)))


def get_scale() -> float:
    return UI_SCALE


def _font(size: float, bold: bool) -> int:
    key = (round(size, 1), bool(bold))
    handle = _FONTS.get(key)
    if handle:
        return handle
    font = P()
    style = 1 if bold else 0
    if _create_font(_FAMILY, size, style, 2, ctypes.byref(font)) != 0:
        return 0
    _FONTS[key] = font.value
    return font.value


def _string_format(align: int, valign: int, ellipsis: bool) -> int:
    key = (align, valign, ellipsis)
    handle = _SF.get(key)
    if handle:
        return handle
    sf = P()
    if _create_sf(0x1000 if ellipsis else 0, 0x0409, ctypes.byref(sf)) != 0:
        return 0
    _sf_align(sf, align)
    _sf_line_align(sf, valign)
    if ellipsis:
        _sf_trimming(sf, 3)  # StringTrimmingEllipsisCharacter
    _SF[key] = sf.value
    return sf.value


class Canvas:
    """绑定一个 HDC 的绘制上下文；逻辑坐标自动按 UI_SCALE 放大到物理像素。"""

    def __init__(self, hdc: int, width: int, height: int,
                 scale: float | None = None):
        self.scale = float(scale if scale is not None else UI_SCALE)
        # width/height 为逻辑尺寸，物理尺寸需乘缩放
        self.width = max(1, int(width * self.scale))
        self.height = max(1, int(height * self.scale))
        self.graphics = P()
        self._brushes: list[int] = []
        self.ok = _create_graphics(hdc, ctypes.byref(self.graphics)) == 0
        if self.ok:
            _set_smooth(self.graphics, 2)   # HighQuality
            _set_text_hint(self.graphics, 3)  # AntiAliasGridFit

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self) -> None:
        for brush in self._brushes:
            _delete_brush(P(brush))
        self._brushes.clear()
        if self.graphics.value:
            _delete_graphics(self.graphics)
            self.graphics = P()

    def _s(self, value) -> float:
        return float(value) * self.scale

    def _brush(self, color: int) -> int:
        brush = P()
        if _create_solid(color, ctypes.byref(brush)) != 0:
            return 0
        self._brushes.append(brush.value)
        return brush.value

    def clear(self, color: int) -> None:
        _fill_rect(self.graphics, P(self._brush(color)), 0, 0,
                   self.width, self.height)

    def rect(self, x, y, w, h, color: int) -> None:
        if w <= 0 or h <= 0:
            return
        brush = self._brush(color)
        _fill_rect(self.graphics, P(brush), self._s(x), self._s(y),
                   self._s(w), self._s(h))

    def border(self, x, y, w, h, color: int, width: float = 1.0) -> None:
        pen = P()
        if _create_pen(color, self._s(width), 0, ctypes.byref(pen)) == 0:
            _draw_rect(self.graphics, pen, self._s(x), self._s(y),
                       self._s(w), self._s(h))
            _delete_pen(pen)

    def rounded(self, x, y, w, h, radius: float, fill: int,
                border: int | None = None, border_width: float = 1.0,
                gradient: int | None = None) -> None:
        if w <= 0 or h <= 0:
            return
        x, y, w, h = self._s(x), self._s(y), self._s(w), self._s(h)
        radius = max(0.0, min(self._s(radius), min(w, h) / 2.0))
        path = P()
        if _create_path(0, ctypes.byref(path)) != 0:
            return
        d = radius * 2
        _add_arc(path, x, y, d, d, 180, 90)
        _add_arc(path, x + w - d, y, d, d, 270, 90)
        _add_arc(path, x + w - d, y + h - d, d, d, 0, 90)
        _add_arc(path, x, y + h - d, d, d, 90, 90)
        _close_figure(path)
        brush = P()
        if gradient is not None:
            r1 = RectF(x, y, w, max(1.0, h / 2))
            r2 = RectF(x, y + h / 2, w, max(1.0, h / 2))
            if _create_gradient(ctypes.byref(r1), ctypes.byref(r2), fill,
                                gradient, 0, ctypes.byref(brush)) != 0:
                _create_solid(fill, ctypes.byref(brush))
        else:
            _create_solid(fill, ctypes.byref(brush))
        _fill_path(self.graphics, brush, path)
        _delete_brush(brush)
        if border is not None:
            pen = P()
            if _create_pen(border, self._s(border_width), 0,
                           ctypes.byref(pen)) == 0:
                _draw_path(self.graphics, pen, path)
                _delete_pen(pen)
        _delete_path(path)

    def clip(self, x, y, w, h) -> None:
        _set_clip(self.graphics, self._s(x), self._s(y),
                  self._s(w), self._s(h), 0)

    def reset_clip(self) -> None:
        _reset_clip(self.graphics)

    def ellipse(self, x, y, w, h, color: int) -> None:
        _fill_ellipse(self.graphics, P(self._brush(color)), self._s(x),
                      self._s(y), self._s(w), self._s(h))

    def text(self, value, x, y, w, h, size: float = 11, color: int = C.TEXT,
             bold: bool = False, align: int = ALIGN_LEFT,
             valign: int = VALIGN_MIDDLE, ellipsis: bool = True) -> None:
        if w <= 0 or h <= 0:
            return
        font = _font(size * self.scale, bold)
        if not font:
            return
        sf = _string_format(align, valign, ellipsis)
        rect = RectF(self._s(x), self._s(y), self._s(w), self._s(h))
        _draw_string(self.graphics, str(value), -1, P(font),
                     ctypes.byref(rect), P(sf) if sf else None,
                     P(self._brush(color)))

    def measure(self, value, size: float = 11, bold: bool = False) -> float:
        font = _font(size * self.scale, bold)
        if not font:
            return 0.0
        rect = RectF(0, 0, 10000, 1000)
        used = RectF()
        fitted = ctypes.c_int(0)
        _measure_string(self.graphics, str(value), -1, P(font),
                        ctypes.byref(rect), None, ctypes.byref(used),
                        ctypes.byref(fitted))
        return float(used.Width) / self.scale


def _solid(color: int) -> int:
    """创建临时画刷；调用方负责删除（此处由 GC 前的显式 delete 管理）。"""
    brush = P()
    if _create_solid(color, ctypes.byref(brush)) != 0:
        return 0
    return brush.value
