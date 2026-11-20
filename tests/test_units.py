from __future__ import annotations

from array import array

from pdf2kindle.config import parse_ranges
from pdf2kindle.inline import SOFT, join_lines, tidy_runs
from pdf2kindle.model import Line, PageData, Run, Span
from pdf2kindle.stats import mark_furniture
from pdf2kindle.textutil import clean_text, is_page_label, match_bullet, match_enum, strip_numbers


def test_parse_ranges():
    assert parse_ranges("1-3,10,98-", 100) == {0, 1, 2, 9, 97, 98, 99}
    assert parse_ranges("-2", 5) == {0, 1}


def test_clean_text_composes_tex_accents_and_ligatures():
    assert clean_text("J¨urgen na¨ıve caf´e") == "Jürgen naïve café"
    assert clean_text("eﬃcient ﬁle") == "efficient file"


def test_page_labels():
    assert is_page_label("12") and is_page_label("xiv") and is_page_label("- 7 -")
    assert not is_page_label("Chapter 7")
    assert strip_numbers("Chapter 12: Running awk 41") == "chapter running awk"


def test_list_markers():
    assert match_bullet("• item").group(1) == "•"
    assert match_enum("12. item").group(1) == "12."
    assert match_enum("(a) item").group(1) == "(a)"
    assert match_enum("1984 was a year") is None


def test_join_lines_marks_soft_hyphens_and_spaces():
    runs = join_lines([[Run("an exam-")], [Run("ple of text")], [Run("and more")]])
    assert "".join(r.text for r in runs) == f"an exam{SOFT}ple of text and more"
    # option names in code keep their hyphen and gain no space
    runs = join_lines([[Run("--dry-", mono=True)], [Run("run", mono=True)]])
    assert "".join(r.text for r in runs) == "--dry-run"
    # a URL broken after a slash is not given a space
    runs = join_lines([[Run("see https://example.org/docs/")], [Run("page.html now")]])
    assert "".join(r.text for r in runs) == "see https://example.org/docs/page.html now"


def test_tidy_runs_moves_spaces_out_of_styled_runs():
    runs = tidy_runs([Run("see "), Run(" the manual ", italic=True), Run("now")])
    assert [(r.text, r.italic) for r in runs] == [("see ", False), ("the manual", True), (" now", False)]


def _line(text: str, y: float, page: int, size: float = 10.0, x: float = 72.0) -> Line:
    xs = array("f", [x + i * 5 for i in range(len(text) + 1)])
    return Line(spans=[Span(text, xs, y - 8, y + 2, y, "Body", size, 0)], page=page)


def test_furniture_spares_footnotes_and_body():
    words = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()
    pages = []
    for i, word in enumerate(words):
        lines = [
            _line(f"My Book    {i + 1}", 40, i, size=8),
            _line(f"The {word} paragraph opens this page in its own way", 90, i),
            _line(f"and {word} continues here for a while", 102, i),
            _line(f"{i % 3 + 1} A footnote about {word} only", 700, i, size=8),
        ]
        pages.append(PageData(index=i, width=612, height=792, lines=lines))
    mark_furniture(pages)
    for pg in pages:
        assert [ln.role for ln in pg.lines] == ["furniture", "", "", ""]


def test_merge_rects_keeps_and_joins():
    import pymupdf

    from pdf2kindle.regions import _merge_rects

    a, b, c = pymupdf.Rect(0, 0, 10, 10), pymupdf.Rect(12, 0, 20, 10), pymupdf.Rect(100, 100, 110, 110)
    merged = _merge_rects([a, b, c], 3.0)
    assert [tuple(r) for r in merged] == [(0, 0, 20, 10), (100, 100, 110, 110)]
