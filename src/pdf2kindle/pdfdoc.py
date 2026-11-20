"""Raw extraction: PDF pages -> visual lines of styled spans, plus links."""

from __future__ import annotations

from array import array

import pymupdf

from .model import Line, LinkRect, PageData, Span

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
    for ch in chars:
        c = ch["c"]
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
        flags=raw["flags"],
    )


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
    aligned code and table rows into pieces; readers see one line.
    """
    items = []
    for spans in fragments:
        y0 = min(s.y0 for s in spans)
        y1 = max(s.y1 for s in spans)
        items.append([spans, spans[0].x0, spans[-1].x1, y0, y1])
    items.sort(key=lambda it: (round(it[3] / 2), it[1]))

    merged: list[list] = []
    for it in items:
        spans, x0, x1, y0, y1 = it
        host = None
        for m in reversed(merged[-12:]):
            overlap = min(y1, m[4]) - max(y0, m[3])
            if overlap <= 0.55 * min(y1 - y0, m[4] - m[3]):
                continue
            if x0 >= m[2] - 1.0 or x1 <= m[1] + 1.0:
                host = m
                break
        if host is None:
            merged.append([list(spans), x0, x1, y0, y1])
            continue
        if x0 >= host[2] - 1.0:  # append on the right
            if not host[0][-1].text.endswith(" ") and not spans[0].text.startswith(" "):
                host[0].append(_space_between(host[0][-1], x0))
            host[0].extend(spans)
        else:  # prepend on the left
            lead = list(spans)
            if not lead[-1].text.endswith(" ") and not host[0][0].text.startswith(" "):
                lead.append(_space_between(lead[-1], host[1]))
            host[0][:0] = lead
        host[1] = min(host[1], x0)
        host[2] = max(host[2], x1)
        host[3] = min(host[3], y0)
        host[4] = max(host[4], y1)

    lines = [Line(spans=m[0], page=page) for m in merged]
    lines.sort(key=lambda ln: (ln.baseline, ln.x0))
    return lines


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
