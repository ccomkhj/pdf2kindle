"""Tools for tuning a conversion: font/outline inspection and an annotated
copy of the PDF that shows what every line was taken to be."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pymupdf

from .config import Options

COLORS = {
    "furniture": (0.6, 0.6, 0.6),
    "heading": (0.85, 0.1, 0.1),
    "subhead": (0.9, 0.45, 0.1),
    "code": (0.1, 0.5, 0.1),
    "list": (0.1, 0.35, 0.85),
    "footnote": (0.55, 0.2, 0.7),
    "term": (0.0, 0.6, 0.6),
    "caption": (0.8, 0.6, 0.0),
    "figure": (0.9, 0.1, 0.6),
    "table": (0.5, 0.3, 0.1),
    "equation": (0.0, 0.45, 0.8),
    "center": (0.3, 0.3, 0.3),
    "p": (0.2, 0.2, 0.2),
}


def inspect_pdf(path: Path) -> None:
    from .pdfdoc import load_page
    from .stats import mark_furniture, mark_monospace, measure

    doc = pymupdf.open(str(path))
    pages = [load_page(doc, i) for i in range(len(doc))]
    inferred = mark_monospace(pages)
    furniture = mark_furniture(pages)
    st = measure(pages)
    meta = doc.metadata or {}
    print(f"file      {path}")
    print(f"pages     {len(doc)}  ({pages[0].width:.0f} x {pages[0].height:.0f} pt)")
    print(f"title     {meta.get('title')!r}   author {meta.get('author')!r}")
    print(f"producer  {meta.get('producer')!r} / {meta.get('creator')!r}")
    print(f"body      {st.body_font} {st.body_size:g}pt, line pitch {st.pitch:g}pt, first-line indent {st.parindent:g}pt")
    print(f"furniture {furniture} running head/foot lines")
    scanned = sum(1 for pg in pages if pg.scanned)
    if scanned:
        print(f"scanned   {scanned} page(s) without a text layer")
    fonts: Counter = Counter()
    mono: dict[tuple[str, float], bool] = {}
    for pg in pages:
        for ln in pg.lines:
            for s in ln.spans:
                key = (s.font, round(s.size * 2) / 2)
                fonts[key] += len(s.text)
                mono[key] = s.mono
    print("\nfonts (characters, name, size)")
    for (font, size), n in fonts.most_common(25):
        flag = "  mono" + (" (inferred)" if font in inferred else "") if mono[(font, size)] else ""
        print(f"  {n:>9}  {font} {size:g}{flag}")
    toc = doc.get_toc()
    print(f"\noutline   {len(toc)} entries")
    for level, title, page in toc[:40]:
        print(f"  {'  ' * (level - 1)}{title}  (p.{page})")
    if len(toc) > 40:
        print(f"  ... {len(toc) - 40} more")


def write_overlay(pdf: Path, opts: Options, out: Path) -> None:
    """Re-run the classification and draw it over the original pages."""
    from . import convert as conv

    trace: list = []
    conv.convert(pdf, opts, trace=trace)
    doc = pymupdf.open(str(pdf))
    for pg, region_rects in trace:
        page = doc[pg.index]
        for ln in pg.lines:
            role = ln.role or "p"
            color = COLORS.get(role, (0, 0, 0))
            rect = pymupdf.Rect(ln.x0, ln.y0, ln.x1, ln.y1)
            page.draw_rect(rect, color=color, width=0.5)
            page.insert_text((max(2, ln.x0 - 30), ln.baseline), role[:6], fontsize=5, color=color)
        for kind, rect in region_rects:
            page.draw_rect(rect, color=COLORS.get(kind, (1, 0, 1)), width=1.2, dashes="[3] 0")
            page.insert_text((rect.x0 + 2, rect.y0 + 7), kind, fontsize=6, color=COLORS.get(kind, (1, 0, 1)))
    doc.save(str(out), garbage=3, deflate=True)
