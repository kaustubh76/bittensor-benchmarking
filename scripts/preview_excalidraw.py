#!/usr/bin/env python
"""Render an .excalidraw file to SVG + PNG using only the standard library.

A dependency-free visual proof of the architecture canvas: boxes (dashed for
zones), their text, and every bound arrow. The SVG is crisp and browser-openable
(readable labels); the PNG is a quick raster for eyeballing layout / collisions.

    uv run python scripts/preview_excalidraw.py [file.excalidraw] [--out DIR] [--scale 0.3]

Optional zone exports (the Readme's suggested pitch-deck crops):

    --crop X,Y,W,H   render only the elements whose bounding box falls inside
                     this canvas-coordinate rectangle (small tolerance applied);
                     arrows are kept only when every point is inside, so
                     cross-zone wires survive iff the crop covers both ends
    --name SUFFIX    write <stem>.<SUFFIX>.svg/.png instead of <stem>.preview.*
"""

from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAD = 40


def _el_bbox(e):
    if e["type"] == "arrow":
        xs = [e["x"] + px for px, py in e["points"]]
        ys = [e["y"] + py for px, py in e["points"]]
        return min(xs), min(ys), max(xs), max(ys)
    return e["x"], e["y"], e["x"] + e.get("width", 0), e["y"] + e.get("height", 0)


def crop_elements(els, rect, tol=10):
    cx0, cy0, cw, ch = rect
    cx1, cy1 = cx0 + cw, cy0 + ch
    kept = []
    for e in els:
        x0, y0, x1, y1 = _el_bbox(e)
        if (x0 >= cx0 - tol and y0 >= cy0 - tol
                and x1 <= cx1 + tol and y1 <= cy1 + tol):
            kept.append(e)
    return kept


def _bounds(els):
    xs, ys = [], []
    for e in els:
        if e["type"] == "arrow":
            for px, py in e["points"]:
                xs.append(e["x"] + px)
                ys.append(e["y"] + py)
        else:
            xs += [e["x"], e["x"] + e.get("width", 0)]
            ys += [e["y"], e["y"] + e.get("height", 0)]
    return min(xs), min(ys), max(xs), max(ys)


# ---------- SVG ----------
def render_svg(doc) -> str:
    els = doc["elements"]
    x0, y0, x1, y1 = _bounds(els)
    W, H = x1 - x0 + 2 * PAD, y1 - y0 + 2 * PAD

    def X(v):
        return round(v - x0 + PAD, 1)

    def Y(v):
        return round(v - y0 + PAD, 1)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" '
           f'viewBox="0 0 {W:.0f} {H:.0f}" font-family="Helvetica,Arial,sans-serif">',
           f'<rect width="{W:.0f}" height="{H:.0f}" fill="#ffffff"/>',
           '<defs><marker id="a" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto">'
           '<path d="M0,0 L7,3 L0,6 Z" fill="#333"/></marker></defs>']
    # rectangles
    for e in els:
        if e["type"] != "rectangle":
            continue
        dash = ' stroke-dasharray="7 5"' if e.get("strokeStyle") == "dashed" else ""
        rx = 4 if e.get("roundness") else 0
        out.append(f'<rect x="{X(e["x"])}" y="{Y(e["y"])}" width="{e["width"]:.0f}" '
                   f'height="{e["height"]:.0f}" rx="{rx}" fill="{e.get("backgroundColor","transparent")}" '
                   f'stroke="{e["strokeColor"]}" stroke-width="{e.get("strokeWidth",2)}"{dash}/>')
    # arrows
    for e in els:
        if e["type"] != "arrow":
            continue
        pts = " ".join(f"{X(e['x']+px)},{Y(e['y']+py)}" for px, py in e["points"])
        dash = ' stroke-dasharray="6 5"' if e.get("strokeStyle") == "dashed" else ""
        out.append(f'<polyline points="{pts}" fill="none" stroke="{e["strokeColor"]}" '
                   f'stroke-width="{e.get("strokeWidth",2)}"{dash} marker-end="url(#a)"/>')
    # text (multi-line)
    for e in els:
        if e["type"] != "text":
            continue
        size = e.get("fontSize", 13)
        for i, line in enumerate(e["text"].split("\n")):
            ln = (line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            out.append(f'<text x="{X(e["x"])}" y="{Y(e["y"]) + size*(i+0.9):.1f}" '
                       f'font-size="{size}" fill="{e["strokeColor"]}">{ln}</text>')
    out.append("</svg>")
    return "\n".join(out)


# ---------- PNG (pure stdlib raster) ----------
def _rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


class Canvas:
    def __init__(self, w, h, bg=(255, 255, 255)):
        self.w, self.h = w, h
        self.buf = bytearray(bg * (w * h))

    def px(self, x, y, c):
        x, y = int(x), int(y)
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.buf[i:i + 3] = bytes(c)

    def hline(self, x0, x1, y, c, t=1):
        for k in range(t):
            for x in range(int(min(x0, x1)), int(max(x0, x1)) + 1):
                self.px(x, y + k, c)

    def vline(self, x, y0, y1, c, t=1):
        for k in range(t):
            for y in range(int(min(y0, y1)), int(max(y0, y1)) + 1):
                self.px(x + k, y, c)

    def rect(self, x, y, w, h, c, t=1):
        self.hline(x, x + w, y, c, t)
        self.hline(x, x + w, y + h - 1, c, t)
        self.vline(x, y, y + h, c, t)
        self.vline(x + w - 1, y, y + h, c, t)

    def fill(self, x, y, w, h, c):
        for yy in range(int(y), int(y + h)):
            self.hline(x, x + w, yy, c, 1)

    def line(self, x0, y0, x1, y1, c):
        x0, y0, x1, y1 = map(int, (x0, y0, x1, y1))
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        while True:
            self.px(x0, y0, c)
            self.px(x0 + 1, y0, c)  # 2px weight
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def png(self) -> bytes:
        raw = bytearray()
        for y in range(self.h):
            raw.append(0)
            raw += self.buf[y * self.w * 3:(y + 1) * self.w * 3]

        def chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        ihdr = struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def render_png(doc, scale=0.3) -> bytes:
    els = doc["elements"]
    x0, y0, x1, y1 = _bounds(els)

    def X(v):
        return (v - x0 + PAD) * scale

    def Y(v):
        return (v - y0 + PAD) * scale

    cv = Canvas(int((x1 - x0 + 2 * PAD) * scale), int((y1 - y0 + 2 * PAD) * scale))
    for e in els:  # zones first (dashed → draw as thin outline)
        if e["type"] == "rectangle":
            c = _rgb(e["strokeColor"])
            cv.rect(X(e["x"]), Y(e["y"]), e["width"] * scale, e["height"] * scale, c, t=2)
            if e.get("strokeStyle") != "dashed":  # card: colored title strip
                cv.fill(X(e["x"]) + 1, Y(e["y"]) + 1, e["width"] * scale - 2, 5, c)
    for e in els:
        if e["type"] == "arrow":
            c = _rgb(e["strokeColor"])
            for k in range(len(e["points"]) - 1):
                ax, ay = e["x"] + e["points"][k][0], e["y"] + e["points"][k][1]
                bx, by = e["x"] + e["points"][k + 1][0], e["y"] + e["points"][k + 1][1]
                cv.line(X(ax), Y(ay), X(bx), Y(by), c)
    return cv.png()


def main() -> None:
    argv = sys.argv[1:]
    flags = {"--out", "--scale", "--crop", "--name"}
    positionals, i, opts = [], 0, {}
    while i < len(argv):
        if argv[i] in flags:
            opts[argv[i]] = argv[i + 1]
            i += 2
        else:
            positionals.append(argv[i])
            i += 1
    src = Path(positionals[0]) if positionals else ROOT / "acr_architecture.excalidraw"
    out = Path(opts["--out"]) if "--out" in opts else src.parent
    scale = float(opts.get("--scale", 0.3))
    doc = json.loads(src.read_text())
    if "--crop" in opts:
        rect = tuple(float(v) for v in opts["--crop"].split(","))
        if len(rect) != 4:
            sys.exit("--crop expects X,Y,W,H")
        doc["elements"] = crop_elements(doc["elements"], rect)
        if not doc["elements"]:
            sys.exit(f"--crop {opts['--crop']} matched no elements")
    suffix = opts.get("--name", "preview")
    out.mkdir(parents=True, exist_ok=True)
    svg_path = out / (src.stem + f".{suffix}.svg")
    png_path = out / (src.stem + f".{suffix}.png")
    svg_path.write_text(render_svg(doc))
    png_path.write_bytes(render_png(doc, scale))
    print(f"wrote {svg_path}\nwrote {png_path}  ({len(doc['elements'])} elements)")


if __name__ == "__main__":
    main()
