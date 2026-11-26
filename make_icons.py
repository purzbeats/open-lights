"""Generate the phone app icons (PNG, standard library only): a warm glow on a dark background.

    py make_icons.py
"""
import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "static"
BG = (13, 14, 18)
WARM = (255, 170, 80)
CORE = (255, 236, 200)


def png(path, size, pixel):
    rows = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(size)) for y in range(size))
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def glow(size, scale):
    """scale < 1 keeps the glow inside Android's maskable safe zone."""
    c = (size - 1) / 2

    def pixel(x, y):
        d = math.hypot(x - c, y - c) / (size / 2) / scale        # 0 at center, 1 at edge of the glow area
        halo = max(0.0, 1 - d) ** 2.2                             # soft outer glow
        core = max(0.0, 1 - d / 0.34) ** 0.6                      # bright bulb center
        col = [BG[i] + (WARM[i] - BG[i]) * halo for i in range(3)]
        col = [col[i] + (CORE[i] - col[i]) * core for i in range(3)]
        return [max(0, min(255, int(v))) for v in col]
    return pixel


if __name__ == "__main__":
    ROOT.mkdir(exist_ok=True)
    png(ROOT / "icon-192.png", 192, glow(192, 0.95))
    png(ROOT / "icon-512.png", 512, glow(512, 0.95))
    png(ROOT / "icon-maskable-512.png", 512, glow(512, 0.7))
    print("icons written to", ROOT)
