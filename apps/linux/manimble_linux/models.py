"""Engine replies stay plain dicts, keyed exactly as the protocol sends them (``scene["stops"][i]["visible"]``).

These helpers port the computed parts of Models.swift: boxes, glyph and table-cell lookup, character styles.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

CHAR_KINDS = ("italic", "bold", "color", "underline", "strike", "highlight")


@dataclass
class Box:
    """Axis-aligned box in frame pixels, origin top-left, y down."""
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def area(self) -> float:
        return self.w * self.h

    def contains(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1

    @classmethod
    def of(cls, v) -> "Box | None":
        return cls(float(v[0]), float(v[1]), float(v[2]), float(v[3])) if v is not None and len(v) >= 4 else None


def stop_box(stop: dict | None, oid: int) -> Box | None:
    for v in (stop or {}).get("visible", []):
        if int(v[0]) == oid:
            return Box(v[1], v[2], v[3], v[4])
    return None


def glyph_boxes(stop: dict, oid: int, start: int, end: int) -> list[Box] | None:
    """Ink of non-space characters ``start..<end`` of a text object, one box per line it spans."""
    g = (stop.get("glyphs") or {}).get(str(oid))
    if not g or start < 0 or end > len(g) or start >= end:
        return None
    out: list[Box] = []
    for v in g[start:end]:
        if len(v) != 4:
            continue
        b = Box(*v)
        if out:
            last = out[-1]
            if b.x0 >= last.x0 - 1 and last.y0 < b.cy < last.y1:
                last.x1 = max(last.x1, b.x1)
                last.y0 = min(last.y0, b.y0)
                last.y1 = max(last.y1, b.y1)
                continue
        out.append(b)
    return out or None


def table_cells(stop: dict, oid: int) -> list[tuple[int, int, Box, Box]]:
    """(row, col, cell area, entry ink) of each data cell of a table."""
    return [(int(c[0]), int(c[1]), Box(*c[2:6]), Box(*c[6:10]))
            for c in (stop.get("cells") or {}).get(str(oid), []) if len(c) >= 10]


def table_cell_at(stop: dict, oid: int, x: float, y: float):
    for cell in table_cells(stop, oid):
        if cell[2].contains(x, y):
            return cell
    return None


def char_styles(obj: dict) -> dict[str, list]:
    """The formatting set on characters of a text, by kind; kinds it does not report left out."""
    out: dict[str, list] = {}
    for kind, key in (("italic", "italic"), ("bold", "bold"), ("color", "colors")):
        if obj.get(key) is not None:
            out[kind] = obj[key]
    dec = obj.get("decorations")
    if dec is not None:
        for kind in ("underline", "strike", "highlight"):
            out[kind] = dec.get(kind) or []
    return out


def frame_box(obj: dict) -> Box | None:
    f = obj.get("frame")
    return Box.of(f) if f and len(f) == 4 else None


def render_dir(scene: dict | None) -> str | None:
    """The render a scene reply comes from: the basename of its frames' folder (an edit's ``seen``)."""
    stops = (scene or {}).get("stops") or []
    if not stops:
        return None
    return os.path.basename(os.path.dirname(stops[0]["png"]))


def frame_size(scene: dict | None) -> tuple[float, float]:
    s = (scene or {}).get("size") or [1280, 720]
    return float(s[0]), float(s[1])


def unit_px(scene: dict | None) -> float:
    """Frame pixels per manim unit."""
    w, _ = frame_size(scene)
    f = (scene or {}).get("frame") or [14.2222]
    return w / float(f[0])


def was_cancelled(error: str | None) -> bool:
    return bool(error) and str(error).startswith("cancelled")
