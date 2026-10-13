"""Open a real browser, let the user log in to Canvas, and hand back the session cookies."""

from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from rich.console import Console


def _launch(p, profile_dir: Path):
    # Prefer the system Chrome (SSO providers are friendlier to it), fall back to bundled Chromium.
    kwargs = dict(user_data_dir=str(profile_dir), headless=False, no_viewport=True)
    try:
        return p.chromium.launch_persistent_context(channel="chrome", **kwargs)
    except PlaywrightError:
        return p.chromium.launch_persistent_context(**kwargs)


def login(base_url: str, profile_dir: Path, console: Console) -> tuple[list[dict], str]:
    """Return (cookies, user_agent) once the browser session can access the Canvas API."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = _launch(p, profile_dir)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(base_url)

        with console.status("[bold cyan]Log in to Canvas in the browser window… (waiting)"):
            while True:
                if not ctx.pages:
                    raise SystemExit("Browser was closed before login completed.")
                try:
                    r = ctx.request.get(f"{base_url}/api/v1/users/self", fail_on_status_code=False)
                    ok = r.ok
                    r.dispose()
                except PlaywrightError:
                    raise SystemExit("Browser was closed before login completed.")
                if ok:
                    break
                time.sleep(1.5)

        cookies = ctx.cookies()
        try:
            user_agent = ctx.pages[0].evaluate("navigator.userAgent")
        except PlaywrightError:
            user_agent = "Mozilla/5.0"
        ctx.close()

    console.print("[green]✓ Logged in[/green]")
    return cookies, user_agent
