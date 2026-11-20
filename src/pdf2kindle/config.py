"""Conversion options, overridable per book from a TOML profile."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, fields
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


@dataclass
class Options:
    # metadata
    title: str = ""
    author: str = ""
    language: str = ""  # BCP 47, e.g. "en", "ko"; guessed when empty

    # which pages to read (1-based, inclusive ranges like "1-3,10,200-")
    pages: str = ""
    skip_pages: str = ""

    # structure
    drop_toc_pages: bool = True  # printed table of contents (replaced by real navigation)
    drop_index: bool = True  # back-of-book index (page numbers are meaningless on Kindle)
    split_level: int = 1  # heading level that starts a new EPUB file
    toc_depth: int = 3

    # extra regular expressions; any line matching one in full is discarded
    drop_lines: list[str] = field(default_factory=list)

    # margins to ignore, as fractions of the page (top, right, bottom, left)
    crop: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])

    # fonts
    mono_fonts: list[str] = field(default_factory=list)  # extra substrings marking monospace fonts
    not_mono_fonts: list[str] = field(default_factory=list)

    # figures and tables
    image_width: int = 1600  # pixels for a figure spanning the full text width
    image_max_dpi: int = 400
    jpeg_quality: int = 85
    tables: str = "auto"  # auto | html | image
    cover: str = "auto"  # auto | none | path to an image

    # code listings: "hanging" marks wrapped lines by indenting their
    # continuation; "plain" emits one ordinary <pre> per listing
    code_wrap: str = "hanging"

    # footnotes
    footnotes: bool = True

    @classmethod
    def load(cls, path: str | Path | None, **overrides) -> "Options":
        data: dict = {}
        if path:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown option(s) in {path}: {', '.join(sorted(unknown))}")
        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**data)


def parse_ranges(spec: str, n_pages: int) -> set[int]:
    """'1-3,10,200-' -> zero-based page indexes."""
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo = int(a) if a.strip() else 1
            hi = int(b) if b.strip() else n_pages
        else:
            lo = hi = int(part)
        out.update(range(max(1, lo) - 1, min(n_pages, hi)))
    return out
