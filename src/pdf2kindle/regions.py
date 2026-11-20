"""Figures, tables and boxes: the parts of a page that are not running text."""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import pymupdf
from PIL import Image

from .config import Options
from .model import Block, Figure, Line, PageData, Table
from .stats import DocStats


@dataclass
class Found:
    blocks: list[Block] = field(default_factory=list)
    boxes: list[pymupdf.Rect] = field(default_factory=list)
    rects: list[tuple[str, pymupdf.Rect]] = field(default_factory=list)  # for the debug overlay


def render(doc: pymupdf.Document, pg: PageData, rect: pymupdf.Rect, opts: Options, text_width: float) -> Figure:
    """Rasterise a page region, sized for a high-density e-ink screen."""
    page = doc[pg.index]
    rect = rect & page.rect
    frac = max(0.05, min(1.0, rect.width / max(text_width, 1.0)))
    zoom = opts.image_width * frac / max(rect.width, 1.0)
    zoom = max(1.0, min(zoom, opts.image_max_dpi / 72))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=rect, alpha=False)
    data, media = encode(pix, opts.jpeg_quality)
    return Figure(
        page=pg.index, y=rect.y0, y_end=rect.y1, data=data, media_type=media, width=pix.width, height=pix.height, frac=frac
    )


def encode(pix: pymupdf.Pixmap, quality: int) -> tuple[bytes, str]:
    """PNG for line art, JPEG for photographs - whichever is smaller without
    hurting flat-colour drawings."""
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if img.getcolors(maxcolors=4096) is not None or _is_gray(img):
        gray = img.convert("L") if _is_gray(img) else img
        buf = io.BytesIO()
        gray.save(buf, "PNG", optimize=True)
        png = buf.getvalue()
        if img.getcolors(maxcolors=4096) is not None:
            return png, "image/png"
        buf = io.BytesIO()
        gray.save(buf, "JPEG", quality=quality, optimize=True)
        jpg = buf.getvalue()
        return (png, "image/png") if len(png) <= 1.3 * len(jpg) else (jpg, "image/jpeg")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue(), "image/jpeg"


def _is_gray(img: Image.Image) -> bool:
    small = img.resize((min(64, img.width), min(64, img.height)))
    for r, g, b in small.getdata():
        if abs(r - g) > 6 or abs(g - b) > 6:
            return False
    return True


def page_image(doc: pymupdf.Document, pg: PageData, opts: Options) -> Figure:
    fig = render(doc, pg, doc[pg.index].rect, opts, pg.width)
    fig.alt = f"Page {pg.index + 1}"
    fig.kind = "page"
    return fig


def find_regions(doc: pymupdf.Document, pg: PageData, lines: list[Line], st: DocStats, opts: Options) -> Found:
    return Found()
