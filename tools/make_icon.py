"""生成应用图标 app.ico（纯 Python，无第三方依赖）。

图标风格与界面一致：蓝色圆角底 + 白色窗口轮廓 + 锁定色点缀。
输出包含 16/24/32/48/64/128/256 多种尺寸，PNG 压缩条目，兼容 Vista+。
运行: python tools/make_icon.py
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app.ico"
SIZES = [16, 24, 32, 48, 64, 128, 256]

BG_TOP = (46, 82, 152)      # HEADER_B
BG_BOTTOM = (28, 50, 92)    # HEADER_A
WINDOW = (255, 255, 255)
WINDOW_ALT = (235, 242, 255)
ACCENT = (54, 105, 233)     # PRIMARY
SUCCESS = (16, 168, 106)


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _mix(dst, src, alpha):
    if alpha <= 0:
        return dst
    if alpha >= 1:
        return src
    return tuple(int(round(dst[i] * (1 - alpha) + src[i] * alpha))
                 for i in range(3))


def _rounded_alpha(nx, ny, x, y, w, h, r, soft):
    """点 (nx,ny) 在圆角矩形内的覆盖度（带抗锯齿软边）。"""
    dx = max(x + r - nx, 0.0, nx - (x + w - r))
    dy = max(y + r - ny, 0.0, ny - (y + h - r))
    outside = math.hypot(dx, dy)
    if outside <= r:
        d = -min(r - outside,
                 min(nx - x, x + w - nx, ny - y, y + h - ny))
    else:
        d = outside - r
    return max(0.0, min(1.0, 0.5 - d / soft))


def _ring_alpha(nx, ny, cx, cy, radius, width, soft):
    dist = abs(math.hypot(nx - cx, ny - cy) - radius)
    d = dist - width / 2
    return max(0.0, min(1.0, 0.5 - d / soft))


def render(size: int) -> bytes:
    """返回一个 size×size 的 RGBA PNG 字节。"""
    scale = 4 if size <= 32 else 2  # 超采样抗锯齿
    ss = size * scale
    rows: list[bytearray] = [bytearray() for _ in range(size)]
    soft = 1.0 / ss

    for py in range(size):
        row = bytearray()
        for px in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(scale):
                for sx in range(scale):
                    nx = (px + (sx + 0.5) / scale) / size
                    ny = (py + (sy + 0.5) / scale) / size

                    r, g, b, a = 0, 0, 0, 0.0

                    # 背景圆角方块
                    ab = _rounded_alpha(nx, ny, 0.0, 0.0, 1.0, 1.0, 0.22, soft)
                    if ab > 0:
                        grad = _lerp(BG_TOP, BG_BOTTOM, min(1.0, max(0.0, ny)))
                        r, g, b, a = grad[0], grad[1], grad[2], ab

                    # 窗口主体（白色圆角矩形）
                    aw = _rounded_alpha(nx, ny, 0.20, 0.26, 0.60, 0.50, 0.05, soft)
                    if aw > 0:
                        win = WINDOW if ny < 0.40 else WINDOW_ALT
                        r, g, b = _mix((r, g, b), win, aw)
                        a = a + aw * (1 - a)

                    # 标题栏分隔线
                    if 0.395 <= ny <= 0.41 and 0.22 <= nx <= 0.78:
                        al = _rounded_alpha(nx, ny, 0.22, 0.395, 0.56, 0.015,
                                            0.006, soft)
                        r, g, b = _mix((r, g, b), ACCENT, al)
                        a = a + al * (1 - a)

                    # 标题栏三个小圆点（窗口按钮）
                    for i, cx in enumerate((0.27, 0.33, 0.39)):
                        al = _ring_alpha(nx, ny, cx, 0.33, 0.018, 0.016, soft)
                        if al > 0:
                            tone = (226, 67, 67) if i == 0 else \
                                ((232, 145, 31) if i == 1 else SUCCESS)
                            r, g, b = _mix((r, g, b), tone, al)
                            a = a + al * (1 - a)

                    # 窗口内容区两条示意线
                    for (ly, lw) in ((0.50, 0.42), (0.60, 0.30)):
                        al = _rounded_alpha(nx, ny, 0.27, ly, lw, 0.045,
                                            0.02, soft)
                        if al > 0:
                            r, g, b = _mix((r, g, b), ACCENT, al * 0.85)
                            a = a + al * (1 - a)

                    acc[0] += r
                    acc[1] += g
                    acc[2] += b
                    acc[3] += a

            count = scale * scale
            alpha = int(round(acc[3] / count * 255))
            if alpha <= 0:
                row.extend(b"\x00\x00\x00\x00")
                continue
            # 未预乘，按 alpha 反推真实颜色
            rr = acc[0] / count
            gg = acc[1] / count
            bb = acc[2] / count
            cov = acc[3] / count
            if cov > 0:
                rr, gg, bb = rr / cov, gg / cov, bb / cov
            row.extend(bytes((min(255, max(0, int(round(rr)))),
                              min(255, max(0, int(round(gg)))),
                              min(255, max(0, int(round(bb)))),
                              alpha)))
        rows[py] = row

    raw = bytearray()
    for row in rows:
        raw.append(0)  # PNG filter: None
        raw.extend(row)

    return _png(size, size, bytes(raw))


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _png(width: int, height: int, raw: bytes) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(raw, 9))
            + _chunk(b"IEND", b""))


def build_ico(images: list[tuple[int, bytes]]) -> bytes:
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries = bytearray()
    body = bytearray()
    for size, png in images:
        entries.extend(struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,
            size if size < 256 else 0,
            0, 0, 1, 32, len(png), offset,
        ))
        body.extend(png)
        offset += len(png)
    return header + bytes(entries) + bytes(body)


def main() -> None:
    images = [(s, render(s)) for s in SIZES]
    OUT.write_bytes(build_ico(images))
    print(f"已生成 {OUT} ({OUT.stat().st_size} bytes, {len(SIZES)} 个尺寸)")


if __name__ == "__main__":
    main()
