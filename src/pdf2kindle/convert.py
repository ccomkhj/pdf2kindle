"""The pipeline: PDF file -> Book model."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

import pymupdf

from . import regions
from .config import Options, parse_ranges
from .finishing import Draft, finish
from .inline import join_lines, line_runs, runs_text
from .layout import Context, OutlineEntry, PageFlow, peel_footnotes, pin_outline
from .model import Block, Book, Code, Heading, Line, PageData, Paragraph
from .pdfdoc import dest_y, load_page
from .stats import mark_furniture, mark_monospace, measure
from .textutil import is_page_label

_CONTENTS_TITLE = re.compile(r"^(table of )?contents$|^brief contents$|^short contents$|^목차$|^차례$", re.I)
_LEADER = re.compile(r"(\.\s*){4,}\s*\S{1,6}\s*$")

# Typesetter droppings that mean nothing once the text reflows.
_ARTIFACTS = [
    r"\(continue[sd] (on|from) (the )?(next|previous) page\)",
    r"(\(continue[sd] (on|from) (the )?(next|previous) page\)\s*){2}",
    r"this page (is )?intentionally (left )?blank\.?",
    r"[\u2500-\u257f\u2012-\u2015_=*~\-]{8,}\ufffd?",  # a ruler typed out of dashes or box-drawing characters
]

Progress = Callable[[str, int, int], None]


def convert(
    pdf_path: str | Path,
    opts: Options | None = None,
    progress: Progress | None = None,
    trace: list | None = None,
) -> Book:
    opts = opts or Options()
    say = progress or (lambda stage, i, n: None)
    doc = pymupdf.open(str(pdf_path))
    if doc.needs_pass:
        raise ValueError("PDF is password protected")
    n = len(doc)

    wanted = parse_ranges(opts.pages, n) if opts.pages else set(range(n))
    wanted -= parse_ranges(opts.skip_pages, n) if opts.skip_pages else set()

    artifact = re.compile("^(?:" + "|".join(_ARTIFACTS + list(opts.drop_lines)) + ")$", re.I)

    # ---- pass 1: raw lines -------------------------------------------------
    pages: list[PageData] = []
    for i in range(n):
        say("read", i + 1, n)
        if i in wanted:
            pg = load_page(doc, i)
            _apply_font_overrides(pg, opts)
            _apply_crop(pg, opts)
            pg.lines = [ln for ln in pg.lines if not artifact.match(ln.text.strip())]
        else:
            r = doc[i].rect
            pg = PageData(index=i, width=r.width, height=r.height, lines=[])
        pages.append(pg)

    info: dict = {"pages": n, "pages_read": len(wanted)}  # what extraction learned; merged into Book.stats
    warnings: list[str] = []
    scanned = [pg.index + 1 for pg in pages if pg.scanned]
    info["scanned_pages"] = len(scanned)
    if scanned:
        warnings.append(
            f"{len(scanned)} page(s) are scanned images with no text layer (first: p.{scanned[0]}); "
            "they are kept as page images. Run OCR (e.g. ocrmypdf) first for reflowable text."
        )

    extra_mono = mark_monospace(pages)
    if extra_mono:
        info["mono_fonts_inferred"] = sorted(extra_mono)
    info["furniture_lines"] = mark_furniture(pages)
    stats = measure(pages)
    info["body_font"] = f"{stats.body_font} {stats.body_size:g}pt"

    toc_pages = _find_toc_pages(pages) if opts.drop_toc_pages else set()
    info["toc_pages_dropped"] = len(toc_pages)

    ctx = Context(stats=stats, footnotes=opts.footnotes)
    outline = _read_outline(doc)
    for entry in outline:
        if entry.page in wanted and entry.page not in toc_pages:
            ctx.outline.setdefault(entry.page, []).append(entry)

    # ---- pass 2: blocks ----------------------------------------------------
    blocks: list[Block] = []
    for pg in pages:
        say("layout", pg.index + 1, n)
        if pg.index not in wanted or pg.index in toc_pages:
            continue
        blocks.extend(_page_blocks(ctx, doc, pg, opts, info, trace, wanted - toc_pages))
    info["outline_entries"] = len(outline)

    # ---- book-level passes (no PDF needed from here on) ----------------------
    placed = [e.page for e in outline if e.matched]
    book = finish(
        Draft(
            blocks=blocks,
            notes=ctx.notes,
            n_pages=n,
            body_size=stats.body_size,
            parindent=stats.parindent,
            heading_styles=ctx.heading_styles,
            first_outline_page=min(placed) if placed else None,
            missing_pages=toc_pages | (set(range(n)) - wanted),
        ),
        opts,
    )
    book.stats = {**info, **book.stats}
    book.warnings = warnings + book.warnings
    _metadata(doc, book, opts, pages)
    _cover(doc, book, opts, wanted)
    doc.close()
    return book


# --------------------------------------------------------------------------
def _apply_font_overrides(pg: PageData, opts: Options) -> None:
    if not opts.mono_fonts and not opts.not_mono_fonts:
        return
    from .model import F_MONO

    for ln in pg.lines:
        for s in ln.spans:
            name = s.font.lower()
            if any(k.lower() in name for k in opts.mono_fonts):
                s.flags |= F_MONO
            if any(k.lower() in name for k in opts.not_mono_fonts):
                s.flags &= ~F_MONO
        ln.recompute()


def _apply_crop(pg: PageData, opts: Options) -> None:
    top, right, bottom, left = (list(opts.crop) + [0, 0, 0, 0])[:4]
    if not (top or right or bottom or left):
        return
    x0, x1 = left * pg.width, pg.width * (1 - right)
    y0, y1 = top * pg.height, pg.height * (1 - bottom)
    pg.lines = [ln for ln in pg.lines if y0 <= ln.baseline <= y1 and ln.x0 >= x0 - 1 and ln.x1 <= x1 + 1]


def _read_outline(doc: pymupdf.Document) -> list[OutlineEntry]:
    out: list[OutlineEntry] = []
    try:
        toc = doc.get_toc(simple=False)
    except Exception:
        return out
    n = len(doc)
    for level, title, page1, info in toc:
        page = page1 - 1
        title = " ".join(str(title).split())
        if not title or page < 0 or page >= n:
            continue
        y = None
        if isinstance(info, dict) and info.get("to") is not None:
            y = dest_y(doc, page, info["to"], info.get("kind", pymupdf.LINK_GOTO))
        out.append(OutlineEntry(level=max(1, min(6, level)), title=title, page=page, y=y))
    return out


def _find_toc_pages(pages: list[PageData]) -> set[int]:
    """Pages that are a printed table of contents: mostly lines ending in a
    page number after dot leaders or a wide gap."""
    limit = max(12, len(pages) // 4)
    share: dict[int, tuple[int, int]] = {}
    for pg in pages[:limit]:
        lines = [ln for ln in pg.lines if ln.role != "furniture"]
        # judge whole rows: the page number is often a separate, smaller piece
        rows: list[list] = []
        for ln in sorted(lines, key=lambda l: (l.baseline, l.x0)):
            if rows and abs(rows[-1][-1].baseline - ln.baseline) < 1.5:
                rows[-1].append(ln)
            else:
                rows.append([ln])
        hits = 0
        for row in rows:
            text = " ".join(ln.text.strip() for ln in row)
            if _LEADER.search(text):
                hits += 1
                continue
            tokens = text.split()
            if len(tokens) < 2 or not is_page_label(tokens[-1]):
                continue
            if len(row) >= 2:
                hits += 1  # the number is a piece of its own
                continue
            last = [s for s in row[0].spans if s.text.strip()]
            if len(last) >= 2 and last[-1].x0 - last[-2].x1 > 0.8 * row[0].size:
                hits += 1
        share[pg.index] = (hits, len(rows))
    titled = {
        pg.index
        for pg in pages[:limit]
        if any(_CONTENTS_TITLE.match(ln.text.strip()) for ln in pg.lines[:6] if ln.role != "furniture")
    }
    strong = sorted(
        i
        for i, (h, n) in share.items()
        if (n >= 5 and h >= 0.5 * n) or (i in titled and n >= 3 and h >= 0.5 * (n - 1))
    )
    if not strong:
        return set()
    # a contents listing runs over consecutive pages; later isolated hits are
    # tables. Its last page may hold only a few lines.
    out = {strong[0]}
    cur = strong[0]
    while True:
        h, n = share.get(cur + 1, (0, 0))
        if n >= 1 and h >= 0.5 * n:
            cur += 1
            out.add(cur)
        else:
            break
    return out


def _page_blocks(
    ctx: Context,
    doc: pymupdf.Document,
    pg: PageData,
    opts: Options,
    info: dict,
    trace: list | None = None,
    wanted_next: set[int] | frozenset = frozenset(),
) -> list[Block]:
    lines = [ln for ln in pg.lines if ln.role != "furniture"]
    if pg.scanned:
        return [regions.page_image(doc, pg, opts)]
    margin_blocks = _margin_notes(ctx, pg, lines)
    lines = [ln for ln in lines if not ln.role]
    found = regions.find_regions(doc, pg, lines, ctx.stats, opts)
    if trace is not None:
        trace.append((pg, found.rects))
    lines = [ln for ln in lines if not ln.role]
    lines.sort(key=lambda ln: (ln.baseline, ln.x0))
    lines, notes = peel_footnotes(ctx, pg, lines)
    ctx.notes.extend(notes)
    pinned = pin_outline(ctx, pg, lines)
    blocks = PageFlow(ctx, pg, lines, bullets=found.bullets).run(pinned)
    blocks.extend(found.blocks)
    blocks.extend(margin_blocks)
    for box in found.boxes:
        held = [
            b
            for b in blocks
            if b.y >= box.y0 - 2 and b.y_end <= box.y1 + 2 and not (isinstance(b, Paragraph) and b.kind == "aside")
        ]
        if any(not isinstance(b, Code) for b in held):  # a frame around a listing adds nothing
            inset = min((b.indent for b in held if isinstance(b, Paragraph)), default=0)
            for b in held:
                b.boxed = True
                if isinstance(b, Paragraph):
                    b.indent -= inset  # the box padding is not an indent
    # outline entries nothing on the page matched: keep them as invisible anchors
    for entry in ctx.outline.get(pg.index, []):
        if not entry.matched:
            # a bookmark often points a little before the page its heading
            # landed on (the end of the previous page, or a float page or
            # two earlier): look ahead before settling for an anchor
            nxt = pg.index + 1
            if entry.carried < 3 and nxt in wanted_next:
                entry.carried += 1
                entry.page = nxt
                entry.y = None
                ctx.outline.setdefault(nxt, []).insert(0, entry)
                continue
            y = entry.y if entry.y is not None else 0.0
            blocks.append(Heading(page=pg.index, y=y - 0.5, y_end=y, level=entry.level, runs=[], title=entry.title))
            info["outline_unmatched"] = info.get("outline_unmatched", 0) + 1
    blocks.sort(key=lambda b: (b.y, b.y_end))
    if blocks:
        top = min(ln.y0 for ln in pg.lines if ln.role != "furniture") if lines else 0
        bottom = max((ln.y1 for ln in pg.lines if ln.role not in ("furniture", "footnote")), default=pg.height)
        for b in blocks:
            if isinstance(b, Code):
                b.at_page_top = b.y - top < 2 * ctx.stats.pitch
                b.at_page_bottom = bottom - b.y_end < 2 * ctx.stats.pitch
    return blocks


# --------------------------------------------------------------------------


def _margin_notes(ctx: Context, pg: PageData, lines: list[Line]) -> list[Block]:
    """Notes set in the outer margin beside the text block.

    They are lifted out of the flow (their lines would otherwise interleave
    with the body) and returned as small asides that follow the paragraph
    they stood next to.
    """
    st = ctx.stats
    left, right = st.left[pg.index], st.right[pg.index]
    em = st.body_size
    side = [ln for ln in lines if not ln.role and (ln.x0 > right + 0.4 * em or ln.x1 < left - 0.4 * em) and ln.size < st.body_size - 0.4]
    if not side:
        return []
    side.sort(key=lambda ln: (ln.baseline, ln.x0))
    groups: list[list[Line]] = []
    for ln in side:
        if groups and 0 < ln.baseline - groups[-1][-1].baseline < 1.6 * st.pitch_for(ln.size):
            groups[-1].append(ln)
        else:
            groups.append([ln])
    out: list[Block] = []
    for group in groups:
        for ln in group:
            ln.role = "margin"
        runs = join_lines([line_runs(ln, pg.links) for ln in group])
        text = runs_text(runs)
        if not text.strip() or is_page_label(text):
            continue
        out.append(Paragraph(page=pg.index, y=group[0].y0, y_end=group[-1].y1, runs=runs, kind="aside"))
    return out


# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
def _metadata(doc: pymupdf.Document, book: Book, opts: Options, pages: list[PageData]) -> None:
    meta = doc.metadata or {}
    title = opts.title or (meta.get("title") or "").strip()
    if not title or title.lower().startswith(("untitled", "microsoft word")):
        # fall back to the largest type on the first pages
        best = None
        for pg in pages[:3]:
            for ln in pg.lines:
                if len(ln.text.strip()) >= 3 and (best is None or ln.size > best.size):
                    best = ln
            if best is not None:
                break  # the first page that has any text is the title page
        title = best.text.strip() if best else Path(doc.name).stem
    book.title = " ".join(title.split())
    book.author = opts.author or (meta.get("author") or "").strip()


def _cover(doc: pymupdf.Document, book: Book, opts: Options, wanted: set[int]) -> None:
    if opts.cover == "none":
        return
    if opts.cover != "auto":
        data = Path(opts.cover).read_bytes()
        book.cover = data
        book.cover_type = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
        return
    if not len(doc):
        return
    page = doc[min(wanted) if wanted else 0]
    zoom = min(3.0, 1600 / max(page.rect.height, 1))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    book.cover = pix.tobytes("jpeg", jpg_quality=88)
    book.cover_type = "image/jpeg"


