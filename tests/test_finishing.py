"""The book-level passes, exercised through their one interface: a ``Draft``
of page-by-page blocks goes in, a finished ``Book`` comes out. No PDF."""

from __future__ import annotations

from pdf2kindle.config import Options
from pdf2kindle.finishing import Draft, finish
from pdf2kindle.inline import KSOFT, SOFT, runs_text
from pdf2kindle.model import Code, Figure, Heading, ListItem, Note, Paragraph, Run


def para(text: str, page: int = 0, y: float = 100.0, **kw) -> Paragraph:
    runs = text if isinstance(text, list) else [Run(text)]
    return Paragraph(page=page, y=y, y_end=y + 10, runs=runs, **kw)


def head(title: str, page: int, level: int = 1, y: float = 50.0) -> Heading:
    return Heading(page=page, y=y, y_end=y + 12, level=level, runs=[Run(title)], title=title)


def texts(book) -> list[str]:
    return [runs_text(b.runs) for b in book.blocks if isinstance(b, (Paragraph, ListItem, Heading))]


def test_only_blocks_are_required():
    book = finish(Draft(blocks=[para("Hello world.")]))
    assert texts(book) == ["Hello world."]
    assert book.language == "en"
    assert book.stats["blocks"] == {"Paragraph": 1}
    assert any("No chapter headings" in w for w in book.warnings)


def test_paragraph_split_by_a_page_break_is_rejoined():
    first = para("The sentence runs to the very edge of the page and", page=0, y=700, open_end=True, last_x1=500, right=500)
    second = para("continues on the next one.", page=1, y=80, first_word_w=40)
    other = para("A new paragraph.", page=1, y=120)
    book = finish(Draft(blocks=[first, second, other]))
    assert texts(book) == ["The sentence runs to the very edge of the page and continues on the next one.", "A new paragraph."]


def test_page_join_skips_a_figure_that_floats_between_the_halves():
    first = para("Text that is cut by the page break and", page=0, y=700, open_end=True, last_x1=500, right=500)
    figure = Figure(page=1, y=60, y_end=200, data=b"x", width=10, height=10)
    second = para("goes on below the figure.", page=1, y=220, first_word_w=30)
    book = finish(Draft(blocks=[first, figure, second]))
    assert [type(b).__name__ for b in book.blocks] == ["Paragraph", "Figure"]
    assert texts(book) == ["Text that is cut by the page break and goes on below the figure."]


def test_a_finished_paragraph_is_not_joined():
    first = para("This one ends here.", page=0, y=700, open_end=False, last_x1=300, right=500)
    second = para("Next page starts fresh.", page=1, y=80, first_word_w=30)
    assert len(finish(Draft(blocks=[first, second])).blocks) == 2


def test_an_indented_first_line_starts_a_new_paragraph_when_the_book_indents():
    first = para("Runs to the edge without a full stop", page=0, y=700, open_end=True, last_x1=500, right=500)
    second = para("Indented opener of the next paragraph.", page=1, y=80, first_word_w=30, first_indented=True)
    assert len(finish(Draft(blocks=[first, second], parindent=15.0)).blocks) == 2
    first.open_end = True
    assert len(finish(Draft(blocks=[first, second], parindent=0.0)).blocks) == 1


def test_listings_split_by_a_page_break_are_rejoined():
    a = Code(page=0, y=600, y_end=700, lines=[[Run("a = 1", mono=True)]], size=9, at_page_bottom=True)
    b = Code(page=1, y=80, y_end=100, lines=[[Run("b = 2", mono=True)]], size=9, at_page_top=True)
    book = finish(Draft(blocks=[a, b]))
    assert len(book.blocks) == 1 and len(book.blocks[0].lines) == 2


def test_line_end_hyphens_follow_the_books_own_spelling():
    blocks = [
        para("An example is an example. The command-line tool has a command-line."),
        para(f"One more exam{SOFT}ple and one more command{SOFT}line."),
    ]
    book = finish(Draft(blocks=blocks))
    assert texts(book)[1] == "One more example and one more command-line."
    assert book.stats["hyphens_joined"] == 1 and book.stats["hyphens_kept"] == 1


def test_korean_line_breaks_follow_the_books_vocabulary():
    blocks = [
        para("프로젝트를 만들고 프로젝트를 저장합니다."),
        para(f"새 프로{KSOFT}젝트를 만들어{KSOFT}봅시다 그리고 봅시다"),
    ]
    book = finish(Draft(blocks=blocks))
    assert texts(book)[1] == "새 프로젝트를 만들어 봅시다 그리고 봅시다"
    assert book.language == "ko"
    assert book.stats["korean_breaks_joined"] == 1 and book.stats["korean_breaks_spaced"] == 1


def test_no_sentinel_survives_in_any_block_kind():
    cap = [Run(f"Figure 1: a cap{SOFT}tion")]
    fig = Figure(page=0, y=10, y_end=90, data=b"x", width=10, height=10, caption=cap)
    item = ListItem(page=0, y=100, y_end=110, runs=[Run(f"an it{SOFT}em")], marker="•")
    book = finish(Draft(blocks=[fig, item, head(f"A head{SOFT}ing", 0, y=120)], notes=[Note("1", [Run(f"a no{SOFT}te")], 0)]))
    everything = runs_text(book.blocks[0].caption) + "".join(texts(book)) + runs_text(book.notes[0].runs)
    assert SOFT not in everything and KSOFT not in everything
    assert "caption" in everything and "item" in everything and "note" in everything


def test_footnote_markers_are_linked_same_page_first():
    notes = [Note("1", [Run("Note on page one.")], 0), Note("1", [Run("Note on page two.")], 1)]
    p0 = para([Run("First"), Run("1", sup=True)], page=0)
    p1 = para([Run("Second"), Run("1", sup=True)], page=1)
    book = finish(Draft(blocks=[p0, p1], notes=notes))
    assert [r.note for b in book.blocks for r in b.runs if r.sup] == [0, 1]
    assert book.stats["footnotes_linked"] == 2 and not book.warnings[:-1]


def test_adjacent_markers_are_split_and_orphans_are_kept():
    notes = [Note("4", [Run("Four.")], 0), Note("5", [Run("Five.")], 0), Note("9", [Run("Nobody points here.")], 0)]
    p0 = para([Run("Text"), Run("45", sup=True)], page=0)
    book = finish(Draft(blocks=[p0], notes=notes))
    assert [r.note for r in book.blocks[0].runs if r.sup] == [0, 1]
    assert book.blocks[1].kind == "note" and "Nobody points here." in runs_text(book.blocks[1].runs)
    assert any("1 footnote(s)" in w for w in book.warnings)


def test_internal_links_point_at_the_block_at_their_destination():
    link = para([Run("see "), Run("Section 2", target=(1, 48.0))], page=0)
    target = head("2 Second", 1, y=50)
    gone = para([Run("lost", target=(7, 10.0))], page=0, y=200)
    book = finish(Draft(blocks=[link, gone, target], missing_pages={7}))
    assert target.anchor and link.runs[1].href == "#" + target.anchor
    assert gone.runs[0].href == "" and gone.runs[0].target is None
    assert book.stats["internal_links"] == 1 and book.stats["internal_links_dropped"] == 1


def test_caption_is_attached_to_the_figure_above_it():
    fig = Figure(page=0, y=10, y_end=90, data=b"x", width=10, height=10)
    cap = para("Figure 3: The pipeline.", y=95, kind="caption")
    loose = para("Figure 4 Another one", y=300)
    fig2 = Figure(page=0, y=200, y_end=290, data=b"x", width=10, height=10)
    book = finish(Draft(blocks=[fig, cap, fig2, loose]))
    assert [type(b).__name__ for b in book.blocks] == ["Figure", "Figure"]
    assert runs_text(book.blocks[0].caption) == "Figure 3: The pipeline."
    assert runs_text(book.blocks[1].caption) == "Figure 4 Another one"


def test_heading_levels_from_type_size_when_there_is_no_outline():
    def cand(text, page, font, size):
        return para(text, page=page, kind="subhead", style=(font, size))

    blocks = [
        cand("The Book Title", 0, "Display", 30.0),  # title page: not a chapter
        cand("One", 2, "Head", 20.0), para("Body.", page=2, y=150),
        cand("Sub", 3, "Head", 14.0),
        cand("Two", 5, "Head", 20.0),
        cand("A bold remark", 5, "Body-Bold", 10.0),
    ]
    book = finish(Draft(blocks=blocks, body_size=10.0))
    got = [(b.level, runs_text(b.runs)) for b in book.blocks if isinstance(b, Heading)]
    assert got == [(1, "One"), (2, "Sub"), (1, "Two")]
    kept = [b for b in book.blocks if isinstance(b, Paragraph) and b.kind == "subhead"]
    assert [runs_text(b.runs) for b in kept] == ["The Book Title", "A bold remark"]
    assert all(b.style is None and b.anchor == "" for b in kept)  # no scratch state left behind
    assert book.stats["chapters"] == 2


def test_with_an_outline_matching_styles_and_section_numbers_decide():
    blocks = [
        para("Display type on the title page", page=0, kind="subhead", style=("Head", 14.0)),
        head("2 Linear Algebra", 4, level=1),
        head("2.1 Systems", 4, level=2, y=200),
        para("2.1.1 Deeper", page=5, kind="subhead", style=("Small", 11.0)),
        para("Unlisted", page=6, kind="subhead", style=("Head", 14.0)),
        para("Just bold", page=6, y=300, kind="subhead", style=("Bold", 10.0)),
    ]
    draft = Draft(blocks=blocks, heading_styles={("Head", 14.0): {2: 9, 0: 2}, ("Small", 11.0): {0: 1}}, first_outline_page=4)
    book = finish(draft)
    got = [(b.level, b.title) for b in book.blocks if isinstance(b, Heading)]
    assert got == [(1, "2 Linear Algebra"), (2, "2.1 Systems"), (3, "2.1.1 Deeper"), (2, "Unlisted")]
    assert [runs_text(b.runs) for b in book.blocks if isinstance(b, Paragraph)] == ["Display type on the title page", "Just bold"]


def test_chapter_label_above_a_chapter_title_becomes_a_label():
    blocks = [para("CHAPTER", page=3, y=40, kind="subhead", style=("Head", 14.0)), head("Articles", 3, y=80)]
    book = finish(Draft(blocks=blocks, heading_styles={("Head", 14.0): {2: 3}}, first_outline_page=3))
    assert book.blocks[0].kind == "label"


def test_index_is_dropped_only_at_the_back_and_through_its_letter_headings():
    blocks = [
        head("Chapter", 10), para("Body.", page=10),
        head("Slicing with range indexes", 40, 2), para("Still body.", page=40),
        head("Index", 90), para("apple, 12", page=90), head("^", 91), para("caret, 3", page=91),
        head("Colophon", 99), para("Set in type.", page=99),
    ]
    book = finish(Draft(blocks=blocks, n_pages=100))
    assert [b.title for b in book.blocks if isinstance(b, Heading)] == ["Chapter", "Slicing with range indexes", "Colophon"]
    assert book.stats["index_dropped"] == "Index"
    kept = finish(Draft(blocks=blocks, n_pages=100), Options(drop_index=False))
    assert sum(isinstance(b, Heading) for b in kept.blocks) == 5
