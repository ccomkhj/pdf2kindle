"""The pipeline: PDF file -> Book model."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Callable

import pymupdf

from . import regions
from .config import Options, parse_ranges
from .inline import KSOFT, SOFT, join_lines, line_runs, runs_text
from .layout import Context, OutlineEntry, PageFlow, peel_footnotes, pin_outline
from .model import Block, Book, Code, Figure, Heading, Line, ListItem, Note, PageData, Paragraph, Run, Table
from .pdfdoc import dest_y, load_page
from .stats import mark_furniture, mark_monospace, measure
from .textutil import is_page_label

_INDEX_TITLE = re.compile(
    r"^(index|indexes|indices|(subject|concept|general|function|keyword|name|author|variable|command|topic|symbol) index|"
    r"index of [\w ]+)$|^색인$|^찾아보기$",
    re.I,
)
_CONTENTS_TITLE = re.compile(r"^(table of )?contents$|^brief contents$|^short contents$|^목차$|^차례$", re.I)
_LOOSE_CAPTION = re.compile(r"^(Figure|Fig\.|Table|Listing|그림|표)\s*\d", re.I)
_SECTION_NUMBER = re.compile(r"^\s*((?:\d+|[A-Z])(?:\.\d+)*)\.?\s+\S")
_LEADER = re.compile(r"(\.\s*){4,}\s*\S{1,6}\s*$")
_WORD = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*", re.UNICODE)

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

    book = Book()
    book.stats["pages"] = n
    book.stats["pages_read"] = len(wanted)
    scanned = [pg.index + 1 for pg in pages if pg.scanned]
    book.stats["scanned_pages"] = len(scanned)
    if scanned:
        book.warnings.append(
            f"{len(scanned)} page(s) are scanned images with no text layer (first: p.{scanned[0]}); "
            "they are kept as page images. Run OCR (e.g. ocrmypdf) first for reflowable text."
        )

    extra_mono = mark_monospace(pages)
    if extra_mono:
        book.stats["mono_fonts_inferred"] = sorted(extra_mono)
    book.stats["furniture_lines"] = mark_furniture(pages)
    stats = measure(pages)
    book.stats["body_font"] = f"{stats.body_font} {stats.body_size:g}pt"

    toc_pages = _find_toc_pages(pages) if opts.drop_toc_pages else set()
    book.stats["toc_pages_dropped"] = len(toc_pages)

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
        blocks.extend(_page_blocks(ctx, doc, pg, opts, book, trace, wanted - toc_pages))
    book.notes = ctx.notes

    _settle_headings(ctx, blocks, outline, book)
    blocks = _drop_index(blocks, opts, book)
    blocks = _attach_captions(blocks)
    blocks = _merge_across_pages(blocks, stats)
    _link_footnotes(blocks, book)
    _resolve_hyphens(blocks, book)
    _resolve_links(blocks, toc_pages | (set(range(n)) - wanted), book)

    book.blocks = blocks
    _metadata(doc, book, opts, pages)
    _cover(doc, book, opts, wanted)
    _count(book)
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
    book: Book,
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
        held = [b for b in blocks if b.y >= box.y0 - 2 and b.y_end <= box.y1 + 2 and not _is_aside(b)]
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
            book.stats["outline_unmatched"] = book.stats.get("outline_unmatched", 0) + 1
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
def _settle_headings(ctx: Context, blocks: list[Block], outline: list[OutlineEntry], book: Book) -> None:
    """Give font-detected headings (``subhead`` paragraphs) a final role."""
    styles = ctx.heading_styles
    subheads = [b for b in blocks if isinstance(b, Paragraph) and b.kind == "subhead"]
    has_outline = any(e.matched for e in outline)
    level_of: dict[str, int] = {}
    if has_outline:
        # a style the outline uses for level L means level L everywhere
        for (font, size), levels in styles.items():
            real = {lv: c for lv, c in levels.items() if lv > 0}
            if real:
                level_of[f"{font}|{size}"] = max(real.items(), key=lambda kv: kv[1])[0]
    else:
        body = ctx.stats.body_size
        counts: Counter = Counter()
        first_page: dict[str, int] = {}
        for b in subheads:
            counts[b.anchor] += 1
            first_page.setdefault(b.anchor, b.page)
        ranked = sorted(
            (sig for sig in counts if float(sig.rsplit("|", 1)[1]) >= body + 1.2),
            key=lambda sig: -float(sig.rsplit("|", 1)[1]),
        )
        # one-off display type at the very start is the title page, not a chapter
        last_page: dict[str, int] = {}
        for b in subheads:
            last_page[b.anchor] = b.page
        ranked = [sig for sig in ranked if last_page[sig] > 1] or ranked[:1]
        for rank, sig in enumerate(ranked[:4]):
            level_of[sig] = rank + 1
    first_real = min((e.page for e in outline if e.matched), default=0) if has_outline else 0
    # "CHAPTER / ONE" lines printed above a chapter title are a label for it
    labels: set[int] = set()
    for i, b in enumerate(blocks):
        if isinstance(b, Heading) and b.level <= 2 and b.runs:
            j = i - 1
            while j >= 0 and isinstance(blocks[j], Paragraph) and blocks[j].kind == "subhead" and blocks[j].page == b.page:
                if len(runs_text(blocks[j].runs)) > 40:
                    break
                labels.add(j)
                j -= 1
    numbered: tuple[tuple[str, ...], int] | None = None  # last numbered heading: (number, level)
    for i, b in enumerate(blocks):
        if isinstance(b, Heading) and b.runs:
            m = _SECTION_NUMBER.match(runs_text(b.runs))
            if m:
                numbered = (tuple(m.group(1).split(".")), b.level)
        if not (isinstance(b, Paragraph) and b.kind == "subhead"):
            continue
        sig, b.anchor = b.anchor, ""
        level = level_of.get(sig)
        if not level and numbered and has_outline:
            # "2.2.1 Title" below outline entry "2.2 ..." is a real heading
            # one level down, even though the outline stops above it
            m = _SECTION_NUMBER.match(runs_text(b.runs))
            if m:
                number = tuple(m.group(1).split("."))
                base, base_level = numbered
                if len(number) > len(base) and number[: len(base)] == base:
                    level = min(6, base_level + len(number) - len(base))
        if i in labels:
            b.kind = "label"
            for r in b.runs:
                r.bold = False
            continue
        if level and b.page >= first_real:  # display type before the first chapter is the title page
            whole_italic = all(r.italic or not r.text.strip() for r in b.runs)
            for r in b.runs:
                r.bold = False
                if whole_italic:
                    r.italic = False
            blocks[i] = Heading(
                page=b.page, y=b.y, y_end=b.y_end, level=level, runs=b.runs, title=runs_text(b.runs), boxed=b.boxed
            )
    book.stats["outline_entries"] = len(outline)


def _drop_index(blocks: list[Block], opts: Options, book: Book) -> list[Block]:
    if not opts.drop_index:
        return blocks
    out: list[Block] = []
    dropping = 0
    n_pages = book.stats.get("pages", 0)
    for b in blocks:
        if isinstance(b, Heading):
            if dropping and b.level <= dropping:
                dropping = 0
            # an index lives at the back of the book; a section merely named
            # "... index" in the middle does not qualify
            if not dropping and b.page >= 0.6 * n_pages and _INDEX_TITLE.match(b.title.strip()):
                dropping = b.level
                book.stats["index_dropped"] = b.title
        if not dropping:
            out.append(b)
    return out


def _is_aside(b: Block) -> bool:
    return isinstance(b, Paragraph) and b.kind == "aside"


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


def _is_caption(p: Paragraph) -> bool:
    if p.kind == "caption":
        return True
    # directly under a figure, "Figure 3 Title" without punctuation still counts
    text = runs_text(p.runs)
    return p.kind == "p" and len(text) < 160 and bool(_LOOSE_CAPTION.match(text)) and not text.rstrip().endswith(".")


def _attach_captions(blocks: list[Block]) -> list[Block]:
    out: list[Block] = []
    skip = False
    for i, b in enumerate(blocks):
        if skip:
            skip = False
            continue
        if isinstance(b, (Figure, Table)) and not b.caption:
            nxt = blocks[i + 1] if i + 1 < len(blocks) else None
            prev = out[-1] if out else None
            if isinstance(nxt, Paragraph) and _is_caption(nxt) and nxt.page == b.page and nxt.y - b.y_end < 40:
                b.caption = nxt.runs
                skip = True
            elif isinstance(prev, Paragraph) and prev.kind == "caption" and prev.page == b.page and b.y - prev.y_end < 40:
                b.caption = prev.runs
                out.pop()
        out.append(b)
    return out


def _merge_across_pages(blocks: list[Block], stats) -> list[Block]:
    """Rejoin paragraphs, list items and listings split by a page break."""
    out: list[Block] = []
    i = 0
    n = len(blocks)
    while i < n:
        b = blocks[i]
        if out and b.page != out[-1].page:
            # floats (figures, tables) at the page top may sit between the halves
            j = i
            while j < n and (isinstance(blocks[j], (Figure, Table)) or _is_aside(blocks[j])) and blocks[j].page == b.page:
                j += 1
            head = blocks[j] if j < n and blocks[j].page == b.page else None
            # ... and floats may also trail the first half at the page bottom
            k = len(out) - 1
            while k >= 0 and (isinstance(out[k], (Figure, Table)) or _is_aside(out[k])) and out[k].page == out[-1].page:
                k -= 1
            tail = out[k] if k >= 0 else None
            if head is not None and tail is not None and tail.page == head.page - 1 and _joins(tail, head, stats):
                _join(tail, head)
                floats = blocks[i:j]
                out.extend(floats)
                i = j + 1
                continue
        out.append(b)
        i += 1
    return out


def _joins(a: Block, b: Block, stats) -> bool:
    if isinstance(a, Code) and isinstance(b, Code):
        return a.at_page_bottom and b.at_page_top and abs(a.size - b.size) < 0.6
    if isinstance(a, (Paragraph, ListItem)) and isinstance(b, Paragraph):
        if isinstance(a, Paragraph) and a.kind != "p":
            return False
        if b.kind != "p" or not a.open_end or a.boxed != b.boxed:
            return False
        if stats.parindent and b.first_indented:
            return False  # the next page opens a fresh, indented paragraph
        if a.right - a.last_x1 > b.first_word_w + 0.5 * stats.body_size:
            return False
        text = runs_text(b.runs)
        return bool(text)
    return False


def _join(a: Block, b: Block) -> None:
    if isinstance(a, Code):
        a.lines.extend(b.lines)
        a.at_page_bottom = b.at_page_bottom
        return
    from .inline import join_lines

    a.runs = join_lines([a.runs, b.runs])
    a.open_end = b.open_end
    a.last_x1 = b.last_x1
    a.right = b.right


# --------------------------------------------------------------------------
def _iter_runs(blocks: list[Block], notes: list[Note]):
    for b in blocks:
        if isinstance(b, (Paragraph, ListItem, Heading)):
            yield from b.runs
        if isinstance(b, (Figure, Table)):
            yield from b.caption
        if isinstance(b, Table):
            for row in b.rows:
                for cell in row:
                    yield from cell
    for note in notes:
        yield from note.runs


def _link_footnotes(blocks: list[Block], book: Book) -> None:
    """Connect superscript markers in the text to the notes peeled off the
    same page (or the page before/after, for notes that float)."""
    by_page: dict[int, dict[str, int]] = {}
    for idx, note in enumerate(book.notes):
        by_page.setdefault(note.page, {}).setdefault(note.label, idx)
    linked = 0

    def split_label(label: str, page: int) -> list[str] | None:
        """'45' -> ['4', '5'] when two markers were set side by side."""
        known = by_page.get(page, {})
        if not label.isdigit() or len(label) < 2:
            return None
        for cut in range(1, len(label)):
            a, b = label[:cut], label[cut:]
            if a in known and (b in known or split_label(b, page)):
                return [a] + (split_label(b, page) or [b])
        return None

    # same page first, so a "1" on the facing page cannot steal the note
    for offsets in ((0,), (1, -1)):
        for b in blocks:
            if isinstance(b, (Figure, Table)):
                holder, attr = b, "caption"
            elif isinstance(b, (Paragraph, ListItem)):
                holder, attr = b, "runs"
            else:
                continue
            new_runs: list[Run] = []
            for r in getattr(holder, attr):
                if not r.sup or r.note >= 0:
                    new_runs.append(r)
                    continue
                label = r.text.strip().strip(".,)")
                parts = [label]
                for off in offsets:
                    if label not in by_page.get(b.page + off, {}):
                        parts = split_label(label, b.page + off) or parts
                        if len(parts) > 1:
                            break
                made = []
                for part in parts:
                    piece = Run(part, sup=True) if len(parts) > 1 else r
                    for off in offsets:
                        idx = by_page.get(b.page + off, {}).get(part)
                        if idx is not None and not book.notes[idx].referenced:
                            piece.note = idx
                            piece.text = part
                            book.notes[idx].referenced = True
                            linked += 1
                            break
                    made.append(piece)
                if len(parts) > 1 and all(m.note < 0 for m in made):
                    made = [r]
                new_runs.extend(made)
            setattr(holder, attr, new_runs)
    # notes nobody points at still belong in the book: keep them after the
    # last block of their page
    orphans = [n for n in book.notes if not n.referenced]
    for note in orphans:
        at = max((i for i, b in enumerate(blocks) if b.page <= note.page), default=len(blocks) - 1)
        label = Run(note.label + " ", sup=True)
        blocks.insert(at + 1, Paragraph(page=note.page, y=1e9, y_end=1e9, runs=[label] + note.runs, kind="note"))
    book.stats["footnotes"] = len(book.notes)
    book.stats["footnotes_linked"] = linked


def _resolve_hyphens(blocks: list[Block], book: Book) -> None:
    """Decide every line-end hyphen: 'exam-|ple' -> 'example' but
    'command-|line' stays hyphenated, judged by how the book spells the word
    elsewhere."""
    vocab: Counter = Counter()
    runs = list(_iter_runs(blocks, book.notes))
    for r in runs:
        if r.mono:
            continue
        for chunk in r.text.split(SOFT):
            for w in _WORD.findall(chunk):
                vocab[w.lower()] += 1
    joined = kept = 0
    pat = re.compile(r"([^\W\d_]+)" + SOFT + r"([^\W\d_]+)", re.UNICODE)

    def keep_hyphen(a: str, b: str) -> bool:
        nonlocal joined, kept
        solid = vocab.get((a + b).lower(), 0)
        hyph = vocab.get((a + "-" + b).lower(), 0)
        keep = hyph > solid
        if not solid and not hyph:
            # unseen either way: two ordinary words of the book joined by a
            # hyphen ("pattern-matching") are a compound, not a broken word
            keep = len(a) >= 4 and len(b) >= 4 and vocab.get(a.lower(), 0) >= 3 and vocab.get(b.lower(), 0) >= 3
        if keep:
            kept += 1
        else:
            joined += 1
        return keep

    def decide(m: re.Match) -> str:
        a, b = m.group(1), m.group(2)
        return a + ("-" if keep_hyphen(a, b) else "") + b

    for idx, r in enumerate(runs):
        if SOFT not in r.text:
            continue
        if r.text.endswith(SOFT) and idx + 1 < len(runs):
            # the word continues in the next run (style change at the break)
            m = re.match(r"[^\W\d_]+", runs[idx + 1].text, re.UNICODE)
            m0 = re.search(r"[^\W\d_]+$", r.text[:-1], re.UNICODE)
            if m and m0:
                r.text = r.text[:-1] + ("-" if keep_hyphen(m0.group(0), m.group(0)) else "")
        r.text = pat.sub(decide, r.text).replace(SOFT, "")
    book.stats["hyphens_joined"] = joined
    book.stats["hyphens_kept"] = kept
    _resolve_korean_breaks(runs, book)


def _resolve_korean_breaks(runs: list[Run], book: Book) -> None:
    """Korean lines may break inside a word. At each Hangul-to-Hangul line
    break, join the two pieces when the book uses the joined word elsewhere,
    and put a space when it does not."""
    if not any(KSOFT in r.text for r in runs):
        return
    word = re.compile(r"[\uac00-\ud7a3]+")
    vocab: Counter = Counter()
    for r in runs:
        for chunk in r.text.split(KSOFT):
            vocab.update(word.findall(chunk))
    pat = re.compile(r"([\uac00-\ud7a3]+)" + KSOFT + r"([\uac00-\ud7a3]+)")
    joined = spaced = 0

    def decide(m: re.Match) -> str:
        nonlocal joined, spaced
        a, b = m.group(1), m.group(2)
        whole = vocab.get(a + b, 0)
        # joined form attested, and at least as familiar as the two halves
        if whole and whole >= min(vocab.get(a, 0), vocab.get(b, 0)) / 4:
            joined += 1
            return a + b
        spaced += 1
        return a + " " + b

    for idx, r in enumerate(runs):
        if KSOFT not in r.text:
            continue
        text = pat.sub(decide, r.text)
        if text.endswith(KSOFT) and idx + 1 < len(runs):
            a = word.search(text[:-1][::-1])
            b = word.match(runs[idx + 1].text)
            left = a.group(0)[::-1] if a and text[:-1].endswith(a.group(0)[::-1]) else ""
            right = b.group(0) if b else ""
            keep_apart = not (left and right and vocab.get(left + right, 0))
            text = text[:-1] + (" " if keep_apart else "")
        r.text = text.replace(KSOFT, " ")
    book.stats["korean_breaks_joined"] = joined
    book.stats["korean_breaks_spaced"] = spaced


def _resolve_links(blocks: list[Block], missing_pages: set[int], book: Book) -> None:
    """Point internal links at the block sitting at their destination."""
    by_page: dict[int, list[Block]] = {}
    for b in blocks:
        by_page.setdefault(b.page, []).append(b)
    counter = 0
    resolved = dropped = 0
    for r in _iter_runs(blocks, book.notes):
        if r.target is None:
            continue
        page, y = r.target
        r.target = None
        candidates = by_page.get(page)
        if not candidates or page in missing_pages:
            dropped += 1
            continue
        dest = None
        for b in candidates:
            if b.y_end >= y - 2:
                dest = b
                break
        dest = dest or candidates[-1]
        # prefer a heading that starts right there
        for b in candidates:
            if isinstance(b, Heading) and abs(b.y - y) < 30:
                dest = b
                break
        if not dest.anchor:
            counter += 1
            dest.anchor = f"x{counter}"
        r.href = "#" + dest.anchor
        resolved += 1
    book.stats["internal_links"] = resolved
    book.stats["internal_links_dropped"] = dropped


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
    book.language = opts.language or _guess_language(book)


def _guess_language(book: Book) -> str:
    sample = "".join(runs_text(b.runs) for b in book.blocks[:400] if isinstance(b, Paragraph))[:20000]
    if not sample:
        return "en"
    hangul = sum(1 for c in sample if "가" <= c <= "힣")
    kana = sum(1 for c in sample if "぀" <= c <= "ヿ")
    han = sum(1 for c in sample if "一" <= c <= "鿿")
    n = len(sample)
    if hangul > 0.15 * n:
        return "ko"
    if kana > 0.05 * n:
        return "ja"
    if han > 0.2 * n:
        return "zh"
    return "en"


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


def _count(book: Book) -> None:
    kinds = Counter(type(b).__name__ for b in book.blocks)
    book.stats["blocks"] = dict(sorted(kinds.items()))
    book.stats["chapters"] = sum(1 for b in book.blocks if isinstance(b, Heading) and b.level == 1)
    words = 0
    for b in book.blocks:
        if isinstance(b, (Paragraph, ListItem)):
            words += len(runs_text(b.runs).split())
    book.stats["words"] = words
    if book.stats.get("footnotes", 0) > book.stats.get("footnotes_linked", 0):
        missing = book.stats["footnotes"] - book.stats["footnotes_linked"]
        book.warnings.append(f"{missing} footnote(s) had no matching marker in the text; they are listed unlinked.")
    if not book.stats["chapters"]:
        book.warnings.append("No chapter headings were recognised; the book is one long section.")
