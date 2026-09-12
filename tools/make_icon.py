"""Draws the app icon (the purple-to-cyan play logo from the UI) into ui/app.ico.

Standard library only. Run from the v2 folder:  python tools/make_icon.py
"""

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "ui" / "app.ico"
SIZES = (256, 48, 32, 24, 16)
TOP_LEFT, BOTTOM_RIGHT = (0x8B, 0x7B, 0xFF), (0x22, 0xD3, 0xEE)


def in_rounded_square(x, y, size, radius):
    cx = min(max(x, radius), size - radius)
    cy = min(max(y, radius), size - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius * radius


def in_triangle(x, y, a, b, c):
    def side(p, q):
        return (q[0] - p[0]) * (y - p[1]) - (q[1] - p[1]) * (x - p[0])
    s1, s2, s3 = side(a, b), side(b, c), side(c, a)
    return (s1 >= 0 and s2 >= 0 and s3 >= 0) or (s1 <= 0 and s2 <= 0 and s3 <= 0)


def render(size, ss=4):
    """RGBA rows, 4x4 supersampled; same geometry as the favicon (64-unit grid)"""
    u = size / 64
    radius = 16 * u
    tri = ((25 * u, 19 * u), (45 * u, 32 * u), (25 * u, 45 * u))
    samples = ss * ss
    rows = []
    for py in range(size):
        row = bytearray(b"\x00")
        for px in range(size):
            cov = r = g = b = 0
            for sy in range(ss):
                y = py + (sy + 0.5) / ss
                for sx in range(ss):
                    x = px + (sx + 0.5) / ss
                    if not in_rounded_square(x, y, size, radius):
                        continue
                    cov += 1
                    if in_triangle(x, y, *tri):
                        r += 255
                        g += 255
                        b += 255
                    else:
                        t = (x + y) / (2 * size)
                        r += TOP_LEFT[0] + (BOTTOM_RIGHT[0] - TOP_LEFT[0]) * t
                        g += TOP_LEFT[1] + (BOTTOM_RIGHT[1] - TOP_LEFT[1]) * t
                        b += TOP_LEFT[2] + (BOTTOM_RIGHT[2] - TOP_LEFT[2]) * t
            if cov:
                row += bytes((round(r / cov), round(g / cov), round(b / cov), round(255 * cov / samples)))
            else:
                row += b"\x00\x00\x00\x00"
        rows.append(bytes(row))
    return b"".join(rows)


def png(size):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(render(size), 9)) + chunk(b"IEND", b""))


def main():
    images = [png(s) for s in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = b""
    for size, data in zip(SIZES, images):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    OUT.write_bytes(header + entries + b"".join(images))
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB, sizes {', '.join(map(str, SIZES))})")


if __name__ == "__main__":
    main()
