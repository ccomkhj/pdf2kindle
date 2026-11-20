"""Figures, tables and boxes: the parts of a page that are not running text."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pymupdf
from PIL import Image

from .config import Options
from .model import Block, Figure, Line, PageData, Table
from .stats import DocStats


BREAK = "\u2028"  # forced line break inside a table cell


@dataclass
class Found:
    blocks: list[Block] = field(default_factory=list)
    boxes: list[pymupdf.Rect] = field(default_factory=list)
    rects: list[tuple[str, pymupdf.Rect]] = field(default_factory=list)  # for the debug overlay
    bullets: list[pymupdf.Rect] = field(default_factory=list)  # list markers drawn as shapes


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
    raw = img.resize((min(64, img.width), min(64, img.height))).tobytes()
    return all(abs(raw[i] - raw[i + 1]) <= 6 and abs(raw[i + 1] - raw[i + 2]) <= 6 for i in range(0, len(raw), 3))


def page_image(doc: pymupdf.Document, pg: PageData, opts: Options) -> Figure:
    fig = render(doc, pg, doc[pg.index].rect, opts, pg.width)
    fig.alt = f"Page {pg.index + 1}"
    fig.kind = "page"
    return fig


def _center_in(ln: Line, r: pymupdf.Rect, pad: float = 1.0) -> bool:
    cx = (ln.x0 + ln.x1) / 2
    cy = (ln.y0 + ln.y1) / 2
    return r.x0 - pad <= cx <= r.x1 + pad and r.y0 - pad <= cy <= r.y1 + pad


def _is_code(ln: Line) -> bool:
    return ln.mono_ratio >= 0.5


def find_regions(doc: pymupdf.Document, pg: PageData, lines: list[Line], st: DocStats, opts: Options) -> Found:
    """Locate figures, tables and drawn boxes; mark the lines they swallow."""
    page = doc[pg.index]
    found = Found()
    left, right = st.left[pg.index], st.right[pg.index]
    text_width = max(right - left, 50.0)
    prect = page.rect

    # ---- raster images -------------------------------------------------------
    pictures: list[pymupdf.Rect] = []
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"]) & prect
        if r.is_empty or r.width < 10 or r.height < 10:
            continue
        if r.get_area() > 0.85 * prect.get_area() and lines:
            continue  # page background behind real text
        pictures.append(r)

    # ---- vector drawings -----------------------------------------------------
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    drawings = [d for d in drawings if not (d["rect"] & prect).is_empty or d["rect"].width * d["rect"].height == 0]
    clusters: list[pymupdf.Rect] = []
    if drawings:
        try:
            clusters = [pymupdf.Rect(r) for r in page.cluster_drawings(drawings=drawings, x_tolerance=4, y_tolerance=4)]
        except Exception:
            clusters = []

    # ---- tables --------------------------------------------------------------
    table_rects: list[pymupdf.Rect] = []
    try:
        tables = page.find_tables().tables if lines else []
    except Exception:
        tables = []
    for t in tables:
        r = pymupdf.Rect(t.bbox)
        inside = [ln for ln in lines if not ln.role and _center_in(ln, r)]
        block = _table(doc, pg, r, inside, drawings, st, opts, text_width) if t.col_count >= 2 and t.row_count >= 2 else None
        if block is None:
            continue
        for ln in inside:
            ln.role = "table"
        table_rects.append(r)
        found.blocks.append(block)
        found.rects.append(("table", r))

    # ---- figures and boxes ---------------------------------------------------
    figure_rects: list[pymupdf.Rect] = list(pictures)
    for c in clusters:
        c = c & prect
        if c.is_empty or (c.width < 3 or c.height < 3):
            continue  # rules, underlines
        if c.width < 14 and c.height < 14:
            continue  # bullets, check marks
        if c.x1 < left - 2 or c.x0 > right + 2:
            continue  # ornaments out in the margin
        if any((c & t).get_area() > 0.5 * min(c.get_area(), t.get_area()) for t in table_rects):
            continue
        members = [d for d in drawings if not (d["rect"] & c).is_empty or c.contains(d["rect"])]
        inside = [ln for ln in lines if not ln.role and _center_in(ln, c)]
        if _is_box(c, members):
            if inside:
                found.boxes.append(c)
                found.rects.append(("box", c))
            continue
        covered = sum((pymupdf.Rect(ln.x0, ln.y0, ln.x1, ln.y1) & c).get_area() for ln in inside)
        if c.width < 0.5 * text_width and covered > 0.4 * c.get_area():
            continue  # a keycap, badge or similar frame drawn around words
        long_lines = sum(1 for ln in inside if ln.x1 - ln.x0 > 0.7 * c.width)
        if long_lines >= 5 and c.width > 0.6 * text_width:
            # running text inside a decorated frame: keep the text as text
            found.boxes.append(c)
            found.rects.append(("box", c))
            continue
        figure_rects.append(c)

    # list bullets that the producer drew as little shapes instead of glyphs
    em = st.body_size
    for d in drawings:
        r = d["rect"]
        if 1.2 <= r.width <= 0.9 * em and 1.2 <= r.height <= 0.9 * em and 0.6 <= r.width / r.height <= 1.7:
            if not any(r.intersects(t) for t in table_rects) and not any(r.intersects(f) for f in figure_rects):
                found.bullets.append(pymupdf.Rect(r))

    figure_rects = _merge_rects(figure_rects, 6.0)
    equation_rects = _equations(lines, st, left, right, table_rects + figure_rects, drawings) if opts.equations else []
    for r in figure_rects:
        # labels that poke out of the drawing still belong to it
        grown = pymupdf.Rect(r)
        for ln in lines:
            if not ln.role and _center_in(ln, r, pad=2.0):
                grown |= pymupdf.Rect(ln.x0, ln.y0, ln.x1, ln.y1)
        grown &= prect
        for ln in lines:
            if not ln.role and _center_in(ln, grown):
                ln.role = "figure"
        pad = pymupdf.Rect(grown.x0 - 2, grown.y0 - 2, grown.x1 + 2, grown.y1 + 2)
        fig = render(doc, pg, pad, opts, text_width)
        found.blocks.append(fig)
        found.rects.append(("figure", grown))
    for r in equation_rects:
        inside = [ln for ln in lines if not ln.role and _center_in(ln, r, pad=0.5)]
        if not inside:
            continue
        for ln in inside:
            ln.role = "equation"
        pad = pymupdf.Rect(r.x0 - 3, r.y0 - 3, r.x1 + 3, r.y1 + 3)
        fig = render(doc, pg, pad, opts, text_width)
        fig.kind = "equation"
        fig.alt = " ".join(" ".join(ln.text.split()) for ln in sorted(inside, key=lambda l: (l.baseline, l.x0)))[:300]
        found.blocks.append(fig)
        found.rects.append(("equation", r))
    return found


_MATH_FONT = re.compile(
    r"^(cmmi|cmsy|cmex|cmbsy|cmmib|msam|msbm|eufm|eusm|eurm|rsfs|lasy|wasy|stmary|esint|mnsymbol|"
    r"txsy|txmi|txex|pxsy|pxmi|pxex|ntxmi|ntxsy|ntxex|newtxmi|mtmi|mtsy|mtex|rpxmi|rtxmi|bbm|dsrom|fourier-m)"
    r"|math|symbol|mt ?extra|euclid",
    re.I,
)
_OPERATORS = set("=+\u2212\u00d7\u00b7\u2264\u2265\u2248\u2260\u2211\u220f\u222b\u221a\u221e\u2208\u2202\u2207\u2192\u2190\u21d2\u2261\u221d\u00b1<>|^")
_EQ_NUMBER = re.compile(r"\(\s*[A-Z]?\d+(\.\d+)*[a-z]?\s*\)\s*$")


def _math_weight(ln: Line) -> tuple[int, int, int]:
    """(characters in mathematical fonts, operator characters, all characters)"""
    math = ops = total = 0
    for s in ln.spans:
        n = sum(1 for c in s.text if not c.isspace())
        total += n
        if _MATH_FONT.search(s.font):
            math += n
        else:
            ops += sum(1 for c in s.text if c in _OPERATORS)
    return math, ops, total


def _core_extent(ln: Line, em: float) -> tuple[float, float]:
    """x extent of a line without a trailing equation number at the margin."""
    pieces = _pieces(ln, em)
    if len(pieces) >= 2 and _EQ_NUMBER.search("(" + pieces[-1].text.strip().strip("()") + ")") and len(pieces[-1].text.strip()) <= 10:
        pieces = pieces[:-1]
    return pieces[0].x0, pieces[-1].x1


def _equations(lines: list[Line], st: DocStats, left: float, right: float, taken: list, drawings: list[dict]) -> list:
    """Displayed equations: set-off lines dominated by mathematical type,
    together with the small fragments that belong to them (limits, the parts
    of a fraction). They are kept as pictures; reflowed, they are unreadable.
    """
    em = st.body_size
    free = [ln for ln in lines if not ln.role and not any(_center_in(ln, t) for t in taken)]
    seeds: list[pymupdf.Rect] = []
    for ln in free:
        math, ops, total = _math_weight(ln)
        if math < 2 or total == 0 or (math + ops) / total < 0.25 or ln.mono_ratio > 0.3:
            continue
        x0, x1 = _core_extent(ln, em)
        if x0 < left + 1.5 * em or x1 > right - 1.0 * em:
            continue  # starts at the margin or runs to it: running text with inline maths
        seeds.append(pymupdf.Rect(ln.x0, ln.y0, ln.x1, ln.y1))
    if not seeds:
        return []
    seeds.sort(key=lambda r: r.y0)
    regions: list[pymupdf.Rect] = []
    for r in seeds:
        if regions and r.y0 - regions[-1].y1 < 0.9 * st.pitch and not _body_between(free, regions[-1], r, left, em):
            regions[-1] |= r
        else:
            regions.append(pymupdf.Rect(r))
    # pull in satellites: limits under a sum, numerators, stacked conditions
    for _ in range(3):
        changed = False
        for reg in regions:
            for ln in free:
                box = pymupdf.Rect(ln.x0, ln.y0, ln.x1, ln.y1)
                if reg.contains(box):
                    continue
                cy = (ln.y0 + ln.y1) / 2
                # limits of a sum or integral sit well clear of the main line
                reach = 1.9 * em if (ln.x1 - ln.x0) < 3 * em and ln.size < st.body_size - 0.5 else 0.9 * em
                if not (reg.y0 - reach <= cy <= reg.y1 + reach):
                    continue
                if ln.x1 < reg.x0 - 2 * em or ln.x0 > reg.x1 + 2 * em:
                    continue
                at_margin = ln.x0 < left + 0.5 * em
                small = ln.size < st.body_size - 0.5 or (ln.x1 - ln.x0) < 6 * em
                inside_band = reg.y0 - 1 <= cy <= reg.y1 + 1
                if inside_band or (small and not at_margin):
                    reg.include_rect(box)
                    changed = True
        if not changed:
            break
    regions = _merge_rects(regions, 1.0)
    for reg in regions:  # fraction bars, radicals
        for d in drawings:
            dr = d["rect"]
            if dr.height < 3 and dr.width < 0.9 * (right - left) and reg.y0 - 2 <= dr.y0 <= reg.y1 + 2 and dr.x0 >= reg.x0 - 3 and dr.x1 <= reg.x1 + 3:
                reg.include_rect(dr)
    return regions


def _body_between(lines: list[Line], a, b, left: float, em: float) -> bool:
    return any(a.y1 <= (ln.y0 + ln.y1) / 2 <= b.y0 and ln.x0 < left + 0.5 * em and ln.x1 - ln.x0 > 8 * em for ln in lines)


def _is_box(c: pymupdf.Rect, members: list[dict]) -> bool:
    """A frame or tinted panel: nothing but a few straight edges hugging the
    cluster's border (or one rectangle filling it)."""
    items = 0
    for d in members:
        r = d["rect"]
        for it in d["items"]:
            items += 1
            if it[0] not in ("l", "re", "qu"):
                return False
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if abs(p1.x - p2.x) > 1 and abs(p1.y - p2.y) > 1:
                    return False  # diagonal stroke
        thin = r.width < 3 or r.height < 3
        fills = r.width > 0.9 * c.width and r.height > 0.9 * c.height
        on_edge = (
            abs(r.x0 - c.x0) < 4 and abs(r.x1 - c.x0) < 4
            or abs(r.x0 - c.x1) < 4 and abs(r.x1 - c.x1) < 4
            or abs(r.y0 - c.y0) < 4 and abs(r.y1 - c.y0) < 4
            or abs(r.y0 - c.y1) < 4 and abs(r.y1 - c.y1) < 4
        )
        if not (fills or (thin and on_edge) or (r.width < 4 and r.height < 4)):
            return False
    return items <= 40


def _merge_rects(rects: list[pymupdf.Rect], tol: float) -> list[pymupdf.Rect]:
    rects = [pymupdf.Rect(r) for r in rects]
    changed = True
    while changed:
        changed = False
        out: list[pymupdf.Rect] = []
        for r in rects:
            for o in out:
                grown = pymupdf.Rect(o.x0 - tol, o.y0 - tol, o.x1 + tol, o.y1 + tol)
                if not (grown & r).is_empty:
                    o.include_rect(r)
                    changed = True
                    break
            else:
                out.append(r)
        rects = out
    return sorted(rects, key=lambda r: (r.y0, r.x0))


# --------------------------------------------------------------------------
# tables
# --------------------------------------------------------------------------
def _table(doc, pg: PageData, r: pymupdf.Rect, inside: list[Line], drawings, st: DocStats, opts: Options, text_width):
    """Turn a detected table region into a ``Table`` when its grid can be
    read with certainty, and into a picture of the region otherwise.

    A wrong cell is worse than an image, so real HTML is only produced for
    (a) fully ruled grids, where the drawn lines give rows and columns, and
    (b) unruled tables whose every row is one text line with cells starting
    on shared left edges.
    """
    if len(inside) < 2:
        return None
    if sum(1 for ln in inside if _is_code(ln)) > 0.4 * len(inside):
        return None  # aligned code or sample output
    inside = sorted(inside, key=lambda ln: (ln.baseline, ln.x0))
    em = st.body_size
    mode = opts.tables

    rows = None
    ys, xs = _grid_lines(r, drawings)
    if len(ys) >= 3 and len(xs) >= 3:
        rows = _grid_rows(inside, ys, xs, em, pg)
    wrapped = False
    if rows is None:
        got = _aligned_rows(inside, em, pg)
        if got is None:
            return None
        rows, clean, wrapped = got
        if not clean or (wrapped and mode != "html"):
            rows = None if mode != "html" or not clean else rows
        if rows is not None and wrapped:
            rows = _fold_continuations(rows)
    n_cols = max((len(row) for row in rows), default=0) if rows else 0
    if mode == "image" or rows is None or n_cols > 6:
        fig = render(doc, pg, pymupdf.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2), opts, text_width)
        fig.kind = "table"
        fig.alt = "Table"
        return fig
    rows = [row + [[] for _ in range(n_cols - len(row))] for row in rows if any(row)]
    if len(rows) < 2:
        return None
    header = all(all(run.bold or run.mono or not run.text.strip() for run in cell) for cell in rows[0] if cell) and any(
        run.bold for cell in rows[0] for run in cell
    )
    if header:
        for cell in rows[0]:
            for run in cell:
                run.bold = False
    return Table(page=pg.index, y=r.y0, y_end=r.y1, rows=rows, header=header)


def _pieces(ln: Line, em: float) -> list[Line]:
    """Split a visual line at wide gaps: one piece per table cell."""
    groups: list[list[int]] = [[]]
    prev = None
    for i, s in enumerate(ln.spans):
        if not s.text.strip():
            continue
        if prev is not None and s.x0 - prev.x1 > 1.2 * em:
            groups.append([])
        groups[-1].append(i)
        prev = s
    return [Line(spans=ln.spans[g[0] : g[-1] + 1], page=ln.page) for g in groups if g]


def _cut_at(piece: Line, anchors: list[float]) -> list[Line]:
    """Cut a piece where a word starts exactly on a column edge: a long cell
    can run right up to its neighbour without a visible gap."""
    cuts: list[tuple[int, int]] = []  # (span index, char index)
    for si, s in enumerate(piece.spans):
        for ci, c in enumerate(s.text):
            if c.isspace():
                continue
            word_start = ci == 0 and (si == 0 or piece.spans[si - 1].text[-1:].isspace() or s.x0 - piece.spans[si - 1].x1 > 1.0)
            word_start = word_start or (ci > 0 and s.text[ci - 1].isspace())
            if not word_start or (si == 0 and ci == 0):
                continue
            if any(abs(s.xs[ci] - a) <= 1.5 for a in anchors):
                cuts.append((si, ci))
    if not cuts:
        return [piece]
    out: list[Line] = []
    start = (0, 0)
    for cut in cuts + [(len(piece.spans), 0)]:
        spans = []
        for si in range(start[0], min(cut[0] + 1, len(piece.spans))):
            s = piece.spans[si]
            lo = start[1] if si == start[0] else 0
            hi = cut[1] if si == cut[0] else len(s.text)
            if hi <= lo:
                continue
            sub = type(s)(
                text=s.text[lo:hi], xs=s.xs[lo : hi + 1], y0=s.y0, y1=s.y1, baseline=s.baseline, font=s.font, size=s.size, flags=s.flags
            )
            spans.append(sub)
        while spans and not spans[-1].text.strip():
            spans.pop()
        if spans:
            out.append(Line(spans=spans, page=piece.page))
        start = cut
    return out


def _aligned_rows(inside: list[Line], em: float, pg: PageData):
    """Rows for an unruled table. Returns (rows, clean, wrapped) or None."""
    from .inline import line_runs, tidy_runs

    per_line = [_pieces(ln, em) for ln in inside]
    edges = sorted(p.x0 for pieces in per_line for p in pieces)
    clusters: list[list[float]] = []
    for x in edges:
        if clusters and x - clusters[-1][-1] <= 2.5:
            clusters[-1].append(x)
        else:
            clusters.append([x])
    anchors = [sum(c) / len(c) for c in clusters if len(c) >= 2]
    if len(anchors) < 2:
        return None
    rows: list[list[list]] = []
    clean = True
    wrapped = False
    last_col_chars: list[int] = []
    for pieces in per_line:
        cells: dict[int, Line] = {}
        for piece in pieces:
            for sub in _cut_at(piece, anchors):
                col = min(range(len(anchors)), key=lambda k: abs(anchors[k] - sub.x0))
                if abs(anchors[col] - sub.x0) > 2.5 or col in cells:
                    clean = False
                    continue
                cells[col] = sub
        if rows and (0 not in cells or (len(cells) == 1 and len(anchors) > 1)):
            wrapped = True  # a cell spilled onto a second line
        rows.append([tidy_runs(line_runs(cells[k], pg.links)) if k in cells else [] for k in range(len(anchors))])
        if len(anchors) - 1 in cells:
            last_col_chars.append(len(cells[len(anchors) - 1].text))
    # two columns of running prose are a definition list, which the text flow
    # renders better than a table would
    if len(anchors) == 2 and last_col_chars and sum(last_col_chars) / len(last_col_chars) > 60:
        return None
    return rows, clean, wrapped


def _grid_lines(r: pymupdf.Rect, drawings: list[dict]) -> tuple[list[float], list[float]]:
    """Positions of ruled lines crossing most of a table: (ys, xs)."""
    horiz: dict[int, float] = {}
    vert: dict[int, float] = {}

    def add(x0: float, y0: float, x1: float, y1: float) -> None:
        if abs(y1 - y0) < 1.5 and abs(x1 - x0) > 3:
            horiz[round((y0 + y1) / 2)] = horiz.get(round((y0 + y1) / 2), 0.0) + abs(x1 - x0)
        elif abs(x1 - x0) < 1.5 and abs(y1 - y0) > 3:
            vert[round((x0 + x1) / 2)] = vert.get(round((x0 + x1) / 2), 0.0) + abs(y1 - y0)

    zone = pymupdf.Rect(r.x0 - 4, r.y0 - 4, r.x1 + 4, r.y1 + 4)
    for d in drawings:
        if (d["rect"] & zone).is_empty and d["rect"].get_area() > 0:
            continue
        for it in d["items"]:
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if zone.contains(p1) and zone.contains(p2):
                    add(p1.x, p1.y, p2.x, p2.y)
            elif it[0] in ("re", "qu"):
                q = it[1].rect if it[0] == "qu" else it[1]
                if not zone.contains(q):
                    continue
                if q.height < 2.5 or q.width < 2.5:
                    add(q.x0, q.y0, q.x1, q.y1) if q.height < 2.5 else add(q.x0, q.y0, q.x0, q.y1)
                else:
                    add(q.x0, q.y0, q.x1, q.y0)
                    add(q.x0, q.y1, q.x1, q.y1)
                    add(q.x0, q.y0, q.x0, q.y1)
                    add(q.x1, q.y0, q.x1, q.y1)

    def strong(found: dict[int, float], need: float) -> list[float]:
        out: list[float] = []
        for pos in sorted(found):
            total = sum(v for k, v in found.items() if abs(k - pos) <= 1)
            if total >= need and (not out or pos - out[-1] > 3):
                out.append(float(pos))
        return out

    return strong(horiz, 0.6 * r.width), strong(vert, 0.6 * r.height)


def _grid_rows(inside: list[Line], ys: list[float], xs: list[float], em: float, pg: PageData):
    from .inline import join_lines, line_runs

    n_rows, n_cols = len(ys) - 1, len(xs) - 1
    grid: list[list[list[Line]]] = [[[] for _ in range(n_cols)] for _ in range(n_rows)]
    for ln in inside:
        for piece in _pieces(ln, em):
            cx = (piece.x0 + piece.x1) / 2
            cy = (piece.y0 + piece.y1) / 2
            ri = next((i for i in range(n_rows) if ys[i] - 1 <= cy <= ys[i + 1] + 1), None)
            ci = next((i for i in range(n_cols) if xs[i] - 1 <= cx <= xs[i + 1] + 1), None)
            if ri is None or ci is None:
                return None
            grid[ri][ci].append(piece)
    from .inline import first_word_width
    from .model import Run

    rows = []
    for row in grid:
        out_row = []
        for ci, cell in enumerate(row):
            cell.sort(key=lambda p: (p.baseline, p.x0))
            # lines that stop short of the cell edge were broken on purpose
            # (one item per line); keep those breaks
            groups: list[list[Line]] = []
            for piece in cell:
                prev = groups[-1][-1] if groups else None
                wrapped = prev is not None and prev.x1 + first_word_width(piece) + 0.3 * em > xs[ci + 1] - 0.5 * em
                if wrapped:
                    groups[-1].append(piece)
                else:
                    groups.append([piece])
            runs: list = []
            for gi, group in enumerate(groups):
                if gi:
                    runs.append(Run(BREAK))
                runs.extend(join_lines([line_runs(p, pg.links) for p in group]))
            out_row.append(runs)
        rows.append(out_row)
    return rows


def _fold_continuations(rows: list) -> list:
    """Rows whose first cell is empty continue the row above (wrapped cells)."""
    from .inline import join_lines

    out: list = []
    for row in rows:
        if out and row and not row[0] and any(row):
            for i, cell in enumerate(row):
                if cell and i < len(out[-1]):
                    out[-1][i] = join_lines([out[-1][i], cell]) if out[-1][i] else cell
            continue
        out.append(row)
    return out
