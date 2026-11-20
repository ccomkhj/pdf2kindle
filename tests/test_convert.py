"""End-to-end checks on the committed fixture (a small Chromium-printed book
with no PDF outline) and its twin that carries one."""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from pdf2kindle.config import Options
from pdf2kindle.convert import convert
from pdf2kindle.epub import write_epub
from pdf2kindle.inline import runs_text
from pdf2kindle.model import Code, Figure, Heading, ListItem, Paragraph, Table

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module", params=["sample.pdf", "sample-outline.pdf"])
def book(request):
    return convert(FIXTURES / request.param, Options())


def _text(block) -> str:
    return runs_text(block.runs)


def test_headings_and_levels(book):
    heads = [(b.level, _text(b)) for b in book.blocks if isinstance(b, Heading)]
    assert heads == [
        (1, "1. Reading Characters"),
        (2, "1.1 Whitespace and Comments"),
        (1, "2. Tokens"),
        (2, "2.1 A Tokenizer in Twenty Lines"),
    ]


def test_running_heads_and_page_numbers_are_gone(book):
    for b in book.blocks:
        if isinstance(b, Paragraph):
            text = _text(b).strip()
            assert text != "Tiny Parsers" or b.page == 0  # title page keeps its title
            assert not text.isdigit()


def test_paragraph_lines_are_rejoined(book):
    first = next(b for b in book.blocks if isinstance(b, Paragraph) and _text(b).startswith("A parser begins"))
    text = _text(first)
    assert "\n" not in text
    assert text.endswith("straightforwardly right.")  # hyphenated across a line end in the PDF
    assert "one character at a time, deciding where each token starts" in text


def test_code_keeps_indentation_and_alignment(book):
    code = next(b for b in book.blocks if isinstance(b, Code))
    lines = ["".join(r.text for r in line) for line in code.lines]
    assert lines[0] == "class Reader:"
    assert lines[1] == "    def __init__(self, text):"
    assert lines[2] == "        self.text = text"
    assert '        return ""          # end of input' in lines
    assert "" in lines  # blank lines inside the listing survive


def test_inline_code_and_links(book):
    para = next(b for b in book.blocks if isinstance(b, Paragraph) and "peek()" in _text(b))
    assert any(r.mono and r.text == "peek()" for r in para.runs)
    linked = [r for b in book.blocks if isinstance(b, Paragraph) for r in b.runs if r.href]
    assert any(r.href.startswith("https://docs.python.org/") and r.text == "language reference" for r in linked)


def test_lists(book):
    items = [b for b in book.blocks if isinstance(b, ListItem)]
    bullets = [b for b in items if not b.ordered]
    numbered = [b for b in items if b.ordered]
    assert len(bullets) == 5 and len(numbered) == 3
    assert [b.marker for b in numbered] == ["1.", "2.", "3."]
    assert len({b.level for b in bullets}) == 2  # the nested level is kept
    assert _text(bullets[1]).endswith("actually helps the person reading it;")


def test_table_is_real_markup(book):
    table = next(b for b in book.blocks if isinstance(b, Table))
    cells = [[runs_text(c) for c in row] for row in table.rows]
    assert table.header
    assert cells[0] == ["Kind", "Example", "Meaning"]
    assert cells[2] == ["NAME", "total", "An identifier, looked up in the current environment when evaluated"]
    assert len(cells) == 4


def test_figures_with_captions(book):
    figs = [b for b in book.blocks if isinstance(b, Figure)]
    assert len(figs) == 2
    assert runs_text(figs[0].caption).startswith("Figure 2.1:")
    assert runs_text(figs[1].caption).startswith("Figure 2.2:")
    assert figs[0].media_type == "image/png" and figs[1].media_type == "image/jpeg"
    # the sentence that merely starts with "Figure 2.1 shows" stays a paragraph
    assert any(isinstance(b, Paragraph) and _text(b).startswith("Figure 2.1 shows") for b in book.blocks)


def test_box_is_kept_as_a_box(book):
    boxed = [b for b in book.blocks if b.boxed]
    assert len(boxed) == 2
    assert _text(boxed[0]).startswith("Note.")
    assert all(b.indent == 0 for b in boxed)
    assert not any(isinstance(b, Code) and b.boxed for b in book.blocks)


def test_epub_is_well_formed(book, tmp_path):
    out = write_epub(book, tmp_path / "book.epub", Options())
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert names[0] == "mimetype"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip"
        for name in names:
            if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                ET.fromstring(z.read(name))  # raises on malformed XML
        opf = ET.fromstring(z.read("OEBPS/content.opf"))
        hrefs = {it.get("href") for it in opf.iter("{http://www.idpf.org/2007/opf}item")}
        for href in hrefs:
            assert "OEBPS/" + href in names
        nav = z.read("OEBPS/nav.xhtml").decode()
        assert "1. Reading Characters" in nav and "2.1 A Tokenizer in Twenty Lines" in nav
        chapter = next(z.read(n).decode() for n in names if n.endswith(".xhtml") and "Reading Characters</h1>" in z.read(n).decode())
        assert '<pre class="ln" style="padding-left: 4.2em; text-indent: -4.2em">    def __init__(self, text):</pre>' in chapter


def test_plain_code_option(book, tmp_path):
    out = write_epub(book, tmp_path / "plain.epub", Options(code_wrap="plain"))
    with zipfile.ZipFile(out) as z:
        html = "".join(z.read(n).decode() for n in z.namelist() if n.endswith(".xhtml"))
    assert "<pre><code>class Reader:\n    def __init__(self, text):" in html
