"""Raw extraction: PDF pages -> visual lines of styled spans, plus links."""

from __future__ import annotations

import unicodedata
from array import array

import pymupdf

from .model import F_BOLD, F_ITALIC, Line, LinkRect, PageData, Span

_TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP

# Characters MuPDF emits that never help a reader.
_DROP = {"­": "-", "​": "", "﻿": "", " ": " ", " ": " ", "\t": " "}


def _font_name(name: str) -> str:
    """Stable font name: no subset tag, no per-object Type 3 numbering."""
    if name.startswith("Type3"):
        return "Type3"
    if len(name) > 7 and name[6] == "+" and name[:6].isupper():
        return name[7:]
    return name


def _make_span(raw: dict) -> Span | None:
    chars = raw["chars"]
    if not chars:
        return None
    text_parts: list[str] = []
    xs = array("f")
    flags = raw["flags"]
    for ch in chars:
        c = ch["c"]
        if "\U0001d400" <= c <= "\U0001d7ff":
            # Mathematical Alphanumeric Symbols: e-reader fonts rarely have
            # them. Use the plain letter and carry the style as formatting.
            name = unicodedata.name(c, "")
            c = unicodedata.normalize("NFKC", c)
            if "ITALIC" in name:
                flags |= F_ITALIC
            if "BOLD" in name:
                flags |= F_BOLD
        if c in _DROP:
            c = _DROP[c]
            if not c:
                continue
        if ord(c) < 32:
            continue
        text_parts.append(c)
        xs.append(ch["bbox"][0])
    if not text_parts:
        return None
    xs.append(chars[-1]["bbox"][2])
    bbox = raw["bbox"]
    return Span(
        text="".join(text_parts),
        xs=xs,
        y0=bbox[1],
        y1=bbox[3],
        baseline=raw["origin"][1],
        font=_font_name(raw["font"]),
        size=raw["size"],
        flags=flags,
    )


def _respace(spans: list[Span]) -> list[Span]:
    """Restore a word space MuPDF missed where the font changes (typically
    between a maths glyph and the following word)."""
    out: list[Span] = []
    for s in spans:
        if out:
            prev = out[-1]
            gap = s.x0 - prev.x1
            if (
                prev.font != s.font
                and gap > 0.22 * max(prev.size, s.size)
                and abs(prev.baseline - s.baseline) < 0.5
                and prev.text[-1:].isalnum()
                and s.text[:1].isalnum()
            ):
                out.append(_space_between(prev, s.x0))
        out.append(s)
    return out


def _space_between(left: Span, x_to: float) -> Span:
    """Synthetic space joining two fragments that sit on one baseline."""
    return Span(
        text=" ",
        xs=array("f", (left.x1, x_to)),
        y0=left.y0,
        y1=left.y1,
        baseline=left.baseline,
        font=left.font,
        size=left.size,
        flags=left.flags,
    )


def _merge_fragments(fragments: list[list[Span]], page: int) -> list[Line]:
    """Join MuPDF line fragments that share a baseline into visual lines.

    MuPDF starts a new "line" at every wide horizontal gap, which splits
    aligned code and table rows into pieces; readers see one line. Exponents
    and indices arrive as fragments of their own, a little above or below.
    """

    class Row:
        __slots__ = ("parts", "x0", "x1", "y0", "y1", "size")

        def __init__(self, spans: list[Span], satellite: bool = False):
            self.parts = [(spans, satellite)]
            self.x0, self.x1 = spans[0].x0, spans[-1].x1
            self.y0 = min(s.y0 for s in spans)
            self.y1 = max(s.y1 for s in spans)
            self.size = max(spans, key=lambda s: len(s.text)).size

        def clear_of(self, x0: float, x1: float) -> bool:
            """No glyph of this row lies under [x0, x1]."""
            return not any(s.x0 < x1 - 1.0 and s.x1 > x0 + 1.0 for spans, _ in self.parts for s in spans if s.text.strip())

        def gap_to(self, x0: float, x1: float) -> float:
            best = 1e9
            for spans, _ in self.parts:
                for s in spans:
                    best = min(best, max(x0 - s.x1, s.x0 - x1, 0.0))
            return best

        def add(self, other: "Row", satellite: bool) -> None:
            self.parts.extend((spans, satellite or sat) for spans, sat in other.parts)
            self.x0, self.x1 = min(self.x0, other.x0), max(self.x1, other.x1)
            if not satellite:
                self.y0, self.y1 = min(self.y0, other.y0), max(self.y1, other.y1)

    rows = sorted((Row(spans) for spans in fragments), key=lambda r: (round(r.y0 / 2), r.x0))

    # pass 1: pieces of one line, left to right
    lines: list[Row] = []
    for row in rows:
        host = None
        for m in reversed(lines[-12:]):
            overlap = min(row.y1, m.y1) - max(row.y0, m.y0)
            if overlap <= 0.55 * min(row.y1 - row.y0, m.y1 - m.y0):
                continue
            if abs(row.size - m.size) > 0.9 and m.gap_to(row.x0, row.x1) > 1.0 * max(row.size, m.size):
                continue  # a margin note beside a body line, not part of it
            if m.clear_of(row.x0, row.x1):
                host = m
                break
        if host is None:
            lines.append(row)
        else:
            host.add(row, satellite=False)

    # pass 2: small raised or lowered pieces join the line they touch
    final: list[Row] = []
    for row in sorted(lines, key=lambda r: -r.size):
        host = None
        for m in final:
            if row.size >= 0.85 * m.size or m.y1 < row.y0 or m.y0 > row.y1:
                continue
            overlap = min(row.y1, m.y1) - max(row.y0, m.y0)
            if overlap > 0.25 * (row.y1 - row.y0) and m.gap_to(row.x0, row.x1) <= 0.6 * m.size and m.clear_of(row.x0, row.x1):
                host = m
                break
        if host is None:
            final.append(row)
        else:
            host.add(row, satellite=True)

    out: list[Line] = []
    for row in final:
        tagged = sorted(((spans, sat) for spans, sat in row.parts), key=lambda part: part[0][0].x0)
        spans: list[Span] = []
        prev_sat = False
        for part, sat in tagged:
            if spans and not (sat or prev_sat) and not spans[-1].text.endswith(" ") and not part[0].text.startswith(" "):
                spans.append(_space_between(spans[-1], part[0].x0))
            spans.extend(part)
            prev_sat = sat
        out.append(Line(spans=spans, page=page))
    out.sort(key=lambda ln: (ln.baseline, ln.x0))
    return out


def _read_links(page: pymupdf.Page, doc: pymupdf.Document) -> list[LinkRect]:
    out: list[LinkRect] = []
    for link in page.get_links():
        r = link.get("from")
        if r is None:
            continue
        kind = link.get("kind")
        if kind == pymupdf.LINK_URI and link.get("uri"):
            out.append(LinkRect(r.x0, r.y0, r.x1, r.y1, uri=link["uri"]))
        elif kind in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED) and link.get("page", -1) >= 0:
            target = link["page"]
            if target >= len(doc):
                continue
            out.append(
                LinkRect(r.x0, r.y0, r.x1, r.y1, page=target, dest_y=dest_y(doc, target, link.get("to"), kind))
            )
    return out


def dest_y(doc: pymupdf.Document, page_index: int, point, kind: int) -> float:
    """Normalise a link/outline destination to a top-left y on its page.

    Named destinations come back in PDF space (origin bottom-left); GoTo
    links are already in page space.
    """
    if point is None:
        return 0.0
    height = doc[page_index].rect.height
    y = point.y
    if kind == pymupdf.LINK_NAMED:
        y = height - y
    return max(0.0, min(height, y))


def load_page(doc: pymupdf.Document, index: int) -> PageData:
    page = doc[index]
    raw = page.get_text("rawdict", flags=_TEXT_FLAGS)
    fragments: list[list[Span]] = []
    n_chars = 0
    for block in raw["blocks"]:
        if block["type"] != 0:
            continue
        for ln in block["lines"]:
            dx, dy = ln["dir"]
            if abs(dy) > 0.05 or dx < 0.95:  # rotated or mirrored text
                continue
            spans = [s for s in (_make_span(r) for r in ln["spans"]) if s is not None]
            spans = _respace(spans)
            while spans and not spans[0].text.strip():
                spans.pop(0)
            while spans and not spans[-1].text.strip():
                spans.pop()
            if not spans or not any(c not in " \ufffd" for s in spans for c in s.text):
                continue  # nothing but glyphs the PDF gives no meaning for
            n_chars += sum(len(s.text) for s in spans)
            fragments.append(spans)

    rect = page.rect
    scanned = False
    if n_chars < 25:
        area = 0.0
        for info in page.get_image_info():
            b = pymupdf.Rect(info["bbox"]) & rect
            area = max(area, b.get_area())
        scanned = area > 0.5 * rect.get_area()

    return PageData(
        index=index,
        width=rect.width,
        height=rect.height,
        lines=_merge_fragments(fragments, index),
        links=_read_links(page, doc),
        scanned=scanned,
    )
