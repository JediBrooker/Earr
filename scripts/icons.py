"""Draws Earr's icon and writes every size the app serves.

The artwork lives here, once: an ear with two sound waves on a rounded tile
filled with the light theme's primary to secondary gradient (see static/tw.css
and templates/components/BrandMark.jinja). Running this writes
static/favicon.svg and renders the PNG and ICO files from the same drawing.

    uv run --group readme python scripts/icons.py
"""

import struct
from pathlib import Path

from playwright.sync_api import Browser, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"

GRADIENT = ("#4f46e5", "#0f766e")
"""earr-light's primary and secondary."""
GLYPH = "#ffffff"

# drawn on a 100 unit grid
_WAVES = (
    '<path d="M 10 40 Q 15 50 10 60" stroke-width="{wave}" opacity="0.75"/>'
    '<path d="M 19 33 Q 27 50 19 67" stroke-width="{wave}" opacity="0.9"/>'
)
_EAR = (
    '<path d="M 42 44 C 42 25 55 13 67 13 C 81 13 90 25 90 39 C 90 51 82 58 77 64'
    ' C 71 71 71 79 66 85 C 61 91 51 91 48 84" stroke-width="{outer}"/>'
    '<path d="M 55 44 C 55 34 61 28 68 28 C 75 28 79 35 77 42'
    ' C 75 49 68 51 66 58" stroke-width="{inner}"/>'
)
# extent of the drawing including stroke width: (centre x, centre y, size)
_WITH_WAVES = (50.0, 51.0, 88.0)
_EAR_ONLY = (66.0, 52.0, 86.0)


def icon(
    size: int | None = None,
    *,
    rounded: bool = True,
    glyph: float = 0.69,
    small: bool = False,
) -> str:
    """The icon as SVG on a 512 unit canvas.

    rounded: tile corners. Off for platforms that cut their own shape (iOS,
        Android's maskable icons).
    glyph: share of the canvas the drawing spans. Maskable icons need theirs
        inside the middle 80% circle.
    small: drop the thin waves and thicken the ear so it survives 16 pixels.
    """
    cx, cy, extent = _EAR_ONLY if small else _WITH_WAVES
    scale = glyph * 512 / extent
    tx, ty = 256 - cx * scale, 256 - cy * scale
    strokes = {"wave": 5, "outer": 12 if small else 8, "inner": 11 if small else 7}
    drawing = ("" if small else _WAVES.format(**strokes)) + _EAR.format(**strokes)
    dimensions = f' width="{size}" height="{size}"' if size else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"{dimensions}>'
        "<title>Earr</title>"
        '<defs><linearGradient id="tile" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{GRADIENT[0]}"/>'
        f'<stop offset="1" stop-color="{GRADIENT[1]}"/>'
        "</linearGradient></defs>"
        f'<rect width="512" height="512" rx="{116 if rounded else 0}" fill="url(#tile)"/>'
        f'<g fill="none" stroke="{GLYPH}" stroke-linecap="round" stroke-linejoin="round"'
        f' transform="translate({tx:.2f} {ty:.2f}) scale({scale:.4f})">{drawing}</g>'
        "</svg>\n"
    )


def render(browser: Browser, svg: str, size: int) -> bytes:
    page = browser.new_page(viewport={"width": size, "height": size})
    page.set_content(f'<body style="margin:0">{svg}</body>')
    png = page.locator("svg").screenshot(omit_background=True)
    page.close()
    return png


def write_ico(path: Path, images: list[tuple[int, bytes]]) -> None:
    """An .ico holding PNG images, which every browser since IE 9 reads."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, data = b"", b""
    for size, png in images:
        entries += struct.pack(
            "<BBBBHHII",
            size % 256,
            size % 256,
            0,
            0,
            1,
            32,
            len(png),
            offset + len(data),
        )
        data += png
    path.write_bytes(header + entries + data)


def main() -> None:
    _ = (STATIC / "favicon.svg").write_text(icon())
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            tiny = render(browser, icon(16, small=True, glyph=0.84), 16)
            png32 = render(browser, icon(32, glyph=0.8), 32)
            png48 = render(browser, icon(48, glyph=0.76), 48)
            outputs = {
                "favicon-16x16.png": tiny,
                "favicon-32x32.png": png32,
                # iOS rounds the corners itself and fills transparency with black
                "apple-touch-icon.png": render(
                    browser, icon(180, rounded=False, glyph=0.62), 180
                ),
                "android-chrome-192x192.png": render(browser, icon(192), 192),
                "android-chrome-512x512.png": render(browser, icon(512), 512),
                "android-chrome-maskable-512x512.png": render(
                    browser, icon(512, rounded=False, glyph=0.56), 512
                ),
            }
        finally:
            browser.close()
    for name, png in outputs.items():
        _ = (STATIC / name).write_bytes(png)
    write_ico(STATIC / "favicon.ico", [(16, tiny), (32, png32), (48, png48)])
    print(f"Wrote favicon.svg, favicon.ico and {len(outputs)} PNGs to {STATIC}")


if __name__ == "__main__":
    main()
