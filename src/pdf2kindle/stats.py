"""Document-wide measurements every later heuristic leans on."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from .model import F_MONO, PageData
from .textutil import is_page_label, strip_numbers


_MONO_NAME = re.compile(
    r"mono(?!type)|courier|consol|menlo|monaco|inconsolata|source ?code|fira ?code|typewriter|lucida ?cons|"
    r"letter ?gothic|andale|cmtt|cmsltt|cmitt|lmtt|txtt|pcr[a-z]{0,2}\d|nimbusmon|cascadia|pragmata|iosevka|"
    r"terminus|anonymous|ocr-?[ab]|prestige",
    re.I,
)
_NARROW = set("il.,;:'|!")
_WIDE = set("mwMW@")


def mark_monospace(pages: list[PageData]) -> set[str]:
    """Flag fixed-width fonts the PDF does not declare as such.

    Many producers (Word, macOS Quartz, some LaTeX setups) leave the
    fixed-pitch bit off. A font counts as monospace when its name says so or
    when its narrow and wide letters measurably advance by the same amount.
    """
    narrow: dict[str, list[float]] = {}
    wide: dict[str, list[float]] = {}
    declared: set[str] = set()
    for pg in pages:
        for ln in pg.lines:
            for s in ln.spans:
                if s.mono:
                    declared.add(s.font)
                    continue
                if len(narrow.get(s.font, ())) > 60 and len(wide.get(s.font, ())) > 60:
                    continue
                for i, c in enumerate(s.text[:-1]):  # the last advance may be a clipped bbox
                    if c in _NARROW:
                        narrow.setdefault(s.font, []).append((s.xs[i + 1] - s.xs[i]) / s.size)
                    elif c in _WIDE:
                        wide.setdefault(s.font, []).append((s.xs[i + 1] - s.xs[i]) / s.size)
    found: set[str] = set()
    for font in set(narrow) | set(wide):
        a, b = narrow.get(font, []), wide.get(font, [])
        if len(a) >= 6 and len(b) >= 6:
            ma, mb = sorted(a)[len(a) // 2], sorted(b)[len(b) // 2]
            if mb > 0 and abs(ma - mb) / mb < 0.04:
                found.add(font)
        elif _MONO_NAME.search(font):
            found.add(font)
    found -= declared
    if found:
        for pg in pages:
            for ln in pg.lines:
                hit = False
                for s in ln.spans:
                    if s.font in found:
                        s.flags |= F_MONO
                        hit = True
                if hit:
                    ln.recompute()
    return found


@dataclass
class DocStats:
    body_size: float = 10.0
    body_font: str = ""
    pitch: float = 12.0  # baseline-to-baseline distance of body text
    parindent: float = 0.0  # first-line indent of body paragraphs (0 = none)
    left: list[float] = field(default_factory=list)  # body left margin per page
    right: list[float] = field(default_factory=list)  # body right margin per page

    def pitch_for(self, size: float) -> float:
        if abs(size - self.body_size) <= 0.75:
            return self.pitch
        return self.pitch * size / self.body_size


def _mode(counter: Counter, default: float) -> float:
    return counter.most_common(1)[0][0] if counter else default


def measure(pages: list[PageData]) -> DocStats:
    st = DocStats()
    sizes: Counter = Counter()
    fonts: Counter = Counter()
    for pg in pages:
        for ln in pg.lines:
            if ln.role == "furniture" or ln.mono_ratio > 0.5:
                continue
            n = len(ln.text)
            sizes[ln.size] += n
            fonts[(ln.font, ln.size)] += n
    st.body_size = _mode(sizes, 10.0)
    body_fonts = Counter({f: n for (f, s), n in fonts.items() if s == st.body_size})
    st.body_font = _mode(body_fonts, "")

    def is_body(ln) -> bool:
        return ln.role != "furniture" and abs(ln.size - st.body_size) <= 0.75

    pitches: Counter = Counter()
    for pg in pages:
        prev = None
        for ln in pg.lines:
            if is_body(ln):
                if prev is not None:
                    d = ln.baseline - prev.baseline
                    if 0.9 * st.body_size <= d <= 2.2 * st.body_size:
                        pitches[round(d * 4) / 4] += 1
                prev = ln
            else:
                prev = None
    st.pitch = _mode(pitches, st.body_size * 1.2)

    # margins: per page, falling back to the typical value for that parity
    lefts: list[float | None] = []
    rights: list[float | None] = []
    for pg in pages:
        body = [ln for ln in pg.lines if is_body(ln)]
        if len(body) < 6:
            lefts.append(None)
            rights.append(None)
            continue
        x0s = Counter(round(ln.x0) for ln in body)
        need = max(2, len(body) * 0.2)
        common = sorted(x for x, n in x0s.items() if n >= need)
        lefts.append(float(common[0]) if common else None)
        x1s = sorted(ln.x1 for ln in body)
        rights.append(x1s[int(len(x1s) * 0.9)] if len(x1s) >= 6 else None)

    def fill(values: list[float | None], fallback_all: float) -> list[float]:
        by_parity = [Counter(), Counter()]
        for i, v in enumerate(values):
            if v is not None:
                by_parity[i % 2][round(v)] += 1
        out = []
        for i, v in enumerate(values):
            if v is None:
                c = by_parity[i % 2] or by_parity[1 - i % 2]
                v = float(_mode(c, fallback_all))
            out.append(v)
        return out

    width = pages[0].width if pages else 612.0
    st.left = fill(lefts, width * 0.12)
    st.right = fill(rights, width * 0.88)

    # first-line indent: line pairs at normal pitch where the first is inset
    indents: Counter = Counter()
    pairs = 0
    for pg in pages:
        left = st.left[pg.index]
        body = [ln for ln in pg.lines if is_body(ln)]
        for a, b in zip(body, body[1:]):
            if abs((b.baseline - a.baseline) - st.pitch) > 0.12 * st.pitch:
                continue
            if abs(b.x0 - left) > 1.5:
                continue
            pairs += 1
            d = a.x0 - b.x0
            if 0.5 * st.body_size <= d <= 4 * st.body_size:
                indents[round(d)] += 1
    if indents:
        value, count = indents.most_common(1)[0]
        near = sum(n for v, n in indents.items() if abs(v - value) <= 1)
        if near >= 8 and near >= 0.01 * pairs:
            st.parindent = float(value)
    return st


# --------------------------------------------------------------------------
# running headers / footers
# --------------------------------------------------------------------------
def mark_furniture(pages: list[PageData]) -> int:
    """Flag running heads, running feet and page numbers as ``furniture``.

    A band (a y position in the top or bottom margin) is furniture when it is
    the outermost line on many pages and what sits there is either a bare page
    number or text that repeats on other pages once digits are ignored. Body
    text also starts at a constant y, but never repeats, so it survives.
    """
    marked = 0
    for zone in ("top", "bottom"):
        for _ in range(3):  # headers may stack two or three lines deep
            n = _mark_band(pages, zone)
            marked += n
            if n == 0:
                break
    return marked + _mark_stray_folios(pages)


def _mark_stray_folios(pages: list[PageData]) -> int:
    """Page numbers that sit somewhere else on a few pages (typically at the
    foot of chapter openers, when every other page has it in the header).

    A bare number at the page edge is a folio when it follows the numbering
    the running heads already established.
    """
    offsets: Counter = Counter()
    for pg in pages:
        for ln in pg.lines:
            if ln.role != "furniture":
                continue
            text = ln.text.strip()
            value = int(text) if text.isdigit() else _edge_number(text)
            if value is not None:
                offsets[value - pg.index] += 1
    known = {off for off, n in offsets.items() if n >= 3}
    if not known:
        return 0
    marked = 0
    for pg in pages:
        for zone in ("top", "bottom"):
            ln = _outermost(pg, zone)
            if ln is None:
                continue
            text = ln.text.strip()
            if text.isdigit() and len(text) <= 4 and (int(text) - pg.index) in known:
                ln.role = "furniture"
                marked += 1
    return marked


def _outermost(pg: PageData, zone: str):
    live = [ln for ln in pg.lines if ln.role != "furniture"]
    if not live:
        return None
    if zone == "top":
        ln = min(live, key=lambda l: l.baseline)
        return ln if ln.baseline < 0.2 * pg.height else None
    ln = max(live, key=lambda l: l.baseline)
    return ln if ln.baseline > 0.8 * pg.height else None


def _edge_number(text: str) -> int | None:
    """Page-number-like token at the start or end of a line, if any."""
    tokens = text.split()
    if len(tokens) < 2:
        return None
    for tok in (tokens[-1], tokens[0]):
        tok = tok.strip(".,:;|()[]")
        if tok.isdigit() and len(tok) <= 4:
            return int(tok)
    return None


def _numbered(members: list) -> set[int]:
    """Members whose edge number tracks the page count (a folio), as opposed
    to footnote labels or list numbers, which do not advance with the page."""
    offsets: dict[int, int] = {}
    for i, (pg, ln) in enumerate(members):
        value = _edge_number(ln.text)
        if value is not None:
            offsets[i] = value - pg.index
    if len(offsets) < 3:
        return set()
    common, count = Counter(offsets.values()).most_common(1)[0]
    if count < 0.5 * len(offsets):
        return set()
    return {i for i, off in offsets.items() if abs(off - common) <= 1}


def _mark_band(pages: list[PageData], zone: str) -> int:
    edge: list[tuple[PageData, object]] = []
    for pg in pages:
        ln = _outermost(pg, zone)
        if ln is not None:
            edge.append((pg, ln))
    if len(edge) < 3:
        return 0
    n_text_pages = max(1, sum(1 for pg in pages if pg.lines))
    buckets: Counter = Counter(round(ln.baseline / 3) for _, ln in edge)

    marked = 0
    done: set[int] = set()
    for bucket, _count in buckets.most_common():
        if bucket in done:
            continue
        members = [(pg, ln) for pg, ln in edge if abs(round(ln.baseline / 3) - bucket) <= 1]
        done.update((bucket - 1, bucket, bucket + 1))
        if len(members) < 3 or len(members) < 0.25 * n_text_pages:
            continue
        keys = [strip_numbers(ln.text) for _, ln in members]
        seen = Counter(keys)
        folios = _numbered(members)
        hits = 0
        for i, ((_, ln), key) in enumerate(zip(members, keys)):
            if not key or is_page_label(ln.text) or seen[key] >= 2 or i in folios:
                hits += 1
        if hits < 0.6 * len(members):
            continue
        typical = Counter(round(ln.size * 2) / 2 for _, ln in members).most_common(1)[0][0]
        for pg, ln in members:
            if ln.size > typical + 0.75 and not is_page_label(ln.text):  # a real heading parked there
                continue
            for other in pg.lines:
                if other.role != "furniture" and abs(other.baseline - ln.baseline) < 2.0:
                    other.role = "furniture"
                    marked += 1
    return marked
