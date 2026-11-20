"""Turn geometric lines into inline runs (paragraph text) or code lines."""

from __future__ import annotations

import re
from statistics import median

from .model import Line, LinkRect, Run, Span
from .textutil import clean_text

# Marks a line-end hyphen whose fate (soft hyphen vs. real hyphen) is decided
# once the whole book's vocabulary is known. Never reaches the output.
SOFT = "\x01"

_CJK = re.compile(r"[぀-ヿ㐀-鿿豈-﫿＀-￯]")
_WS = re.compile(r"[ \t\r\n]+")


def _style_of(span: Span, line_size: float) -> tuple[bool, bool, bool, bool]:
    sup = span.superscript and span.size < 0.85 * line_size
    has_alpha = any(c.isalpha() for c in span.text)
    return (span.bold and has_alpha, span.italic and has_alpha, span.mono, sup)


def _links_for(line: Line, links: list[LinkRect]) -> list[LinkRect]:
    if not links:
        return []
    mid = (line.y0 + line.y1) / 2
    return [lk for lk in links if lk.y0 - 1 <= mid <= lk.y1 + 1 and lk.x1 > line.x0 and lk.x0 < line.x1]


def _link_at(x: float, hits: list[LinkRect]) -> LinkRect | None:
    for lk in hits:
        if lk.x0 - 0.5 <= x <= lk.x1 + 0.5:
            return lk
    return None


def line_runs(line: Line, links: list[LinkRect], skip_chars: int = 0) -> list[Run]:
    """Runs for one line. ``skip_chars`` drops leading characters (a list
    marker, a footnote label)."""
    hits = _links_for(line, links)
    runs: list[Run] = []
    to_skip = skip_chars
    for span in line.spans:
        text = span.text
        start = 0
        if to_skip:
            if to_skip >= len(text):
                to_skip -= len(text)
                continue
            start = to_skip
            to_skip = 0
        bold, italic, mono, sup = _style_of(span, line.size)
        if not hits:
            _push(runs, Run(text[start:], bold, italic, mono, sup))
            continue
        # split the span wherever link coverage changes
        seg_start = start
        current = _link_at((span.xs[start] + span.xs[start + 1]) / 2, hits)
        for i in range(start + 1, len(text) + 1):
            here = _link_at((span.xs[i] + span.xs[i + 1]) / 2, hits) if i < len(text) else None
            if i == len(text) or here is not current:
                run = Run(text[seg_start:i], bold, italic, mono, sup)
                if current is not None:
                    if current.uri:
                        run.href = current.uri
                    else:
                        run.target = (current.page, current.dest_y)
                _push(runs, run)
                seg_start = i
                current = here
    return runs


def _push(runs: list[Run], run: Run) -> None:
    if not run.text:
        return
    if runs and runs[-1].same_style(run):
        runs[-1].text += run.text
    elif runs and not run.text.strip() and not run.href and run.target is None:
        runs[-1].text += run.text  # whitespace never needs its own style
    else:
        runs.append(run)


def join_lines(per_line: list[list[Run]]) -> list[Run]:
    """Join the lines of a paragraph into one run list, removing line-end
    hyphenation candidates and inserting the missing inter-line spaces."""
    out: list[Run] = []
    for runs in per_line:
        runs = [r for r in runs]
        if not runs:
            continue
        if out:
            last = out[-1]
            tail = last.text.rstrip()
            head = runs[0].text.lstrip()
            runs[0].text = head
            if (
                len(tail) >= 2
                and tail[-1] in "-‐"
                and tail[-2].isalpha()
                and head[:1].islower()
                and not last.mono
            ):
                last.text = tail[:-1] + SOFT
            elif len(tail) >= 2 and tail[-1] in "-‐" and last.mono and runs[0].mono:
                last.text = tail  # option names and the like: keep, no space
            elif tail and head and _CJK.match(tail[-1]) and _CJK.match(head[0]):
                last.text = tail
            else:
                last.text = tail + " "
        for r in runs:
            _push(out, r)
    return tidy_runs(out)


def tidy_runs(runs: list[Run]) -> list[Run]:
    out: list[Run] = []
    for r in runs:
        r.text = clean_text(_WS.sub(" ", r.text))
        if not r.text:
            continue
        if out and out[-1].text.endswith(" ") and r.text.startswith(" "):
            r.text = r.text[1:]
            if not r.text:
                continue
        if out and out[-1].same_style(r):
            out[-1].text += r.text
        else:
            out.append(r)
    if out:
        out[0].text = out[0].text.lstrip()
        out[-1].text = out[-1].text.rstrip()
        out = [r for r in out if r.text]
    # keep leading/trailing spaces outside styled runs: "<i>word </i>next" reads badly
    for i, r in enumerate(out):
        if (r.bold or r.italic or r.mono or r.sup or r.href or r.target) and r.text != r.text.rstrip() and i + 1 < len(out):
            r.text = r.text.rstrip()
            if not out[i + 1].text.startswith(" "):
                out[i + 1].text = " " + out[i + 1].text
    return out


def runs_text(runs: list[Run]) -> str:
    return "".join(r.text for r in runs).replace(SOFT, "")


def first_word_width(line: Line) -> float:
    """Width of the first word of a line (how much room it needed)."""
    seen = False
    for span in line.spans:
        for i, c in enumerate(span.text):
            if c.isspace():
                if seen:
                    return span.xs[i] - line.x0
            else:
                seen = True
    return line.x1 - line.x0


# --------------------------------------------------------------------------
# code
# --------------------------------------------------------------------------
def char_width(lines: list[Line]) -> float:
    """Advance width of the monospace font used by a code block."""
    widths: list[float] = []
    for ln in lines:
        for s in ln.spans:
            if not s.mono:
                continue
            for i, c in enumerate(s.text):
                if not c.isspace():
                    w = s.xs[i + 1] - s.xs[i]
                    if w > 0.5:
                        widths.append(w)
        if len(widths) > 400:
            break
    if not widths:
        return max(1.0, lines[0].size * 0.5)
    return median(widths)


def code_lines(lines: list[Line], pitch_hint: float) -> list[list[Run]]:
    """Rebuild code text on a character grid so indentation and column
    alignment survive, whether or not the PDF contains real space glyphs."""
    width = char_width(lines)
    left = min(ln.x0 for ln in lines)
    gaps = [b.baseline - a.baseline for a, b in zip(lines, lines[1:])]
    positive = [g for g in gaps if g > 1]
    pitch = min(positive) if positive else pitch_hint
    pitch = max(pitch, 0.9 * lines[0].size)

    out: list[list[Run]] = []
    prev_base = None
    for ln in lines:
        if prev_base is not None:
            blanks = int(round((ln.baseline - prev_base) / pitch)) - 1
            for _ in range(max(0, min(blanks, 3))):
                out.append([])
        prev_base = ln.baseline
        runs: list[Run] = []
        col = 0
        for span in ln.spans:
            bold = span.bold
            italic = span.italic
            for i, c in enumerate(span.text):
                if c.isspace():
                    continue
                want = int(round((span.xs[i] - left) / width))
                if want > col:
                    _push_code(runs, " " * (want - col), False, False)
                    col = want
                _push_code(runs, c, bold, italic)
                col += 1
        out.append(runs)
    return out


def _push_code(runs: list[Run], text: str, bold: bool, italic: bool) -> None:
    if runs and (runs[-1].bold == bold and runs[-1].italic == italic or not text.strip()):
        runs[-1].text += text
    else:
        runs.append(Run(text, bold=bold, italic=italic, mono=True))
