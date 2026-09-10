#!/usr/bin/env python3
"""Generate the extension's PNG icons with the stdlib only (no Pillow).

A rounded square in Scrapescape's accent (#e94560) with a white download arrow.
Run from this directory: python3 generate_icons.py
"""
import os
import struct
import zlib

ACCENT = (233, 69, 96)
WHITE = (255, 255, 255)


def _pixel(x, y, n):
    """Return (r, g, b, a) for pixel (x, y) in an n x n icon."""
    cx = n / 2.0
    r = 0.20 * n  # corner radius for the rounded square

    # Rounded corners -> transparent
    for corner_x, corner_y in ((r, r), (n - r, r), (r, n - r), (n - r, n - r)):
        inside_box = (
            (corner_x == r and x < r or corner_x != r and x > n - r)
            and (corner_y == r and y < r or corner_y != r and y > n - r)
        )
        if inside_box and (x - corner_x) ** 2 + (y - corner_y) ** 2 > r * r:
            return (0, 0, 0, 0)

    # White download arrow: stem, arrowhead, baseline tray.
    stem = (0.24 * n <= y <= 0.50 * n) and abs(x - cx) <= 0.07 * n
    head = False
    if 0.46 * n <= y <= 0.68 * n:
        t = (y - 0.46 * n) / (0.68 * n - 0.46 * n)  # 0..1 top->tip
        head = abs(x - cx) <= 0.20 * n * (1 - t)
    tray = (0.74 * n <= y <= 0.82 * n) and abs(x - cx) <= 0.22 * n

    if stem or head or tray:
        return (*WHITE, 255)
    return (*ACCENT, 255)


def _png(n):
    raw = bytearray()
    for y in range(n):
        raw.append(0)  # filter type 0 (None) per scanline
        for x in range(n):
            raw.extend(_pixel(x, y, n))
    comp = zlib.compress(bytes(raw), 9)

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", comp) + chunk(b"IEND", b"")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    for size in (16, 32, 48, 128):
        with open(os.path.join(here, f"icon{size}.png"), "wb") as f:
            f.write(_png(size))
        print(f"wrote icon{size}.png")


if __name__ == "__main__":
    main()
