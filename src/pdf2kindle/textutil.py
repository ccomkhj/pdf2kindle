"""Small text helpers shared across stages."""

from __future__ import annotations

import re
import unicodedata

_ROMAN = re.compile(r"^(?=[ivxlcdm]+$)m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$", re.I)
_ALNUM = re.compile(r"[^0-9a-zÀ-￿]+")

BULLETS = "•◦▪▫‣⁃∙·●○■□▶►–—*-"
_BULLET = re.compile(r"^\s*([" + re.escape(BULLETS) + r"])\s+(?=\S)")
_ENUM = re.compile(r"^\s*(\(?(?:\d{1,3}|[a-zA-Z]|[ivxIVX]{1,5})[.)])\s+(?=\S)")
_SENT_END = re.compile(r"[.!?:;…][\"'”’)\]]*$")


def norm_key(text: str) -> str:
    """Lower-case alphanumerics only: for comparing titles with page text."""
    text = unicodedata.normalize("NFKC", text).lower()
    return _ALNUM.sub("", text)


def is_roman(token: str) -> bool:
    return bool(token) and bool(_ROMAN.match(token))


def is_page_label(text: str) -> bool:
    """True for strings that are only a page number ('12', 'xii', '- 12 -')."""
    core = text.strip().strip("-–—|·•[]() ").strip()
    if core.lower().startswith("page "):
        core = core[5:].strip()
    return bool(core) and (core.isdigit() or is_roman(core))


def strip_numbers(text: str) -> str:
    """Remove digits and roman-numeral tokens (running heads vary only there)."""
    words = []
    for tok in re.split(r"[\s|·•–—-]+", text.lower()):
        tok = re.sub(r"\d+", "", tok).strip(".,:;()[]")
        if tok and not is_roman(tok):
            words.append(tok)
    return " ".join(words)


def match_bullet(text: str) -> re.Match | None:
    return _BULLET.match(text)


def match_enum(text: str) -> re.Match | None:
    return _ENUM.match(text)


def ends_sentence(text: str) -> bool:
    return bool(_SENT_END.search(text.rstrip()))


# TeX sets an accented letter as two glyphs: the accent, then the base letter.
_ACCENTS = {
    "¨": "̈",  # diaeresis
    "´": "́",  # acute
    "ˆ": "̂",  # circumflex
    "˜": "̃",  # tilde
    "ˇ": "̌",  # caron
    "˘": "̆",  # breve
    "˙": "̇",  # dot above
    "˚": "̊",  # ring
    "¯": "̄",  # macron
    "¸": "̧",  # cedilla
}
_ACCENT_PAIR = re.compile("([" + "".join(_ACCENTS) + "]) ?([A-Za-zıȷ])")


def _compose(m: re.Match) -> str:
    base = {"ı": "i", "ȷ": "j"}.get(m.group(2), m.group(2))
    composed = unicodedata.normalize("NFC", base + _ACCENTS[m.group(1)])
    return composed if len(composed) == 1 else m.group(0)


def clean_text(text: str) -> str:
    """Normalise presentation forms (ligatures, detached accents) without
    touching ordinary punctuation or CJK."""
    if any("ﬀ" <= c <= "ﬆ" for c in text):
        text = "".join(unicodedata.normalize("NFKC", c) if "ﬀ" <= c <= "ﬆ" else c for c in text)
    if any(c in _ACCENTS for c in text):
        text = _ACCENT_PAIR.sub(_compose, text)
    return text
