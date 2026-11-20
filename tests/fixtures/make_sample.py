"""Regenerate tests/fixtures/sample.pdf (and sample-outline.pdf).

The fixture is a tiny, original "technical book" printed to PDF by Chromium,
so the test-suite exercises a non-TeX producer: real space glyphs, drawn
table grids, tinted boxes, raster and vector figures, running heads and no
outline. Needs Playwright; the generated PDFs are committed, so running the
tests does not.

    python tests/fixtures/make_sample.py
"""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent


def _photo() -> str:
    img = Image.new("RGB", (480, 220))
    px = img.load()
    for x in range(480):
        for y in range(220):
            px[x, y] = (40 + x // 3, 90 + (x * y) % 97, 200 - y // 2)
    ImageDraw.Draw(img).ellipse((170, 40, 310, 180), fill=(250, 220, 90))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Tiny Parsers</title>
<style>
@page { size: 6in 9in; margin: 0.9in 0.7in 0.9in 0.7in;
  @top-center { content: "Tiny Parsers"; font: italic 8pt "DejaVu Serif"; }
  @bottom-center { content: counter(page); font: 8pt "DejaVu Serif"; } }
body { font: 10pt/1.38 "DejaVu Serif"; text-align: justify; hyphens: auto; }
h1 { font: 700 21pt "Inter"; break-before: page; margin: 0.6in 0 0.3in; text-align: left; }
h2 { font: 700 13.5pt "Inter"; margin: 1.3em 0 0.5em; text-align: left; }
p { margin: 0 0 0.75em; }
code, pre { font-family: "JetBrains Mono"; font-size: 8.4pt; }
pre { background: #f0f0f0; border: 0.5pt solid #999; padding: 6pt 8pt; line-height: 1.3; white-space: pre; }
.note { border: 1pt solid #557; background: #eef; padding: 6pt 9pt; margin: 1em 0; }
table { border-collapse: collapse; margin: 1em auto; font-size: 9pt; }
td, th { border: 0.6pt solid #333; padding: 3pt 7pt; text-align: left; }
figure { margin: 1em 0; text-align: center; } figcaption { font-size: 8.5pt; font-style: italic; }
blockquote { margin: 1em 2.2em; font-style: italic; }
.title { break-before: avoid; text-align: center; margin-top: 2.4in; }
.title .t { font: 800 30pt "Inter"; } .title .a { font: 12pt "DejaVu Serif"; margin-top: 0.5in; }
</style></head><body>
<div class="title"><div class="t">Tiny Parsers</div><div class="a">A. N. Example</div></div>

<h1>1. Reading Characters</h1>
<p>A parser begins with characters. Before a program can understand an expression it has to
walk over the text one character at a time, deciding where each token starts and where it stops.
This chapter builds the smallest useful version of that walk, and the remaining chapters lean on
it heavily. Nothing here is complicated, but every later simplification depends on getting these
fundamentals straightforwardly right.</p>
<p>We keep two pieces of state: the input string and a position. The function <code>peek()</code>
returns the current character without consuming it, and <code>advance()</code> moves forward.
Everything else is assembled from those two operations.</p>
<pre>class Reader:
    def __init__(self, text):
        self.text = text
        self.pos = 0

    def peek(self):
        if self.pos &lt; len(self.text):
            return self.text[self.pos]
        return ""          # end of input

    def advance(self):
        ch = self.peek()
        self.pos += 1
        return ch</pre>
<p>The empty string marks the end of input. That choice keeps comparisons simple, because
<code>"" in "0123456789"</code> is true in Python and has to be guarded against explicitly; see the
<a href="https://docs.python.org/3/reference/expressions.html">language reference</a> for details.</p>
<div class="note"><p><b>Note.</b> Reading a character at a time looks slow, yet it is rarely the
bottleneck. Measure before replacing it with anything cleverer than a loop.</p>
<p>A second paragraph inside the note keeps the box honest.</p></div>
<h2>1.1 Whitespace and Comments</h2>
<p>Most grammars ignore whitespace between tokens. We skip it in one place so that no other
function needs to think about it:</p>
<ul><li>spaces and tabs are skipped silently;</li>
<li>newlines are skipped too, but counted, so that error messages can report a line number that
actually helps the person reading it;
<ul><li>carriage returns are treated as spaces;</li><li>form feeds are rejected.</li></ul></li>
<li>comments run from <code>#</code> to the end of the line.</li></ul>
<p>The order of those checks matters less than their completeness. A tokenizer that forgets one
kind of blank will fail in ways that are remarkably hard to diagnose from the outside.</p>
<ol><li>Skip blanks.</li><li>Look at the next character.</li><li>Dispatch on its class, and
keep going until the class changes or the input ends, whichever happens first.</li></ol>

<h1>2. Tokens</h1>
<p>A token is a pair: a kind and the text it covers. Table 2.1 lists the kinds used throughout
the book. The set is deliberately small, since every additional kind multiplies the number of
cases the parser must consider.</p>
<table><tr><th>Kind</th><th>Example</th><th>Meaning</th></tr>
<tr><td>NUMBER</td><td><code>42</code></td><td>An unsigned decimal integer</td></tr>
<tr><td>NAME</td><td><code>total</code></td><td>An identifier, looked up in the current
environment when evaluated</td></tr>
<tr><td>OP</td><td><code>+</code></td><td>One of the four arithmetic operators</td></tr></table>
<figure><svg width="330" height="120" viewBox="0 0 330 120" xmlns="http://www.w3.org/2000/svg">
<rect x="5" y="40" width="90" height="40" rx="6" fill="#dfe8ff" stroke="#224"/>
<rect x="120" y="40" width="90" height="40" rx="6" fill="#dfe8ff" stroke="#224"/>
<rect x="235" y="40" width="90" height="40" rx="6" fill="#ffe9c9" stroke="#422"/>
<path d="M95 60 L120 60 M210 60 L235 60" stroke="#222" stroke-width="1.5"/>
<path d="M112 54 L120 60 L112 66 M227 54 L235 60 L227 66" fill="none" stroke="#222" stroke-width="1.5"/>
<text x="50" y="65" font-family="Inter" font-size="11" text-anchor="middle">characters</text>
<text x="165" y="65" font-family="Inter" font-size="11" text-anchor="middle">tokens</text>
<text x="280" y="65" font-family="Inter" font-size="11" text-anchor="middle">tree</text>
</svg><figcaption>Figure 2.1: The pipeline from text to tree</figcaption></figure>
<p>Figure 2.1 shows where tokens sit. The tokenizer is the only stage that sees raw text, and
the parser is the only stage that sees tokens; keeping those boundaries strict is what makes
each stage testable on its own.</p>
<blockquote>Programs must be written for people to read, and only incidentally for machines to
execute.</blockquote>
<h2>2.1 A Tokenizer in Twenty Lines</h2>
<p>The loop below produces tokens until the reader is exhausted. Notice the long line: on a
narrow screen it has to wrap, and the indentation that follows must survive the journey.</p>
<pre>def tokens(reader):
    while reader.peek():
        ch = reader.peek()
        if ch.isspace():
            reader.advance()
        elif ch.isdigit():
            yield ("NUMBER", take_while(reader, str.isdigit))
        elif ch.isalpha() or ch == "_":
            yield ("NAME", take_while(reader, lambda c: c.isalnum() or c == "_"))
        else:
            yield ("OP", reader.advance())

$ python tiny.py "total + 42"
('NAME', 'total')   ('OP', '+')   ('NUMBER', '42')</pre>
<figure><img src="%(photo)s" width="300"><figcaption>Figure 2.2: A photograph, for
contrast</figcaption></figure>
<p>That is the whole tokenizer. It does not know about precedence, about parentheses, or about
what any name means, and that ignorance is the point: each of those concerns belongs to a later
stage, where it can be handled without re-reading a single character of the original input.</p>
</body></html>"""


def main() -> None:
    html = HTML % {"photo": _photo()}
    with sync_playwright() as p:
        launch = {}
        if os.environ.get("PDF2KINDLE_CHROMIUM"):
            launch["executable_path"] = os.environ["PDF2KINDLE_CHROMIUM"]
        browser = p.chromium.launch(**launch)
        page = browser.new_page()
        page.set_content(html)
        page.pdf(path=str(HERE / "sample.pdf"), prefer_css_page_size=True, print_background=True)
        page.pdf(path=str(HERE / "sample-outline.pdf"), prefer_css_page_size=True, print_background=True, outline=True, tagged=True)
        browser.close()


if __name__ == "__main__":
    main()
