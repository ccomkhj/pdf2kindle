"""Render an EPUB the way a Kindle would page it, as PNG screenshots.

Not a Kindle emulator - it is Chromium with a Kindle-sized viewport, typical
reading font size and true pagination (CSS columns) - but it is close enough
to judge code wrapping, figure sizes and heading hierarchy without a device.
Needs the optional ``playwright`` dependency.
"""

from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

# CSS pixel viewport at 2x device scale, and the font size that gives roughly
# the characters-per-line a reader sees at a mid-range font setting.
DEVICES = {
    "paperwhite": (618, 824, 23),  # 6.8", 1236 x 1648
    "kindle": (536, 724, 21),  # 6", 1072 x 1448
    "scribe": (930, 1240, 26),  # 10.2", 1860 x 2480
}

_READER_CSS = """
html { height: 100vh; overflow: hidden; }
body {
  box-sizing: border-box; height: 100vh; margin: 0 !important; padding: %(m)dpx !important;
  column-width: calc(100vw - %(m2)dpx); column-gap: %(m2)dpx; column-fill: auto;
  font-family: "Bitstream Charter", "DejaVu Serif", Georgia, serif;
  font-size: %(fs)dpx; line-height: 1.4; text-align: justify; hyphens: auto;
  background: #f4f1ea; color: #111;
}
"""


def _spine(root: Path) -> list[Path]:
    container = ET.parse(root / "META-INF/container.xml")
    opf_rel = container.find(".//{*}rootfile").get("full-path")
    opf = root / opf_rel
    tree = ET.parse(opf)
    items = {it.get("id"): it.get("href") for it in tree.findall(".//{*}manifest/{*}item")}
    return [opf.parent / items[ref.get("idref")] for ref in tree.findall(".//{*}spine/{*}itemref")]


def preview(
    epub: Path,
    out_dir: Path,
    device: str = "paperwhite",
    sections: list[int] | None = None,
    max_pages: int = 12,
) -> list[Path]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("preview needs Playwright: pip install 'pdf2kindle[preview]' && playwright install chromium") from exc

    width, height, font = DEVICES[device]
    margin = round(width * 0.06)
    css = _READER_CSS % {"m": margin, "m2": 2 * margin, "fs": font}
    out_dir.mkdir(parents=True, exist_ok=True)
    shots: list[Path] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with zipfile.ZipFile(epub) as z:
            z.extractall(root)
        spine = _spine(root)
        chosen = sections if sections else list(range(len(spine)))
        with sync_playwright() as p:
            launch = {}
            if os.environ.get("PDF2KINDLE_CHROMIUM"):
                launch["executable_path"] = os.environ["PDF2KINDLE_CHROMIUM"]
            browser = p.chromium.launch(**launch)
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=2)
            for idx in chosen:
                if idx >= len(spine):
                    continue
                page.goto(spine[idx].as_uri())
                page.add_style_tag(content=css)
                page.wait_for_timeout(150)
                total = page.evaluate("Math.ceil(document.documentElement.scrollWidth / window.innerWidth)")
                for n in range(min(total, max_pages)):
                    page.evaluate(f"document.documentElement.scrollLeft = {n} * window.innerWidth")
                    shot = out_dir / f"s{idx:03d}-p{n + 1:03d}.png"
                    page.screenshot(path=str(shot))
                    shots.append(shot)
            browser.close()
    return shots
