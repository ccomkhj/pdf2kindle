"""Intermediate representation shared by the extraction and EPUB stages.

Two layers live here:

* *raw* geometry straight from the PDF (``Span`` -> ``Line`` -> ``PageData``)
* the *book* model (``Block`` subclasses with ``Run`` inline content), which is
  what the EPUB writer consumes and carries no page geometry that matters for
  rendering.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field

# MuPDF span flag bits
F_SUPER = 1
F_ITALIC = 2
F_SERIF = 4
F_MONO = 8
F_BOLD = 16


# --------------------------------------------------------------------------
# raw geometry
# --------------------------------------------------------------------------
@dataclass(slots=True)
class Span:
    """A run of characters sharing one font, on one baseline."""

    text: str
    xs: array  # len(text) + 1 floats: left edge of every char, then right edge
    y0: float
    y1: float
    baseline: float
    font: str
    size: float
    flags: int

    @property
    def x0(self) -> float:
        return self.xs[0]

    @property
    def x1(self) -> float:
        return self.xs[-1]

    @property
    def mono(self) -> bool:
        return bool(self.flags & F_MONO)

    @property
    def bold(self) -> bool:
        return bool(self.flags & F_BOLD)

    @property
    def italic(self) -> bool:
        return bool(self.flags & F_ITALIC)

    @property
    def superscript(self) -> bool:
        return bool(self.flags & F_SUPER)


@dataclass(slots=True)
class Line:
    """One visual line of text (all spans sharing a baseline, left to right)."""

    spans: list[Span]
    page: int
    x0: float = 0.0
    x1: float = 0.0
    y0: float = 0.0
    y1: float = 0.0
    baseline: float = 0.0
    size: float = 0.0  # dominant font size (by character count)
    font: str = ""  # dominant font name
    mono_ratio: float = 0.0
    bold_ratio: float = 0.0
    italic_ratio: float = 0.0
    role: str = ""  # set by the classifier, used by the debug overlay

    def __post_init__(self) -> None:
        self.recompute()

    def recompute(self) -> None:
        spans = self.spans
        self.x0 = min(s.x0 for s in spans)
        self.x1 = max(s.x1 for s in spans)
        weight: dict[tuple[str, float], int] = {}
        total = mono = bold = italic = 0
        y0s: list[tuple[int, float]] = []
        for s in spans:
            n = sum(1 for c in s.text if not c.isspace())
            if n == 0:
                continue
            total += n
            key = (s.font, round(s.size * 2) / 2)
            weight[key] = weight.get(key, 0) + n
            if s.mono:
                mono += n
            if s.bold:
                bold += n
            if s.italic:
                italic += n
            y0s.append((n, s.y0))
        if total == 0:
            total = 1
            weight[(spans[0].font, round(spans[0].size * 2) / 2)] = 1
        self.font, self.size = max(weight.items(), key=lambda kv: kv[1])[0]
        dominant = [s for s in spans if (s.font, round(s.size * 2) / 2) == (self.font, self.size)] or spans
        # the baseline most of the dominant text sits on (a synthetic space or
        # an index in the same font must not define it)
        votes: dict[float, int] = {}
        for s in dominant:
            key = round(s.baseline, 1)
            votes[key] = votes.get(key, 0) + sum(1 for c in s.text if not c.isspace())
        self.baseline = max(votes.items(), key=lambda kv: kv[1])[0] if votes else dominant[0].baseline
        self.y0 = min(s.y0 for s in dominant)
        self.y1 = max(s.y1 for s in dominant)
        self.mono_ratio = mono / total
        self.bold_ratio = bold / total
        self.italic_ratio = italic / total

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass(slots=True)
class LinkRect:
    x0: float
    y0: float
    x1: float
    y1: float
    uri: str = ""  # external target
    page: int = -1  # internal target page
    dest_y: float = 0.0  # internal target y (top-left coordinates)


@dataclass(slots=True)
class PageData:
    index: int
    width: float
    height: float
    lines: list[Line]
    links: list[LinkRect] = field(default_factory=list)
    scanned: bool = False


# --------------------------------------------------------------------------
# book model
# --------------------------------------------------------------------------
@dataclass(slots=True)
class Run:
    """Inline text with formatting."""

    text: str
    bold: bool = False
    italic: bool = False
    mono: bool = False
    sup: bool = False
    sub: bool = False
    href: str = ""  # external URL
    target: tuple[int, float] | None = None  # internal link (page, y)
    note: int = -1  # index into Book.notes when this run is a footnote ref

    def same_style(self, other: "Run") -> bool:
        return (
            self.bold == other.bold
            and self.italic == other.italic
            and self.mono == other.mono
            and self.sup == other.sup
            and self.sub == other.sub
            and self.href == other.href
            and self.target == other.target
            and self.note == other.note
        )


@dataclass(slots=True)
class Block:
    page: int = 0
    y: float = 0.0  # top of block on its page (top-left coordinates)
    y_end: float = 0.0
    anchor: str = ""  # element id, set when something links here
    boxed: bool = False  # sits inside a drawn box (sidebar, admonition)


@dataclass(slots=True)
class Heading(Block):
    level: int = 1
    runs: list[Run] = field(default_factory=list)
    toc: bool = True  # appears in the navigation
    title: str = ""  # navigation title


@dataclass(slots=True)
class Paragraph(Block):
    runs: list[Run] = field(default_factory=list)
    indent: int = 0  # left indent level relative to the body margin
    kind: str = "p"  # p | subhead | caption | center
    first_indented: bool = False  # the PDF indented its first line
    open_end: bool = False  # last line ran to the margin (may continue)
    last_x1: float = 0.0
    first_word_w: float = 0.0
    right: float = 0.0  # right text margin where the paragraph sat


@dataclass(slots=True)
class ListItem(Block):
    runs: list[Run] = field(default_factory=list)
    marker: str = ""
    ordered: bool = False
    level: int = 0
    open_end: bool = False
    last_x1: float = 0.0
    right: float = 0.0


@dataclass(slots=True)
class Code(Block):
    lines: list[list[Run]] = field(default_factory=list)
    indent: int = 0
    size: float = 0.0
    at_page_bottom: bool = False
    at_page_top: bool = False


@dataclass(slots=True)
class Figure(Block):
    image: str = ""  # file name inside the EPUB
    data: bytes = b""
    media_type: str = "image/png"
    width: int = 0
    height: int = 0
    frac: float = 1.0  # share of the text width the figure occupied
    caption: list[Run] = field(default_factory=list)
    alt: str = ""
    kind: str = "figure"  # figure | table


@dataclass(slots=True)
class Table(Block):
    rows: list[list[list[Run]]] = field(default_factory=list)  # rows -> cells -> runs
    header: bool = False
    caption: list[Run] = field(default_factory=list)


@dataclass(slots=True)
class Rule(Block):
    pass


@dataclass(slots=True)
class Note:
    """A footnote: body runs plus the label printed in the PDF."""

    label: str
    runs: list[Run]
    page: int
    referenced: bool = False


@dataclass(slots=True)
class Book:
    title: str = ""
    author: str = ""
    language: str = "en"
    blocks: list[Block] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    cover: bytes = b""
    cover_type: str = "image/jpeg"
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
