"""Book model -> EPUB 3 (with an EPUB 2 NCX for older Kindle pipelines)."""

from __future__ import annotations

import html
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from .config import Options
from .inline import runs_text
from .model import Block, Book, Code, Figure, Heading, ListItem, Note, Paragraph, Rule, Run, Table

_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_SPLIT_CHARS = 280_000  # sub-split a section larger than this at the next heading level


def esc(text: str) -> str:
    return html.escape(_XML_BAD.sub("", text), quote=True)


@dataclass
class Section:
    file: str
    title: str
    blocks: list[Block] = field(default_factory=list)


@dataclass
class NavPoint:
    title: str
    href: str
    level: int
    children: list["NavPoint"] = field(default_factory=list)


class Writer:
    def __init__(self, book: Book, opts: Options | None = None):
        self.book = book
        self.opts = opts or Options()
        self.images: list[tuple[str, bytes, str]] = []
        self.anchor_file: dict[str, str] = {}
        self.sections: list[Section] = []
        self.nav: list[NavPoint] = []

    # ---- structure ---------------------------------------------------------
    def _split(self) -> None:
        chunks = self._cut(self.book.blocks, self.opts.split_level)
        for i, chunk in enumerate(chunks):
            head = next((b for b in chunk if isinstance(b, Heading)), None)
            title = head.title if head and chunk and chunk[0] is head else (self.book.title if i == 0 else "")
            self.sections.append(Section(file=f"text/part{i:04d}.xhtml", title=title, blocks=chunk))
        hid = 0
        for sec in self.sections:
            for b in sec.blocks:
                if isinstance(b, Heading):
                    hid += 1
                    if not b.anchor:
                        b.anchor = f"h{hid}"
                if b.anchor:
                    self.anchor_file[b.anchor] = sec.file

    def _cut(self, blocks: list[Block], level: int) -> list[list[Block]]:
        chunks: list[list[Block]] = [[]]
        for b in blocks:
            if isinstance(b, Heading) and b.level <= level and chunks[-1]:
                chunks.append([])
            chunks[-1].append(b)
        chunks = [c for c in chunks if c]
        out: list[list[Block]] = []
        for c in chunks:
            size = sum(len(runs_text(b.runs)) if hasattr(b, "runs") else 200 for b in c)
            deeper = any(isinstance(b, Heading) and b.level == level + 1 for b in c[1:])
            if size > _SPLIT_CHARS and deeper and level < 4:
                out.extend(self._cut(c, level + 1))
            else:
                out.append(c)
        return out

    def _build_nav(self) -> None:
        stack: list[NavPoint] = []
        for sec in self.sections:
            for b in sec.blocks:
                if not isinstance(b, Heading) or not b.toc or b.level > self.opts.toc_depth:
                    continue
                title = b.title or runs_text(b.runs)
                if not title.strip():
                    continue
                point = NavPoint(title=" ".join(title.split()), href=f"{sec.file}#{b.anchor}", level=b.level)
                while stack and stack[-1].level >= point.level:
                    stack.pop()
                (stack[-1].children if stack else self.nav).append(point)
                stack.append(point)

    # ---- inline ------------------------------------------------------------
    def _runs(self, runs: list[Run], here: str, in_code: bool = False) -> str:
        out: list[str] = []
        for r in runs:
            text = esc(r.text)
            if not text:
                continue
            if r.note >= 0:
                out.append(
                    f'<sup><a class="noteref" epub:type="noteref" id="fnref{r.note}" href="#fn{r.note}">{text}</a></sup>'
                )
                continue
            if r.mono and not in_code:
                text = f"<code>{text}</code>"
            if r.bold:
                text = f"<b>{text}</b>"
            if r.italic:
                text = f"<i>{text}</i>"
            if r.sup:
                text = f"<sup>{text}</sup>"
            if r.href:
                href = r.href
                if href.startswith("#"):
                    target = self.anchor_file.get(href[1:])
                    if target is None:
                        href = ""
                    elif target != here:
                        href = Path(target).name + href
                if href:
                    text = f'<a href="{esc(href)}">{text}</a>'
            out.append(text)
        return "".join(out)

    # ---- blocks ------------------------------------------------------------
    def _section_xhtml(self, sec: Section) -> str:
        body: list[str] = []
        notes: list[int] = []
        blocks = sec.blocks
        i = 0
        in_box = False
        while i < len(blocks):
            b = blocks[i]
            if b.boxed != in_box:
                body.append('<div class="box">' if b.boxed else "</div>")
                in_box = b.boxed
            if isinstance(b, ListItem):
                j = i
                while j < len(blocks) and isinstance(blocks[j], ListItem) and blocks[j].boxed == in_box:
                    j += 1
                body.append(self._list(blocks[i:j], sec.file, notes))
                i = j
                continue
            body.append(self._block(b, sec.file, notes))
            i += 1
        if in_box:
            body.append("</div>")
        if notes:
            body.append('<section class="notes" epub:type="footnotes">')
            for idx in notes:
                note = self.book.notes[idx]
                body.append(
                    f'<aside class="footnote" epub:type="footnote" id="fn{idx}"><p>'
                    f'<a href="#fnref{idx}">{esc(note.label)}</a>. {self._runs(note.runs, sec.file)}</p></aside>'
                )
            body.append("</section>")
        return self._page(sec.title or self.book.title, "\n".join(x for x in body if x))

    def _collect_notes(self, runs: list[Run], notes: list[int]) -> None:
        for r in runs:
            if r.note >= 0 and r.note not in notes:
                notes.append(r.note)

    def _idattr(self, b: Block) -> str:
        return f' id="{esc(b.anchor)}"' if b.anchor else ""

    def _block(self, b: Block, here: str, notes: list[int]) -> str:
        if isinstance(b, Heading):
            if not b.runs:
                return f'<div{self._idattr(b)}></div>'
            return f"<h{b.level}{self._idattr(b)}>{self._runs(b.runs, here)}</h{b.level}>"
        if isinstance(b, Paragraph):
            self._collect_notes(b.runs, notes)
            classes = []
            if b.kind != "p":
                classes.append(b.kind)
            if b.indent:
                classes.append(f"in{b.indent}")
            cls = f' class="{" ".join(classes)}"' if classes else ""
            return f"<p{cls}{self._idattr(b)}>{self._runs(b.runs, here)}</p>"
        if isinstance(b, Code):
            lines = ["".join(self._code_run(r) for r in line) for line in b.lines]
            longest = max((sum(len(r.text) for r in line) for line in b.lines), default=0)
            cls = ' class="wide"' if longest > 62 else ""
            return f"<pre{cls}{self._idattr(b)}><code>" + "\n".join(lines) + "</code></pre>"
        if isinstance(b, Figure):
            return self._figure(b, here, notes)
        if isinstance(b, Table):
            return self._table(b, here, notes)
        if isinstance(b, Rule):
            return "<hr/>"
        return ""

    def _code_run(self, r: Run) -> str:
        text = esc(r.text)
        if r.bold and text.strip():
            text = f"<b>{text}</b>"
        if r.italic and text.strip():
            text = f"<i>{text}</i>"
        return text

    def _figure(self, b: Figure, here: str, notes: list[int]) -> str:
        ext = "png" if b.media_type == "image/png" else "jpg"
        name = f"images/img{len(self.images) + 1:04d}.{ext}"
        self.images.append((name, b.data, b.media_type))
        width = 100 if b.frac >= 0.7 else max(35, int(round(b.frac * 100 / 5) * 5) + 10)
        alt = esc(b.alt or runs_text(b.caption) or "Figure")
        cap = ""
        if b.caption:
            self._collect_notes(b.caption, notes)
            cap = f"<figcaption>{self._runs(b.caption, here)}</figcaption>"
        return (
            f'<figure{self._idattr(b)}><img src="../{name}" alt="{alt}" style="width: {width}%"/>{cap}</figure>'
        )

    def _table(self, b: Table, here: str, notes: list[int]) -> str:
        out = [f"<table{self._idattr(b)}>"]
        if b.caption:
            out.append(f"<caption>{self._runs(b.caption, here)}</caption>")
        for r, row in enumerate(b.rows):
            tag = "th" if b.header and r == 0 else "td"
            cells = "".join(f"<{tag}>{esc(c).replace(chr(10), '<br/>')}</{tag}>" for c in row)
            out.append(f"<tr>{cells}</tr>")
        out.append("</table>")
        return "\n".join(out)

    def _list(self, items: list[ListItem], here: str, notes: list[int]) -> str:
        levels = sorted({it.level for it in items})
        # collapse near-equal offsets into depth steps
        depth_of: dict[int, int] = {}
        depth = -1
        last = None
        for lv in levels:
            if last is None or lv - last >= 1:
                depth += 1
            depth_of[lv] = depth
            last = lv
        out: list[str] = []
        stack: list[str] = []  # open list tags

        def open_list(it: ListItem) -> None:
            tag = "ol" if it.ordered else "ul"
            out.append('<ol class="explicit">' if it.ordered else "<ul>")
            stack.append(tag)

        for idx, it in enumerate(items):
            d = min(depth_of[it.level], len(stack))  # never skip a nesting level
            want_tag = "ol" if it.ordered else "ul"
            while len(stack) > d + 1:
                out.append(f"</li></{stack.pop()}>")
            if len(stack) == d + 1 and stack[-1] != want_tag:
                out.append(f"</li></{stack.pop()}>")
            if len(stack) == d + 1:
                out.append("</li>")
            else:
                open_list(it)
            self._collect_notes(it.runs, notes)
            marker = f'<span class="marker">{esc(it.marker)}</span> ' if it.ordered else ""
            out.append(f"<li{self._idattr(it)}>{marker}{self._runs(it.runs, here)}")
        while stack:
            out.append(f"</li></{stack.pop()}>")
        return "".join(out)

    # ---- documents ---------------------------------------------------------
    def _page(self, title: str, body: str, css: str = "../style.css") -> str:
        lang = esc(self.book.language)
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
            f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
            f'xml:lang="{lang}" lang="{lang}">\n<head>\n<meta charset="utf-8"/>\n'
            f"<title>{esc(title)}</title>\n"
            f'<link rel="stylesheet" type="text/css" href="{css}"/>\n</head>\n<body>\n{body}\n</body>\n</html>\n'
        )

    def _nav_xhtml(self) -> str:
        def ol(points: list[NavPoint]) -> str:
            if not points:
                return ""
            items = "".join(f'<li><a href="{esc(p.href)}">{esc(p.title)}</a>{ol(p.children)}</li>' for p in points)
            return f"<ol>{items}</ol>"

        first = self.sections[0].file if self.sections else "nav.xhtml"
        start = self.nav[0].href if self.nav else first
        body = (
            f'<nav epub:type="toc" id="toc"><h1>Contents</h1>{ol(self.nav) or "<ol><li><a href=%s>Start</a></li></ol>" % repr(first).replace(chr(39), chr(34))}</nav>\n'
            '<nav epub:type="landmarks" id="landmarks" hidden="hidden"><ol>'
            '<li><a epub:type="toc" href="nav.xhtml#toc">Contents</a></li>'
            f'<li><a epub:type="bodymatter" href="{esc(start)}">Start</a></li></ol></nav>'
        )
        return self._page("Contents", body, css="style.css")

    def _ncx(self, uid: str) -> str:
        counter = 0

        def depth(points: list[NavPoint]) -> int:
            return 1 + max((depth(p.children) for p in points), default=0) if points else 0

        def render(points: list[NavPoint]) -> str:
            nonlocal counter
            out = []
            for p in points:
                counter += 1
                out.append(
                    f'<navPoint id="np{counter}" playOrder="{counter}"><navLabel><text>{esc(p.title)}</text></navLabel>'
                    f'<content src="{esc(p.href)}"/>{render(p.children)}</navPoint>'
                )
            return "".join(out)

        points = render(self.nav)
        if not points and self.sections:
            points = (
                '<navPoint id="np1" playOrder="1"><navLabel><text>Start</text></navLabel>'
                f'<content src="{self.sections[0].file}"/></navPoint>'
            )
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n<head>'
            f'<meta name="dtb:uid" content="urn:uuid:{uid}"/><meta name="dtb:depth" content="{max(1, depth(self.nav))}"/>'
            '<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>\n'
            f"<docTitle><text>{esc(self.book.title)}</text></docTitle>\n<navMap>{points}</navMap>\n</ncx>\n"
        )

    def _opf(self, uid: str, files: list[tuple[str, str, str]], spine: list[str]) -> str:
        b = self.book
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta = [
            f'<dc:identifier id="pub-id">urn:uuid:{uid}</dc:identifier>',
            f"<dc:title>{esc(b.title or 'Untitled')}</dc:title>",
            f"<dc:language>{esc(b.language)}</dc:language>",
        ]
        if b.author:
            meta.append(f"<dc:creator>{esc(b.author)}</dc:creator>")
        meta.append(f'<meta property="dcterms:modified">{now}</meta>')
        if b.cover:
            meta.append('<meta name="cover" content="cover-image"/>')
        manifest = []
        for fid, href, media in files:
            props = ""
            if fid == "nav":
                props = ' properties="nav"'
            elif fid == "cover-image":
                props = ' properties="cover-image"'
            manifest.append(f'<item id="{fid}" href="{esc(href)}" media-type="{media}"{props}/>')
        refs = "".join(f'<itemref idref="{fid}"/>' for fid in spine)
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id" '
            f'xml:lang="{esc(b.language)}">\n'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n' + "\n".join(meta) + "\n</metadata>\n"
            "<manifest>\n" + "\n".join(manifest) + "\n</manifest>\n"
            f'<spine toc="ncx">{refs}</spine>\n</package>\n'
        )

    # ---- output ------------------------------------------------------------
    def write(self, path: str | Path) -> Path:
        path = Path(path)
        self._split()
        self._build_nav()
        b = self.book
        uid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"pdf2kindle:{b.title}:{b.author}"))

        docs = [(sec.file, self._section_xhtml(sec)) for sec in self.sections]
        files: list[tuple[str, str, str]] = [
            ("nav", "nav.xhtml", "application/xhtml+xml"),
            ("ncx", "toc.ncx", "application/x-dtbncx+xml"),
            ("css", "style.css", "text/css"),
        ]
        spine: list[str] = []
        for i, (name, _) in enumerate(docs):
            files.append((f"t{i}", name, "application/xhtml+xml"))
            spine.append(f"t{i}")
        # the visible contents page goes after the first (title) section
        spine.insert(1 if len(spine) > 1 else 0, "nav")
        for i, (name, _data, media) in enumerate(self.images):
            files.append((f"i{i}", name, media))
        cover_name = ""
        if b.cover:
            cover_name = "images/cover." + ("png" if b.cover_type == "image/png" else "jpg")
            files.append(("cover-image", cover_name, b.cover_type))

        css = resources.files("pdf2kindle").joinpath("kindle.css").read_text(encoding="utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as z:
            z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)

            def put(name: str, data: str | bytes, store: bool = False) -> None:
                z.writestr(name, data, compress_type=zipfile.ZIP_STORED if store else zipfile.ZIP_DEFLATED)

            put(
                "META-INF/container.xml",
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                "</rootfiles></container>\n",
            )
            put("OEBPS/content.opf", self._opf(uid, files, spine))
            put("OEBPS/nav.xhtml", self._nav_xhtml())
            put("OEBPS/toc.ncx", self._ncx(uid))
            put("OEBPS/style.css", css)
            for name, text in docs:
                put("OEBPS/" + name, text)
            for name, data, _media in self.images:
                put("OEBPS/" + name, data, store=True)
            if b.cover:
                put("OEBPS/" + cover_name, b.cover, store=True)
        return path


def write_epub(book: Book, path: str | Path, opts: Options | None = None) -> Path:
    return Writer(book, opts).write(path)
