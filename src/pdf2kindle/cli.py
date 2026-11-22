"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__
from .config import Options


def _progress():
    last = {"stage": "", "t": 0.0}

    def say(stage: str, i: int, n: int) -> None:
        now = time.time()
        if stage != last["stage"] or now - last["t"] > 0.5 or i == n:
            last["stage"], last["t"] = stage, now
            sys.stderr.write(f"\r{stage:>7}: page {i}/{n}   ")
            if i == n:
                sys.stderr.write("\n")
            sys.stderr.flush()

    return say


def cmd_convert(args: argparse.Namespace) -> int:
    from .convert import convert
    from .epub import write_epub

    overrides = dict(
        title=args.title,
        author=args.author,
        language=args.language,
        pages=args.pages,
        skip_pages=args.skip_pages,
        tables=args.tables,
        cover=args.cover,
    )
    if args.keep_index:
        overrides["drop_index"] = False
    if args.keep_toc_pages:
        overrides["drop_toc_pages"] = False
    if args.no_footnotes:
        overrides["footnotes"] = False
    opts = Options.load(args.profile, **overrides)

    src = Path(args.pdf)
    out = Path(args.output) if args.output else src.with_suffix(".epub")
    t0 = time.time()
    book = convert(src, opts, progress=None if args.quiet else _progress())
    write_epub(book, out, opts)
    if args.debug_pdf:
        from .debug import write_overlay

        write_overlay(src, opts, Path(args.debug_pdf))
    report = {"output": str(out), "title": book.title, "author": book.author, "language": book.language}
    report.update(book.stats)
    report["seconds"] = round(time.time() - t0, 1)
    report["warnings"] = book.warnings
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    if not args.quiet:
        _print_report(report)
    if not args.quiet and not args.send_to:
        _show_epub(out)
    if args.send_to:
        return _deliver(out, args.send_to, args.quiet)
    return 0


def _show_epub(epub: Path) -> None:
    print(f"Saved EPUB: {epub.resolve()}", file=sys.stderr)
    print("Upload to Kindle: https://www.amazon.com/sendtokindle", file=sys.stderr)


def _deliver(epub: Path, destination: str, quiet: bool = False) -> int:
    from .delivery import send_epub

    try:
        send_epub(epub, destination)
    except (OSError, RuntimeError) as exc:
        print(f"Could not submit {epub}: {exc}", file=sys.stderr)
        if epub.is_file():
            _show_epub(epub)
        return 1
    if not quiet:
        _show_epub(epub)
        print(f"Submitted {epub.name} to Apple Mail for {destination}. Check Outbox and Sent before retrying.", file=sys.stderr)
    return 0


def _print_report(report: dict) -> None:
    w = sys.stderr.write
    w(f"\n{report['title']!r} -> {report['output']}\n")
    for key in (
        "pages",
        "chapters",
        "words",
        "blocks",
        "body_font",
        "mono_fonts_inferred",
        "outline_entries",
        "outline_unmatched",
        "toc_pages_dropped",
        "index_dropped",
        "furniture_lines",
        "footnotes",
        "footnotes_linked",
        "internal_links",
        "hyphens_joined",
        "hyphens_kept",
        "scanned_pages",
        "seconds",
    ):
        if key in report:
            w(f"  {key:<20} {report[key]}\n")
    for warning in report["warnings"]:
        w(f"  ! {warning}\n")


def cmd_inspect(args: argparse.Namespace) -> int:
    from .debug import inspect_pdf

    inspect_pdf(Path(args.pdf))
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    from .preview import preview

    sections = [int(x) for x in args.sections.split(",")] if args.sections else None
    out = Path(args.out) if args.out else Path(args.epub).with_suffix("").with_name(Path(args.epub).stem + "-preview")
    shots = preview(Path(args.epub), out, device=args.device, sections=sections, max_pages=args.max_pages)
    print(f"{len(shots)} page image(s) in {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pdf2kindle", description="Convert technical-book PDFs to Kindle-ready EPUB.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command")

    c = sub.add_parser("convert", help="convert a PDF to EPUB (default command)")
    c.add_argument("pdf")
    c.add_argument("-o", "--output", help="output .epub (default: next to the PDF)")
    c.add_argument("-p", "--profile", help="TOML file with per-book options")
    c.add_argument("--title")
    c.add_argument("--author")
    c.add_argument("--language", help="BCP 47 code such as en or ko (default: guessed)")
    c.add_argument("--pages", help='pages to convert, e.g. "1-3,10,200-"')
    c.add_argument("--skip-pages", help="pages to leave out")
    c.add_argument("--tables", choices=["auto", "html", "image"])
    c.add_argument("--cover", help='"auto", "none" or an image file')
    c.add_argument("--keep-index", action="store_true", help="keep the back-of-book index")
    c.add_argument("--keep-toc-pages", action="store_true", help="keep the printed table of contents")
    c.add_argument("--no-footnotes", action="store_true", help="leave footnotes in the text flow")
    c.add_argument("--debug-pdf", help="write an annotated copy of the PDF showing how each line was classified")
    c.add_argument("--report", help="write the conversion report as JSON")
    c.add_argument("-q", "--quiet", action="store_true")
    c.add_argument("--send-to", metavar="ADDRESS", help="submit the finished EPUB via macOS Mail")
    c.set_defaults(func=cmd_convert)

    send = sub.add_parser("send", help="send an existing EPUB via macOS Mail")
    send.add_argument("epub", type=Path)
    send.add_argument("--to", required=True, metavar="ADDRESS")
    send.set_defaults(func=lambda args: _deliver(args.epub, args.to))

    i = sub.add_parser("inspect", help="show fonts, outline and layout statistics of a PDF")
    i.add_argument("pdf")
    i.set_defaults(func=cmd_inspect)

    v = sub.add_parser("preview", help="render an EPUB as Kindle-sized page images (needs Playwright)")
    v.add_argument("epub")
    v.add_argument("-o", "--out", help="directory for the PNG files")
    v.add_argument("--device", choices=["paperwhite", "kindle", "scribe"], default="paperwhite")
    v.add_argument("--sections", help="spine positions to render, e.g. 3,4 (default: all)")
    v.add_argument("--max-pages", type=int, default=12, help="pages to render per section")
    v.set_defaults(func=cmd_preview)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ("convert", "send", "inspect", "preview", "-h", "--help", "--version"):
        argv.insert(0, "convert")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
