"""Book-level finishing passes: page-by-page blocks in, a finished ``Book`` out.

Nothing here touches a PDF. Whatever produced the blocks (the built-in
geometric page analysis, or any other extraction backend) hands over a
``Draft`` and gets back a book whose paragraphs are rejoined across page
breaks, whose headings have their final levels, whose footnotes, internal
links and line-end word breaks are resolved, and whose index is gone.

The passes depend on each other's results, so their order is part of this
module and not something a caller arranges:

1. headings   - font-detected candidates get a level or become plain text;
                must come first, later passes look at heading levels
2. index      - needs final heading levels to find where the index ends
3. captions   - before merging, while a caption still sits next to its figure
4. page joins - before footnotes and words, which work on whole paragraphs
5. footnotes  - markers are matched by page, so before anything renumbers
6. line ends  - hyphens and Korean breaks, judged against the whole book
7. links      - last: anchors are only given to blocks that survived
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .config import Options
from .inline import KSOFT, SOFT, join_lines, runs_text
from .judge import caption_label, is_display_size
from .model import Block, Book, Code, Figure, Heading, ListItem, Note, Paragraph, Run, Table

_INDEX_TITLE = re.compile(
    r"^(index|indexes|indices|(subject|concept|general|function|keyword|name|author|variable|command|topic|symbol) index|"
    r"index of [\w ]+)$|^색인$|^찾아보기$",
    re.I,
)
_SECTION_NUMBER = re.compile(r"^\s*((?:\d+|[A-Z])(?:\.\d+)*)\.?\s+\S")
_WORD = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*", re.UNICODE)


@dataclass
class Draft:
    """What page analysis hands over to the finishing passes.

    Only ``blocks`` is required. The rest are facts about the source that
    some passes use; left at their defaults, those passes simply have less
    to go on (no outline: heading levels come from type size alone).
    """

    blocks: list[Block]
    notes: list[Note] = field(default_factory=list)  # footnotes peeled off the pages
    n_pages: int = 0  # pages in the source ("the back of the book" needs it)
    body_size: float = 10.0  # size of the running text, in points
    parindent: float = 0.0  # first-line indent of running text (0: block paragraphs)
    # display type seen on headings: (font, size) -> {outline level: count};
    # level 0 counts candidates the outline did not name
    heading_styles: dict[tuple[str, float], dict[int, int]] = field(default_factory=dict)
    # page of the first heading the outline placed; None: no usable outline
    first_outline_page: int | None = None
    # pages that were left out; links pointing into them are dropped
    missing_pages: set[int] = field(default_factory=set)


def finish(draft: Draft, opts: Options | None = None) -> Book:
    """Run every book-level pass and return the finished book.

    Fills ``Book.blocks``, ``notes``, ``language`` and the pass-related
    ``stats`` and ``warnings``. Title, author and cover are the caller's.
    """
    opts = opts or Options()
    book = Book(notes=draft.notes)
    blocks = list(draft.blocks)
    _settle_headings(blocks, draft)
    blocks = _drop_index(blocks, draft, opts, book)
    blocks = _attach_captions(blocks)
    blocks = _merge_across_pages(blocks, draft)
    _link_footnotes(blocks, book)
    _resolve_hyphens(blocks, book)
    _resolve_links(blocks, draft.missing_pages, book)
    book.blocks = blocks
    book.language = opts.language or guess_language(book)
    _count(book)
    return book


def _settle_headings(blocks: list[Block], draft: Draft) -> None:
    """Give font-detected headings (``subhead`` paragraphs) a final role."""
    styles = draft.heading_styles
    subheads = [b for b in blocks if isinstance(b, Paragraph) and b.kind == "subhead"]
    has_outline = draft.first_outline_page is not None
    level_of: dict[tuple[str, float], int] = {}
    if has_outline:
        # a style the outline uses for level L means level L everywhere
        for (font, size), levels in styles.items():
            real = {lv: c for lv, c in levels.items() if lv > 0}
            if real:
                level_of[(font, size)] = max(real.items(), key=lambda kv: kv[1])[0]
    else:
        body = draft.body_size
        counts: Counter = Counter()
        first_page: dict[tuple[str, float], int] = {}
        for b in subheads:
            counts[b.style] += 1
            first_page.setdefault(b.style, b.page)
        ranked = sorted((sig for sig in counts if is_display_size(sig[1], body)), key=lambda sig: -sig[1])
        # one-off display type at the very start is the title page, not a chapter
        last_page: dict[tuple[str, float], int] = {}
        for b in subheads:
            last_page[b.style] = b.page
        ranked = [sig for sig in ranked if last_page[sig] > 1] or ranked[:1]
        for rank, sig in enumerate(ranked[:4]):
            level_of[sig] = rank + 1
    first_real = draft.first_outline_page or 0
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
        sig, b.style = b.style, None
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


def _drop_index(blocks: list[Block], draft: Draft, opts: Options, book: Book) -> list[Block]:
    if not opts.drop_index:
        return blocks
    out: list[Block] = []
    dropping = 0
    n_pages = draft.n_pages
    for b in blocks:
        if isinstance(b, Heading):
            # the index ends at the next real heading of its rank; its own
            # initial-letter headings ("A", "^", "~") are part of it
            if dropping and b.level <= dropping and sum(1 for c in b.title if c.isalnum()) >= 4:
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


def _is_caption(p: Paragraph) -> bool:
    if p.kind == "caption":
        return True
    # directly under a figure, "Figure 3 Title" without punctuation still counts
    text = runs_text(p.runs)
    return p.kind == "p" and len(text) < 160 and caption_label(text, loose=True) is not None and not text.rstrip().endswith(".")


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


def _merge_across_pages(blocks: list[Block], draft: Draft) -> list[Block]:
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
            if head is not None and tail is not None and tail.page == head.page - 1 and _joins(tail, head, draft):
                _join(tail, head)
                floats = blocks[i:j]
                out.extend(floats)
                i = j + 1
                continue
        out.append(b)
        i += 1
    return out


def _joins(a: Block, b: Block, draft: Draft) -> bool:
    if isinstance(a, Code) and isinstance(b, Code):
        return a.at_page_bottom and b.at_page_top and abs(a.size - b.size) < 0.6
    if isinstance(a, (Paragraph, ListItem)) and isinstance(b, Paragraph):
        if isinstance(a, Paragraph) and a.kind != "p":
            return False
        if b.kind != "p" or not a.open_end or a.boxed != b.boxed:
            return False
        if draft.parindent and b.first_indented:
            return False  # the next page opens a fresh, indented paragraph
        if a.right - a.last_x1 > b.first_word_w + 0.5 * draft.body_size:
            return False
        text = runs_text(b.runs)
        return bool(text)
    return False


def _join(a: Block, b: Block) -> None:
    if isinstance(a, Code):
        a.lines.extend(b.lines)
        a.at_page_bottom = b.at_page_bottom
        return
    a.runs = join_lines([a.runs, b.runs])
    a.open_end = b.open_end
    a.last_x1 = b.last_x1
    a.right = b.right


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


def guess_language(book: Book) -> str:
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
