"""Classify the lines of each page into book blocks.

Order of business per page: drop furniture, carve out figure/table regions,
peel footnotes off the bottom, pin outline entries to the lines that print
them, then walk the remaining lines top to bottom grouping them into code
blocks, list items and paragraphs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .inline import SOFT, code_lines, first_word_width, join_lines, line_runs, runs_text, tidy_runs
from .model import Block, Code, Heading, Line, ListItem, Note, PageData, Paragraph
from .stats import DocStats
from .textutil import ends_sentence, match_bullet, match_enum, norm_key

STRONG_BULLETS = "•◦▪▫‣⁃∙●○■□▶►"
_CAPTION = re.compile(r"^(Figure|Fig\.|Table|Listing|Example|Exhibit|그림|표)\s*[\dA-Z][\d.\-]*\s*[:.\u2013\u2014|-]\s", re.I)
# "12 Text", "12. Text", "* Text" - but not "2.1 Text" (a numbered exercise or heading)
_NOTE_LABEL = re.compile(r"^\s*(\d{1,3}|[*†‡§¶]+)(?:[.)](?=\s)|(?=\s)|(?=[^\W\d]))\s*")


@dataclass
class OutlineEntry:
    level: int
    title: str
    page: int
    y: float | None = None
    matched: bool = False
    carried: int = 0  # how many following pages it has been retried on


@dataclass
class Context:
    stats: DocStats
    outline: dict[int, list[OutlineEntry]] = field(default_factory=dict)
    notes: list[Note] = field(default_factory=list)
    footnotes: bool = True
    heading_styles: dict[tuple[str, float], dict[int, int]] = field(default_factory=dict)


# --------------------------------------------------------------------------
# line predicates
# --------------------------------------------------------------------------
def is_code_line(ln: Line) -> bool:
    if ln.mono_ratio >= 0.95:
        return True
    if ln.mono_ratio >= 0.5:
        # the non-monospace remainder may only be symbols (prompts, arrows)
        return not any((not s.mono) and any(c.isalpha() for c in s.text) for s in ln.spans)
    return False


def heading_like(ln: Line, st: DocStats) -> bool:
    if ln.mono_ratio > 0.5:
        return False
    if ln.size >= st.body_size + 1.2:
        return True
    return ln.bold_ratio >= 0.85 and ln.size >= st.body_size - 0.25 and len(ln.text) <= 90


# --------------------------------------------------------------------------
# footnotes
# --------------------------------------------------------------------------
def peel_footnotes(ctx: Context, pg: PageData, lines: list[Line]) -> tuple[list[Line], list[Note]]:
    """Split trailing footnote lines off a page.

    Footnotes are the run of smaller-than-body lines at the very bottom whose
    first line opens with a label (number or symbol).
    """
    st = ctx.stats
    if not ctx.footnotes or len(lines) < 2:
        return lines, []
    limit = st.body_size - 0.7
    k = len(lines)
    while k > 0 and lines[k - 1].size <= limit and not is_code_line(lines[k - 1]):
        k -= 1
    # code inside a footnote: allow mono lines once we are inside the small-print zone
    tail = lines[k:]
    if not tail or k == 0:
        return lines, []
    if len(tail) > 12 and len(tail) > 0.45 * len(lines):
        return lines, []  # half a page of small print is not footnotes (exercises, a bibliography)
    # the zone must sit apart from the body above it
    gap = tail[0].baseline - lines[k - 1].baseline
    if gap < 1.15 * st.pitch and lines[k - 1].size > limit and not _NOTE_LABEL.match(tail[0].text):
        return lines, []
    # walk forward to the first labelled line; anything before it is not a note
    starts = [i for i, ln in enumerate(tail) if _is_note_start(ln, tail, i)]
    if not starts or starts[0] != 0:
        return lines, []
    notes: list[Note] = []
    for a, b in zip(starts, starts[1:] + [len(tail)]):
        group = tail[a:b]
        m = _NOTE_LABEL.match(group[0].text)
        label = m.group(1)
        per_line = [line_runs(group[0], pg.links, skip_chars=m.end())]
        per_line += [line_runs(ln, pg.links) for ln in group[1:]]
        runs = join_lines(per_line)
        for r in runs:
            r.sup = False
        for ln in group:
            ln.role = "footnote"
        notes.append(Note(label=label, runs=runs, page=pg.index))
    return lines[:k], notes


def _is_note_start(ln: Line, tail: list[Line], i: int) -> bool:
    m = _NOTE_LABEL.match(ln.text)
    if not m:
        return False
    first = ln.spans[0]
    label = m.group(1)
    raised = first.superscript or first.size < ln.size - 0.5 or first.baseline < ln.baseline - 1.0
    if label.isdigit():
        if i == 0:
            return True
        # later labels: must look like a fresh note, not a wrapped line starting with a number
        prev = tail[i - 1]
        return raised or ln.x0 > prev.x0 + 3 or ln.x0 < prev.x0 - 3
    return True


# --------------------------------------------------------------------------
# outline -> heading lines
# --------------------------------------------------------------------------
def pin_outline(ctx: Context, pg: PageData, lines: list[Line]) -> dict[int, tuple[int, OutlineEntry]]:
    """Find, for each outline entry on this page, the lines that print it.

    Returns {index of first line: (index after last line, entry)}.
    """
    entries = ctx.outline.get(pg.index, [])
    found: dict[int, tuple[int, OutlineEntry]] = {}
    if not entries:
        return found
    st = ctx.stats
    taken: set[int] = set()
    keys = [norm_key(ln.text) for ln in lines]
    for entry in entries:
        want = norm_key(entry.title)
        if not want:
            continue
        best: tuple[float, int, int] | None = None
        for i, ln in enumerate(lines):
            if i in taken or not keys[i]:
                continue
            joined = ""
            for j in range(i, min(i + 4, len(lines))):
                if j in taken:
                    break
                if j > i and (
                    abs(lines[j].size - ln.size) > 0.6
                    or lines[j].baseline - lines[j - 1].baseline > 1.6 * st.pitch_for(ln.size)
                ):
                    break
                joined += keys[j]
                score = _title_score(want, joined)
                if score <= 0:
                    continue
                styled = heading_like(ln, st) or (ln.bold_ratio >= 0.25 and len(ln.text) < 90)
                if not styled and score < 1.0:
                    continue
                score += 0.5 if styled else 0.0
                score += min(0.4, max(0.0, (ln.size - st.body_size) / 20))
                if entry.y is not None:
                    score -= min(0.6, abs(ln.y0 - entry.y) / pg.height)
                if best is None or score > best[0]:
                    best = (score, i, j + 1)
        if best is None and entry.y is not None:
            # text did not match (odd encoding, letter-spacing): take the
            # heading-styled line sitting at the destination, if there is one
            for i, ln in enumerate(lines):
                if i in taken or not heading_like(ln, st):
                    continue
                if -6 <= ln.y0 - entry.y <= 90:
                    j = i + 1
                    while (
                        j < len(lines)
                        and j not in taken
                        and abs(lines[j].size - ln.size) < 0.6
                        and lines[j].font == ln.font
                        and lines[j].baseline - lines[j - 1].baseline < 1.6 * st.pitch_for(ln.size)
                    ):
                        j += 1
                    best = (0.1, i, j)
                    break
        if best is None:
            continue
        _, i, j = best
        # a small "Chapter 3" label right above belongs to the heading
        if i > 0 and (i - 1) not in taken:
            above = lines[i - 1]
            k = keys[i - 1]
            if (
                k
                and len(k) <= 14
                and want.startswith(k)
                and lines[i].baseline - above.baseline < 8 * above.size
                and not want.startswith(keys[i])
            ):
                i -= 1
        entry.matched = True
        found[i] = (j, entry)
        taken.update(range(i, j))
    return found


def _title_score(want: str, got: str) -> float:
    if want == got:
        return 1.0
    if len(got) < 3 or len(want) < 3:
        return 0.0
    short, long = (got, want) if len(got) <= len(want) else (want, got)
    if (long.endswith(short) or long.startswith(short)) and len(short) >= 0.45 * len(long):
        return 0.6 + 0.3 * len(short) / len(long)
    return 0.0


# --------------------------------------------------------------------------
# the flow
# --------------------------------------------------------------------------
class PageFlow:
    def __init__(
        self,
        ctx: Context,
        pg: PageData,
        lines: list[Line],
        bullets: list | None = None,
    ):
        self.ctx = ctx
        self.pg = pg
        self.st = ctx.stats
        self.lines = lines
        self.left = ctx.stats.left[pg.index]
        self.right = ctx.stats.right[pg.index]
        self.blocks: list[Block] = []
        self.measure = self._measures()
        # lines whose bullet is a drawn shape rather than a character
        self.drawn: dict[int, float] = {}
        em = ctx.stats.body_size
        for mark in bullets or []:
            cy = (mark.y0 + mark.y1) / 2
            for ln in lines:
                if ln.y0 <= cy <= ln.y1 and -1 <= ln.x0 - mark.x1 <= 2.2 * em:
                    self.drawn[id(ln)] = mark.x0
                    break

    # -- helpers ------------------------------------------------------------
    def _measures(self) -> dict[int, float]:
        """Right margin that applies to each line.

        Usually the page's, but text set to a narrower measure (quotations,
        admonitions) must be judged against its own margin, or every one of
        its lines looks like it "ended early". A stanza is a run of lines
        at even line spacing (in practice one paragraph); it has its own
        margin when it is inset on both sides alike or at least two of its
        lines end flush.
        """
        st = self.st
        out: dict[int, float] = {}
        stanza: list[Line] = []

        def close() -> None:
            if not stanza:
                return
            right = self.right
            widest = max(ln.x1 for ln in stanza)
            if widest < self.right - 1.5 * st.body_size and len(stanza) >= 2:
                inset = min(ln.x0 for ln in stanza) - self.left
                flush = sum(1 for ln in stanza if widest - ln.x1 < 1.5)
                if flush >= 2 or (inset > st.body_size and abs((self.right - widest) - inset) < 1.5 * st.body_size):
                    right = widest
            for ln in stanza:
                out[id(ln)] = right
            stanza.clear()

        prev = None
        for ln in self.lines:
            if is_code_line(ln):
                close()
                prev = None
                continue
            if prev is not None:
                gap = ln.baseline - prev.baseline
                if gap <= 0 or gap > 1.12 * st.pitch_for(prev.size):
                    close()
            stanza.append(ln)
            prev = ln
        close()
        return out

    def _para_right(self, para: list[Line]) -> float:
        if len(para) >= 2:
            widest = max(ln.x1 for ln in para)
            if widest < self.right - 2 * self.st.body_size:
                return widest
        return self.measure.get(id(para[0]), self.right)

    def _continues(self, para: list[Line], ln: Line, strict: bool = False) -> bool:
        st = self.st
        prev = para[-1]
        gap = ln.baseline - prev.baseline
        pitch = st.pitch_for(para[0].size)
        if gap <= 0.3 * pitch or gap > 1.4 * pitch:
            return False
        code_ish = ln.mono_ratio > 0.5 or prev.mono_ratio > 0.5
        if abs(ln.size - para[0].size) > 1.0 and not code_ish:
            return False
        em = para[0].size
        if ln.x0 < para[0].x0 - 0.3 * em and self._starts_item(para[0]):
            return False  # text back out at the margin does not belong to a list item
        if len(para) == 1:
            dx = ln.x0 - prev.x0
            if dx < -3.5 * em or dx > 9 * em:
                return False
            if strict and abs(dx) > 0.4 * em and abs(-dx - st.parindent) > 1.5:
                return False
        elif abs(ln.x0 - para[1].x0) > 0.4 * em:
            return False
        if self._starts_item(ln, forced_only=True):
            return False
        right = self._para_right(para)
        room = right - prev.x1
        if first_word_width(ln) + 0.25 * em < room - 0.3 * em:
            return False  # the next word would have fitted: the paragraph ended
        if strict and room > 2.5 * em:
            return False
        return True

    def _starts_item(self, ln: Line, forced_only: bool = False) -> bool:
        if id(ln) in self.drawn:
            return True
        text = ln.text
        m = match_bullet(text)
        if m:
            return m.group(1) in STRONG_BULLETS or not forced_only
        if forced_only:
            return False
        return bool(match_enum(text))

    # -- main loop ----------------------------------------------------------
    def run(self, pinned: dict[int, tuple[int, OutlineEntry]]) -> list[Block]:
        lines = self.lines
        st = self.st
        para: list[Line] = []
        i = 0
        n = len(lines)
        while i < n:
            ln = lines[i]
            if i in pinned:
                self._flush(para)
                para = []
                end, entry = pinned[i]
                self._emit_heading(lines[i:end], entry)
                i = end
                continue
            if is_code_line(ln):
                if para and self._continues(para, ln, strict=True):
                    para.append(ln)
                    i += 1
                    continue
                nxt = lines[i + 1] if i + 1 < n and (i + 1) not in pinned else None
                starts_para = (
                    nxt is not None
                    and not is_code_line(nxt)
                    and ln.x1 >= self.right - 2.5 * ln.size
                    and self._continues([ln], nxt, strict=True)
                )
                if not starts_para:
                    self._flush(para)
                    para = []
                    j = i + 1
                    while j < n and j not in pinned and is_code_line(lines[j]):
                        gap = lines[j].baseline - lines[j - 1].baseline
                        if gap > 3.2 * st.pitch_for(ln.size) or gap <= 0 or abs(lines[j].size - ln.size) > 1.6:
                            break
                        j += 1
                    self._emit_code(lines[i:j], lines[j] if j < n else None)
                    i = j
                    continue
            if para and self._continues(para, ln):
                para.append(ln)
            else:
                self._flush(para, ln)
                para = [ln]
            i += 1
        self._flush(para)
        return self.blocks

    # -- emitters -----------------------------------------------------------
    def _emit_heading(self, group: list[Line], entry: OutlineEntry) -> None:
        for ln in group:
            ln.role = "heading"
        runs = join_lines([line_runs(ln, []) for ln in group])
        whole_italic = all(r.italic or not r.text.strip() for r in runs)
        for r in runs:
            r.bold = False
            r.sup = False
            if whole_italic:
                r.italic = False  # the heading style itself, not emphasis
            r.text = r.text.replace(SOFT, "")
        main = max(group, key=lambda l: l.size)
        sig = (main.font, main.size)
        self.ctx.heading_styles.setdefault(sig, {}).setdefault(entry.level, 0)
        self.ctx.heading_styles[sig][entry.level] += 1
        self.blocks.append(
            Heading(page=self.pg.index, y=group[0].y0, y_end=group[-1].y1, level=entry.level, runs=runs, title=entry.title)
        )

    def _emit_code(self, group: list[Line], after: Line | None) -> None:
        st = self.st
        first = group[0]
        # a monospace term at the margin with its synopsis set far to the
        # right below it: emit the term, then the synopsis as its own listing
        if (
            len(group) >= 2
            and first.x1 - first.x0 < 0.5 * (self.right - self.left)
            and all(ln.x0 >= first.x0 + 4.5 * st.body_size for ln in group[1:])
        ):
            self._emit_term(first)
            self._emit_code(group[1:], after)
            return
        # a lone short monospace line directly above indented prose is a
        # definition term, not a listing
        if (
            len(group) == 1
            and after is not None
            and not is_code_line(after)
            and after.x0 >= first.x0 + 1.5 * st.body_size
            and 0 < after.baseline - first.baseline <= 1.3 * st.pitch
            and first.x1 - first.x0 < 0.7 * (self.right - self.left)
        ):
            self._emit_term(first)
            return
        for ln in group:
            ln.role = "code"
        self.blocks.append(
            Code(
                page=self.pg.index,
                y=first.y0,
                y_end=group[-1].y1,
                lines=code_lines(group, st.pitch_for(first.size)),
                size=first.size,
            )
        )

    def _emit_term(self, line: Line) -> None:
        line.role = "term"
        self.blocks.append(
            Paragraph(
                page=self.pg.index,
                y=line.y0,
                y_end=line.y1,
                runs=tidy_runs(line_runs(line, self.pg.links)),
                kind="term",
                indent=self._indent(line.x0),
                right=self.right,
                last_x1=line.x1,
            )
        )

    def _indent(self, x: float) -> int:
        em = self.st.body_size
        off = x - self.left
        if off < 0.9 * em:
            return 0
        return max(1, min(4, int(round(off / (2.2 * em)))))

    def _flush(self, para: list[Line], after: Line | None = None) -> None:
        if not para:
            return
        st = self.st
        pg = self.pg
        first = para[0]
        em = st.body_size
        text0 = first.text

        # a short line sitting directly above more deeply indented prose is
        # the term of a definition list
        if (
            len(para) == 1
            and after is not None
            and after.x0 >= first.x0 + 1.5 * em
            and 0 < after.baseline - first.baseline <= 1.3 * st.pitch
            and first.x1 - first.x0 < 0.7 * (self.right - self.left)
            and not self._starts_item(first)
            and not heading_like(first, st)
            and not ends_sentence(text0)
        ):
            self._emit_term(first)
            return

        # definition list: "TERM      description that wraps
        #                            under the description"
        if len(para) >= 2 and para[1].x0 - first.x0 > 1.5 * em and not self._starts_item(first):
            cut = _gap_split(first, para[1].x0, em)
            if cut is not None:
                term_line, rest_line = cut
                self._emit_term(term_line)
                self._flush([rest_line] + para[1:])
                return

        # list item?
        bullet = match_bullet(text0)
        enum = None if bullet else match_enum(text0)
        hanging = len(para) >= 2 and para[1].x0 > first.x0 + 0.4 * em
        if bullet and bullet.group(1) not in STRONG_BULLETS and len(para) >= 2 and not hanging:
            bullet = None  # a dash opening an ordinary paragraph
        if enum and len(para) >= 2 and not hanging:
            enum = None
        if enum and heading_like(first, st):
            enum = None
        m = bullet or enum
        drawn = self.drawn.get(id(first))
        if m or drawn is not None:
            skip = m.end() if m and drawn is None else 0
            per_line = [line_runs(first, pg.links, skip_chars=skip)] + [line_runs(ln, pg.links) for ln in para[1:]]
            item_x = first.x0 if drawn is None else drawn
            for ln in para:
                ln.role = "list"
            last = para[-1]
            self.blocks.append(
                ListItem(
                    page=pg.index,
                    y=first.y0,
                    y_end=last.y1,
                    runs=join_lines(per_line),
                    marker="\u2022" if drawn is not None else m.group(1),
                    ordered=enum is not None and drawn is None,
                    level=int(round((item_x - self.left) / em)),
                    open_end=self._open_end(para),
                    last_x1=last.x1,
                    right=self._para_right(para),
                )
            )
            return

        runs = join_lines([line_runs(ln, pg.links) for ln in para])
        last = para[-1]
        kind = "p"
        text = runs_text(runs)
        if all(heading_like(ln, st) for ln in para) and len(para) <= 3 and len(text) <= 200:
            kind = "subhead"
        elif _CAPTION.match(text) and len(text) < 400:
            kind = "caption"
        elif (
            len(para) == 1
            and first.x0 - self.left > 2 * em
            and abs((first.x0 - self.left) - (self.right - first.x1)) < 1.5 * em
            and first.x1 - first.x0 < 0.75 * (self.right - self.left)
            and not any(o is not first and abs(o.x0 - first.x0) < 1.0 for o in self.lines)
        ):
            kind = "center"
        for ln in para:
            ln.role = kind
        # where does the paragraph body sit? ignore a first-line indent
        body_x = min(ln.x0 for ln in para[1:]) if len(para) >= 2 else first.x0
        first_indented = False
        if len(para) >= 2:
            first_indented = first.x0 - body_x > 0.4 * em
        elif st.parindent and abs(first.x0 - self.left - st.parindent) < 1.5:
            body_x = self.left
            first_indented = True
        block = Paragraph(
            page=pg.index,
            y=first.y0,
            y_end=last.y1,
            runs=runs,
            kind=kind,
            indent=0 if kind in ("center", "subhead") else self._indent(body_x),
            first_indented=first_indented,
            open_end=self._open_end(para),
            last_x1=last.x1,
            first_word_w=first_word_width(first),
            right=self._para_right(para),
        )
        if kind == "subhead":
            main = max(para, key=lambda l: l.size)
            block.style = (main.font, main.size)
            self.ctx.heading_styles.setdefault(block.style, {}).setdefault(0, 0)
            self.ctx.heading_styles[block.style][0] += 1
        self.blocks.append(block)

    def _open_end(self, para: list[Line]) -> bool:
        """Did the last line run to the margin (so the text may continue on
        the next page)?"""
        last = para[-1]
        return self._para_right(para) - last.x1 < 1.2 * self.st.body_size and not ends_sentence(last.text)


def _gap_split(first: Line, cont_x: float, em: float) -> tuple[Line, Line] | None:
    """Split 'TERM <wide gap> text' where text starts at the continuation x."""
    spans = first.spans
    for i in range(1, len(spans)):
        s = spans[i]
        if not s.text.strip():
            continue
        if abs(s.x0 - cont_x) <= 2.0:
            prev_text = [p for p in spans[:i] if p.text.strip()]
            if not prev_text:
                return None
            if s.x0 - prev_text[-1].x1 < 0.8 * em:
                return None
            head = Line(spans=prev_text if len(prev_text) == i else spans[:i], page=first.page)
            tail = Line(spans=spans[i:], page=first.page)
            return head, tail
        if s.x0 > cont_x + 2.0:
            break
    return None
