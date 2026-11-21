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
    "other": ("Example", "Exhibit", "Algorithm"),
}
_WORD_KIND = {word.lower(): kind for kind, words in _CAPTION_WORDS.items() for word in words}
_WORDS = "|".join(re.escape(w) for w in sorted(_WORD_KIND, key=len, reverse=True))
# "Figure 2.1: ...", "Table A-3. ...", "그림 4 - ..."
_CAPTION = re.compile(rf"^\s*({_WORDS})\s*[\dA-Z][\d.\-]*\s*[:.–—|-](?:\s|$)", re.I)
# "Figure 3 Title": no punctuation after the number
_CAPTION_LOOSE = re.compile(rf"^\s*({_WORDS})\s*\d", re.I)


def is_code(ln: Line) -> bool:
    """Is this a line of a listing (as opposed to prose with code in it)?"""
    if ln.mono_ratio >= 0.95:
        return True
    if ln.mono_ratio >= 0.5:
        # the non-monospace remainder may only be symbols (prompts, arrows)
        return not any((not s.mono) and any(c.isalpha() for c in s.text) for s in ln.spans)
    return False


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
    """What a caption-opening line labels: "figure", "table", "listing" or
    "other" - or None when the text does not open a caption.

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
        if m and _WORD_KIND[m.group(1).lower()] != "other":
            return _WORD_KIND[m.group(1).lower()]
    return None
