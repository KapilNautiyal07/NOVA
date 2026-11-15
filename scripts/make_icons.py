"""Render NOVA's orb icon as PNGs (pure Python, no dependencies)."""
import math, struct, sys, zlib
from pathlib import Path

STOPS = [(0.0, (255, 255, 255)), (0.12, (201, 194, 255)), (0.42, (124, 108, 255)), (0.72, (59, 47, 191)), (1.0, (26, 20, 70))]

def grad(t):
    t = max(0.0, min(1.0, t))
    for (a, ca), (b, cb) in zip(STOPS, STOPS[1:]):
        if t <= b:
            k = (t - a) / (b - a)
            return tuple(round(x + (y - x) * k) for x, y in zip(ca, cb))
    return STOPS[-1][1]

def render(size, ss=4):
    n = size * ss
    cx = cy = n / 2
    r = n * 0.46
    hx, hy = n * 0.36, n * 0.30  # highlight
    out = bytearray()
    for y in range(size):
        out.append(0)
        for x in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    px, py = x * ss + sx + 0.5, y * ss + sy + 0.5
                    if math.hypot(px - cx, py - cy) <= r:
                        t = math.hypot(px - hx, py - hy) / (r * 1.55)
                        c = grad(t)
                        acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]; acc[3] += 255
            k = ss * ss
            a = acc[3] / k
            if a:
                out += bytes([round(acc[0] * 255 / acc[3]), round(acc[1] * 255 / acc[3]), round(acc[2] * 255 / acc[3]), round(a)])
            else:
                out += b"\0\0\0\0"
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(out), 9)) + chunk(b"IEND", b"")

if __name__ == "__main__":
    dest = Path(sys.argv[1])
    for s in (16, 32, 48, 128):
        (dest / f"icon{s}.png").write_bytes(render(s))
    print("icons written to", dest)
