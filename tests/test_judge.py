"""One answer per question about a line, shared by every stage."""

from __future__ import annotations

from array import array

import pymupdf
import pytest

from pdf2kindle.config import Options
from pdf2kindle.convert import convert
from pdf2kindle.inline import runs_text
from pdf2kindle.judge import caption_label, is_code, is_display_size, is_heading_like
from pdf2kindle.model import F_BOLD, F_MONO, Figure, Line, Paragraph, Span
from pdf2kindle.stats import DocStats


def line(*parts: tuple[str, int], size: float = 10.0) -> Line:
    spans, x = [], 72.0
    for text, flags in parts:
        xs = array("f", [x + i * 5 for i in range(len(text) + 1)])
        spans.append(Span(text, xs, 90, 102, 100, "F", size, flags))
        x = xs[-1]
    return Line(spans=spans, page=0)


@pytest.mark.parametrize(
    "text, strict, loose",
    [
        ("Figure 2.1: The pipeline", "figure", "figure"),
        ("Fig. 3. A plot", "figure", "figure"),
        ("그림 4: 메모리 구조", "figure", "figure"),
        ("표 2-1. 자료형", "table", "table"),
        ("Table A-3 | Results", "table", "table"),
        ("Listing 7: hello.c", "listing", "listing"),
        ("Algorithm 1: K-means", "algorithm", "algorithm"),
        ("Example 2.3: Matrices", "example", "example"),
        ("Figure 2.1 shows where tokens sit.", None, "figure"),  # a sentence, unless it sits under a float
        ("그림 3 메모리", None, "figure"),
        ("Example 3 is instructive", None, None),  # loose never answers for the catch-all words
        ("Figures are numbered per chapter.", None, None),
        ("Tablespoons 2: sugar", None, None),
    ],
)
def test_caption_label(text, strict, loose):
    assert caption_label(text) == strict
    assert caption_label(text, loose=True) == loose


def test_is_code_wants_a_listing_not_prose_with_code_in_it():
    assert is_code(line(("for (auto x : v) {", F_MONO)))
    assert is_code(line(("$ ", 0), ("make install", F_MONO)))  # a symbol prompt in another font
    assert not is_code(line(("TEXTDOMAIN", F_MONO), (" is set.", 0)))
    assert not is_code(line(("Run ", 0), ("make", F_MONO), (" first", 0)))


def test_is_code_with_cjk_text():
    # comments and string literals fall back to the text font for Hangul
    assert is_code(line(("char ch; // ", F_MONO), ("문자", 0)))
    assert is_code(line(('printf("', F_MONO), ("변수 입력", 0), (' : ");', F_MONO)))
    assert is_code(line(("x = 1  # ", F_MONO), ("값", 0)))
    # ... but prose with a lot of inline code is still prose
    assert not is_code(line(('printf("%d", a)', F_MONO), (" 를 봅시다", 0)))
    # a fixed-pitch CJK font is no evidence of code
    assert not is_code(line(("(1) ", 0), ("日文 章魚 怎麼 說?", F_MONO)))


def test_which_openers_make_a_caption_paragraph():
    from pdf2kindle.judge import is_caption_paragraph

    assert is_caption_paragraph("Figure 2.1: The pipeline")
    assert is_caption_paragraph("Example 2.3: Matrices")
    assert not is_caption_paragraph("Algorithm 1: Go through all pairs of cells")
    assert not is_caption_paragraph("Figure 2.1 shows where tokens sit.")


def test_heading_likeness():
    st = DocStats(body_size=10.0)
    assert is_display_size(11.2, 10.0) and not is_display_size(11.0, 10.0)
    assert is_heading_like(line(("Chapter One", 0), size=14.0), st)
    assert is_heading_like(line(("A bold remark", F_BOLD)), st)
    assert not is_heading_like(line(("Plain running text.", 0)), st)
    assert not is_heading_like(line(("int main(void)", F_MONO), size=14.0), st)


def test_korean_caption_groups_the_whole_figure(tmp_path):
    """A figure made of loose strokes plus a label, captioned in Korean, must
    come out as one figure with its caption - the same as an English one."""
    doc = pymupdf.open()
    page = doc.new_page(width=420, height=595)
    y = 60.0
    for i in range(9):
        page.insert_text((50, y), f"Body text line number {i} runs across the page to the margin here.", fontsize=10)
        y += 13
    page.insert_text((150, y + 14), "입력 → 처리 → 출력", fontname="korea", fontsize=9)  # a label above the drawing
    top = y + 30
    page.draw_line((120, top), (300, top + 50))
    page.draw_line((120, top + 50), (300, top))
    page.draw_circle((210, top + 25), 18)
    page.insert_text((140, top + 72), "그림 1: 처리 흐름", fontname="korea", fontsize=9)
    y = top + 100
    for i in range(9):
        page.insert_text((50, y), f"More body text, line {i}, continues below the figure to the margin.", fontsize=10)
        y += 13
    pdf = tmp_path / "ko.pdf"
    doc.save(pdf)

    book = convert(pdf, Options())
    figs = [b for b in book.blocks if isinstance(b, Figure)]
    assert len(figs) == 1
    assert runs_text(figs[0].caption).startswith("그림 1:")
    stray = [runs_text(b.runs) for b in book.blocks if isinstance(b, Paragraph) and "입력" in runs_text(b.runs)]
    assert stray == []  # the label is inside the picture, not left over as text
