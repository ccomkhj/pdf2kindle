"""Questions asked about a single line of text, each answered in one place.

Several stages need to know the same things about a line: is it code, is it
set in display type, does it open a caption. When each stage kept its own
copy of the test the copies drifted apart (one caption pattern knew Korean,
another did not). Ask here instead.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .model import Line

if TYPE_CHECKING:
    from .stats import DocStats

# Type this many points larger than the running text is display type.
DISPLAY_BUMP = 1.2

# Caption openers by what they label. Add a word here and every stage knows it.
_CAPTION_WORDS = {
    "figure": ("Figure", "Fig.", "그림"),
    "table": ("Table", "표"),
    "listing": ("Listing",),
    "example": ("Example", "Exhibit"),
    "algorithm": ("Algorithm",),
}
# Words that also open ordinary numbered text ("Example 3 is ...", "Algorithm
# 1: do this, Algorithm 2: do that"), so they need the stricter evidence.
_WEAK_KINDS = ("example", "algorithm")
_WORD_KIND = {word.lower(): kind for kind, words in _CAPTION_WORDS.items() for word in words}
_WORDS = "|".join(re.escape(w) for w in sorted(_WORD_KIND, key=len, reverse=True))
# "Figure 2.1: ...", "Table A-3. ...", "그림 4 - ..."
_CAPTION = re.compile(rf"^\s*({_WORDS})\s*[\dA-Z][\d.\-]*\s*[:.–—|-](?:\s|$)", re.I)
# "Figure 3 Title": no punctuation after the number
_CAPTION_LOOSE = re.compile(rf"^\s*({_WORDS})\s*\d", re.I)


def _cjk(c: str) -> bool:
    return "\u2e80" <= c <= "\u9fff" or "\uac00" <= c <= "\ud7a3" or "\uf900" <= c <= "\ufaff" or "\uff00" <= c <= "\uffef"


def is_code(ln: Line) -> bool:
    """Is this a line of a listing (as opposed to prose with code in it)?

    Evidence for code is fixed-pitch *Latin* type: CJK fonts are fixed-pitch
    by nature and say nothing. What is not fixed-pitch may only be symbols
    (prompts, arrows) or CJK text where code allows prose: after a comment
    opener or inside a string literal, where the code font had no glyphs
    for it and the producer fell back to the text font.
    """
    code = other = 0
    for s in ln.spans:
        for c in s.text:
            if c.isspace():
                continue
            if s.mono and not _cjk(c):
                code += 1
            else:
                other += 1
    total = code + other
    if total == 0 or code < 0.5 * total:
        return False
    if code >= 0.95 * total:
        return True
    seen = ""  # the code so far, to tell where prose may legitimately appear
    for s in ln.spans:
        if s.mono:
            seen += s.text
            continue
        for c in s.text:
            if not c.isalpha():
                continue
            in_comment = any(mark in seen for mark in ("//", "/*", "#", "--", ";;"))
            in_string = seen.count('"') % 2 == 1 or seen.count("'") % 2 == 1
            if not (_cjk(c) and (in_comment or in_string)):
                return False
    return True


def is_display_size(size: float, body_size: float) -> bool:
    """Is type of this size noticeably larger than the running text?"""
    return size >= body_size + DISPLAY_BUMP


def is_heading_like(ln: Line, st: "DocStats") -> bool:
    """Could this line be a heading: display type, or a short bold line?"""
    if ln.mono_ratio > 0.5:
        return False
    if is_display_size(ln.size, st.body_size):
        return True
    return ln.bold_ratio >= 0.85 and ln.size >= st.body_size - 0.25 and len(ln.text) <= 90


def caption_label(text: str, loose: bool = False) -> str | None:
    """What a caption-opening line labels ("figure", "table", "listing",
    "example", "algorithm"), or None when the text does not open a caption.

    The strict form needs punctuation after the number ("Figure 2.1: ..."),
    because "Figure 2.1 shows ..." is an ordinary sentence. ``loose`` drops
    that requirement, for text already known to sit directly under a float;
    it only answers for figures, tables and listings.
    """
    m = _CAPTION.match(text)
    if m:
        return _WORD_KIND[m.group(1).lower()]
    if loose:
        m = _CAPTION_LOOSE.match(text)
        if m and _WORD_KIND[m.group(1).lower()] not in _WEAK_KINDS:
            return _WORD_KIND[m.group(1).lower()]
    return None


def is_caption_paragraph(text: str) -> bool:
    """Should a paragraph opening like this be set as a caption? Yes for
    figures, tables, listings and examples; "Algorithm 1: ..." is more often
    a step of running text than a caption."""
    return caption_label(text) not in (None, "algorithm")
