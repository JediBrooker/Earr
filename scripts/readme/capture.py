"""Takes the README screenshots and renders the logo and feature graphic.

Builds the frontend the way the Dockerfile does, seeds a throwaway database
(seed.py), starts the app against a fake Prowlarr (prowlarr_stub.py) and drives
it with Playwright. Book data comes from the real Audible API, so this needs a
network connection.

Writes media/screenshots/*.png, media/logo.png and media/feature.png.
"""

import json
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from playwright.sync_api import Browser, Page, sync_playwright

from scripts.readme.prowlarr_stub import ProwlarrStub, StubBook

ROOT = Path(__file__).resolve().parents[2]
MEDIA = ROOT / "media"
SCREENSHOTS = MEDIA / "screenshots"
BRAND = MEDIA / "brand"
ICON = ROOT / "static" / "favicon.svg"
"""The app's icon, drawn by scripts/icons.py. The README logo is the same file."""
CACHE = Path.home() / ".cache" / "earr-readme"

THEME_STORAGE_KEY = "theme"
"""localStorage key the layout reads the theme from."""
THEMES = {"light": "earr-light", "dark": "earr-dark"}
"""README variant -> DaisyUI theme name. The only place theme names are kept."""

DESKTOP = {"viewport": {"width": 1280, "height": 800}, "device_scale_factor": 1}
MOBILE = {
    "viewport": {"width": 390, "height": 844},
    "device_scale_factor": 2,
    "is_mobile": True,
    "has_touch": True,
}

SETTLE_CSS = """
*, *::before, *::after {
  animation: none !important;
  transition: none !important;
  caret-color: transparent !important;
}
.toastify { display: none !important; }
/* the demo's library folders only exist inside Docker, so the health light would
   report a problem with the machine taking the screenshots, not with Earr */
#nav-system-dot { display: none !important; }
"""


def log(message: str) -> None:
    print(f"[readme] {message}", flush=True)


# --- frontend -----------------------------------------------------------------


def _download(url: str, target: Path) -> None:
    log(f"downloading {url}")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    with (
        urllib.request.urlopen(url) as response,  # pyright: ignore[reportAny]
        partial.open("wb") as out,
    ):
        shutil.copyfileobj(response, out)  # pyright: ignore[reportAny]
    _ = partial.replace(target)


def tailwind_binary() -> Path:
    """The Tailwind CLI at the version the Dockerfile builds with."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    match = re.search(r"tailwindcss/releases/download/v([\d.]+)/", dockerfile)
    if not match:
        raise RuntimeError("Could not find the Tailwind version in the Dockerfile")
    version = match.group(1)
    system = {"darwin": "macos", "linux": "linux"}.get(sys.platform)
    machine = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}
    arch = machine.get(platform.machine().lower())
    if not system or not arch:
        raise RuntimeError(f"No Tailwind build for {sys.platform}/{platform.machine()}")
    asset = f"tailwindcss-{system}-{arch}"
    binary = CACHE / f"{asset}-{version}"
    if not binary.exists():
        _download(
            f"https://github.com/tailwindlabs/tailwindcss/releases/download/v{version}/{asset}",
            binary,
        )
        binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
        if system == "macos":
            # recent macOS kills the release build on launch until it is re-signed
            _ = subprocess.run(
                ["codesign", "--force", "--sign", "-", str(binary)],
                check=True,
                capture_output=True,
            )
    return binary


def build_frontend() -> None:
    static = ROOT / "static"
    for name in ("daisyui.mjs", "daisyui-theme.mjs"):
        if not (static / name).exists():
            _download(
                f"https://github.com/saadeghi/daisyui/releases/latest/download/{name}",
                static / name,
            )
    log("building static/globals.css")
    _ = subprocess.run(
        [
            str(tailwind_binary()),
            "-i",
            "static/tw.css",
            "-o",
            "static/globals.css",
            "-m",
        ],
        check=True,
        capture_output=True,
    )
    from app.util.fetch_js import fetch_scripts

    fetch_scripts(True)


# --- demo instance ------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])  # pyright: ignore[reportAny]


def git_version() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except OSError, subprocess.CalledProcessError:
        return "main"
    return f"main-{sha}"


def _run(args: list[str], env: dict[str, str], what: str) -> None:
    result = subprocess.run(args, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{what} failed:\n{result.stdout}\n{result.stderr}")


class Demo:
    def __init__(self, base_url: str, password: str, summary: dict[str, object]):
        self.base_url: str = base_url
        self.password: str = password
        self.summary: dict[str, object] = summary

    @property
    def sources_asin(self) -> str:
        return str(self.summary["sources_asin"])


@contextmanager
def demo_instance() -> Iterator[Demo]:
    """A seeded Earr on a free port, backed by the Prowlarr stub."""
    workdir = Path(tempfile.mkdtemp(prefix="earr-readme-"))
    password = secrets.token_urlsafe(18)
    stub_port, app_port = free_port(), free_port()
    summary_file = workdir / "summary.json"
    # variables from the environment beat .env.local, so a developer's local
    # settings can't leak into the screenshots
    env = os.environ | {
        "EARR_APP__CONFIG_DIR": str(workdir / "config"),
        "EARR_APP__DEBUG": "false",
        "EARR_APP__LOG_LEVEL": "WARNING",
        "EARR_APP__VERSION": git_version(),
        "EARR_APP__BASE_URL": "",
        "EARR_APP__DEFAULT_REGION": "us",
        "EARR_APP__OPENAPI_ENABLED": "false",
        "EARR_DB__USE_POSTGRES": "false",
        "EARR_DEMO_PASSWORD": password,
        "EARR_DEMO_PROWLARR": f"http://127.0.0.1:{stub_port}",
        "EARR_DEMO_OUT": str(summary_file),
    }

    log("creating the demo database")
    _run([sys.executable, "-m", "alembic", "upgrade", "heads"], env, "alembic")
    log("seeding it with books from Audible")
    _run([sys.executable, "-m", "scripts.readme.seed"], env, "seeding")
    summary = json.loads(summary_file.read_text())  # pyright: ignore[reportAny]
    books: list[StubBook] = list(summary["books"].values())  # pyright: ignore[reportAny]

    stub = ProwlarrStub(stub_port, books)
    stub.start()
    app_log = (workdir / "app.log").open("w")
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(app_port),
        ],
        env=env,
        stdout=app_log,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{app_port}"
    try:
        _wait_for_health(base_url, server, workdir / "app.log")
        yield Demo(base_url, password, summary)  # pyright: ignore[reportAny]
    finally:
        server.terminate()
        try:
            _ = server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
        app_log.close()
        stub.stop()
        shutil.rmtree(workdir, ignore_errors=True)


def _wait_for_health(base_url: str, server: subprocess.Popen[bytes], app_log: Path):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"Earr exited during startup:\n{app_log.read_text()}")
        try:
            url = f"{base_url}/api/health"
            with urllib.request.urlopen(url, timeout=2) as r:  # pyright: ignore[reportAny]
                if r.status == 200:  # pyright: ignore[reportAny]
                    log(f"Earr is up on {base_url}")
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Earr did not start in time:\n{app_log.read_text()}")


# --- screenshots --------------------------------------------------------------


def login(browser: Browser, demo: Demo) -> dict[str, object]:
    context = browser.new_context()
    page = context.new_page()
    _ = page.goto(f"{demo.base_url}/login")
    page.fill("#username", "alex")
    page.fill("#password", demo.password)
    page.click("#submit")
    page.wait_for_url(lambda url: url.rstrip("/") == demo.base_url, timeout=30_000)
    state = context.storage_state()
    context.close()
    return dict(state)


def settle(page: Page) -> None:
    """Waits until lazy sections, images and fonts are in, and stills the page."""
    page.wait_for_load_state("networkidle")
    page.wait_for_function(
        "() => !document.querySelector('[hx-trigger=load]')", timeout=90_000
    )
    page.wait_for_load_state("networkidle")
    page.wait_for_function(
        "() => Array.from(document.images).every(i => i.complete)", timeout=60_000
    )
    page.evaluate("() => document.fonts.ready.then(() => true)")
    _ = page.add_style_tag(content=SETTLE_CSS)
    page.evaluate("() => document.activeElement && document.activeElement.blur()")
    page.mouse.move(0, 0)
    page.wait_for_timeout(400)


SEARCH = "brandon sanderson"
"""What the search screenshots search for. feature.html shows it in its URL bar."""


class Shot:
    def __init__(
        self,
        name: str,
        path: Callable[[Demo], str],
        wait_for: str | None = None,
        mobile: bool = False,
    ):
        self.name: str = name
        self.path: Callable[[Demo], str] = path
        self.wait_for: str | None = wait_for
        self.mobile: bool = mobile


SHOTS = [
    Shot("home", lambda d: "/"),
    Shot("search", lambda d: "/search?q=" + quote(SEARCH)),
    Shot("wishlist", lambda d: "/wishlist", wait_for="#book-table-body"),
    Shot(
        "sources",
        lambda d: f"/wishlist/sources/{d.sources_asin}",
        wait_for="#sources table tbody tr",
    ),
    Shot("activity", lambda d: "/activity", wait_for=".queue-item"),
    Shot("books", lambda d: "/library", wait_for=".library-book"),
    Shot("library", lambda d: "/settings/library", wait_for="#folder-template"),
    Shot(
        "mobile",
        lambda d: "/search?q=" + quote(SEARCH),
        wait_for=".book-card",
        mobile=True,
    ),
]


def take_screenshots(browser: Browser, demo: Demo) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    state = login(browser, demo)
    for variant, theme in THEMES.items():
        for mobile in (False, True):
            context = browser.new_context(
                storage_state=state,  # pyright: ignore[reportArgumentType]
                color_scheme=variant,  # pyright: ignore[reportArgumentType]
                reduced_motion="reduce",
                **(MOBILE if mobile else DESKTOP),  # pyright: ignore[reportArgumentType]
            )
            context.add_init_script(
                f"localStorage.setItem({json.dumps(THEME_STORAGE_KEY)}, {json.dumps(theme)})"
            )
            page = context.new_page()
            for shot in (s for s in SHOTS if s.mobile == mobile):
                _ = page.goto(demo.base_url + shot.path(demo))
                if shot.wait_for:
                    page.wait_for_selector(shot.wait_for, timeout=90_000)
                settle(page)
                target = SCREENSHOTS / f"{shot.name}-{variant}.png"
                _ = page.screenshot(path=target)
                log(f"wrote {target.relative_to(ROOT)}")
            context.close()


# --- graphics -----------------------------------------------------------------


def render_logo(browser: Browser) -> None:
    page = browser.new_page(viewport={"width": 512, "height": 512})
    page.set_content(f'<body style="margin:0">{ICON.read_text()}</body>')
    page.locator("svg").evaluate(
        "svg => (svg.style.width = svg.style.height = '512px')"
    )
    _ = page.locator("svg").screenshot(path=MEDIA / "logo.png", omit_background=True)
    page.close()
    log("wrote media/logo.png")


def render_feature(browser: Browser) -> None:
    page = browser.new_page(viewport={"width": 1280, "height": 640})
    _ = page.goto((BRAND / "feature.html").as_uri())
    page.wait_for_load_state("networkidle")
    page.evaluate("() => document.fonts.ready.then(() => true)")
    page.wait_for_function(
        "() => Array.from(document.images).every(i => i.complete && i.naturalWidth)"
    )
    _ = page.screenshot(path=MEDIA / "feature.png")
    page.close()
    log("wrote media/feature.png")


def main() -> None:
    os.chdir(ROOT)
    build_frontend()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            with demo_instance() as demo:
                take_screenshots(browser, demo)
            render_logo(browser)
            # last, because it shows two of the screenshots
            render_feature(browser)
        finally:
            browser.close()


if __name__ == "__main__":
    main()
