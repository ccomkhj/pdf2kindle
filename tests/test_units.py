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


def _mono_page(font: str, flags: int = 0) -> PageData:
    """One line of text in ``font`` where every glyph advances by 6pt."""
    text = "will it mill, William? I'm ill. Mmm, www: warm womb."
    xs = array("f", [72 + i * 6.0 for i in range(len(text) + 1)])
    return PageData(index=0, width=612, height=792, lines=[Line(spans=[Span(text, xs, 90, 102, 100, font, 10.0, flags)], page=0)])


def test_monospace_is_inferred_from_glyph_advances():
    from pdf2kindle.stats import mark_monospace

    page = _mono_page("SomeUnflaggedFont")
    assert mark_monospace([page]) == {"SomeUnflaggedFont"}
    assert page.lines[0].mono_ratio == 1.0


def test_proportional_font_is_left_alone():
    from pdf2kindle.stats import mark_monospace

    text = "will it mill, William? I'm ill. Mmm, www: warm womb."
    widths = {"i": 2.8, "l": 2.8, ",": 2.5, ".": 2.5, "'": 2.0, "m": 8.3, "w": 7.2, "W": 9.4}
    xs = array("f", [72.0])
    for c in text:
        xs.append(xs[-1] + widths.get(c, 5.0))
    page = PageData(index=0, width=612, height=792, lines=[Line(spans=[Span(text, xs, 90, 102, 100, "Serif", 10.0, 0)], page=0)])
    assert mark_monospace([page]) == set()


def test_monospace_by_name_when_too_little_text_to_measure():
    from pdf2kindle.stats import mark_monospace

    xs = array("f", [72 + i * 6.0 for i in range(4)])
    page = PageData(index=0, width=612, height=792, lines=[Line(spans=[Span("x=1", xs, 90, 102, 100, "Consolas", 10.0, 0)], page=0)])
    assert mark_monospace([page]) == {"Consolas"}


def _raw_span(text: str, x: float, widths: list[float], gaps: list[float], font: str = "F", size: float = 10.0) -> dict:
    chars = []
    for c, w, g in zip(text, widths, gaps):
        x += g
        chars.append({"c": c, "bbox": (x, 90.0, x + w, 102.0), "origin": (x, 100.0)})
        x += w
    return {"chars": chars, "bbox": (chars[0]["bbox"][0], 90.0, x, 102.0), "origin": (chars[0]["bbox"][0], 100.0), "font": font, "size": size, "flags": 0}


def test_korean_word_spaces_come_from_glyph_gaps():
    from pdf2kindle.pdfdoc import _make_span

    # "새프로젝트를만드는" printed as three words with no space glyphs
    text = "새프로젝트를만드는"
    gaps = [0, 3.0, 0, 0, 0, 0, 3.0, 0, 0]
    span = _make_span(_raw_span(text, 72.0, [10.0] * len(text), gaps))
    assert span.text == "새 프로젝트를 만드는"
    assert len(span.xs) == len(span.text) + 1


def test_maths_alphanumerics_become_styled_letters():
    from pdf2kindle.pdfdoc import _make_span

    span = _make_span(_raw_span("\U0001d44b", 72.0, [7.0], [0]))  # MATHEMATICAL ITALIC CAPITAL X
    assert span.text == "X" and span.italic


def test_korean_line_breaks_use_the_books_vocabulary():
    from pdf2kindle.convert import _resolve_korean_breaks
    from pdf2kindle.inline import KSOFT
    from pdf2kindle.model import Book

    runs = [
        Run("프로젝트를 만들고 프로젝트를 저장합니다."),
        Run(f"새 프로{KSOFT}젝트를 만들어{KSOFT}봅시다 그리고 봅시다"),
    ]
    book = Book()
    _resolve_korean_breaks(runs, book)
    assert runs[1].text == "새 프로젝트를 만들어 봅시다 그리고 봅시다"
    assert book.stats["korean_breaks_joined"] == 1 and book.stats["korean_breaks_spaced"] == 1


def test_river_columns_handle_centred_cells():
    from pdf2kindle.regions import _river_columns

    def row(cells, y):
        spans = []
        for text, x in cells:
            xs = array("f", [x + i * 5 for i in range(len(text) + 1)])
            spans.append(Span(text, xs, y - 8, y + 2, y, "Body", 10.0, 0))
        return Line(spans=spans, page=0)

    rows = [row([("name", 100), ("country", 190), ("rating", 290)], 100), row([("Friends", 92), ("United", 180), ("States", 214), ("7.8", 297)], 114)]
    cols = _river_columns(rows, 10.0)
    assert len(cols) == 3
