"""Entry point: login → pick courses → download."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx
from rich.console import Console

from .api import CanvasAPI, CanvasError, fetch_courses
from .auth import clear_session, load_session, login, save_session
from .download import CourseDownloader
from .tui import CoursePicker

CONFIG_DIR = Path.home() / ".config" / "canvas-downloader-full"
CONFIG_FILE = CONFIG_DIR / "config.json"
SESSION_FILE = CONFIG_DIR / "session.json"


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _normalize(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _try_saved_session(url: str, workers: int) -> tuple[CanvasAPI, dict] | None:
    saved = load_session(SESSION_FILE, url)
    if not saved:
        return None
    api = CanvasAPI(url, *saved, workers=workers)
    try:
        me = api.get("/api/v1/users/self")
    except (CanvasError, httpx.HTTPError, ValueError):
        # Expired session: Canvas returns 401 or redirects to the HTML login page.
        api.client.close()
        clear_session(SESSION_FILE)
        return None
    if not isinstance(me, dict) or "id" not in me:
        api.client.close()
        clear_session(SESSION_FILE)
        return None
    return api, me


def main() -> None:
    ap = argparse.ArgumentParser(description="Download full Canvas courses, including Canvas-hosted videos.")
    ap.add_argument("--url", help="Canvas base URL, e.g. https://canvas.myschool.edu (remembered)")
    ap.add_argument("--out", type=Path, help="Output directory (default: ~/Downloads/Canvas, remembered)")
    ap.add_argument("--workers", type=int, default=4, help="Parallel downloads (default 4)")
    ap.add_argument("--login", action="store_true", help="Ignore the saved session and log in again")
    args = ap.parse_args()

    console = Console()
    cfg = _load_config()

    url = args.url or cfg.get("url")
    if not url:
        url = console.input("[bold]Canvas URL[/bold] (e.g. canvas.myschool.edu): ")
    url = _normalize(url)
    out = (args.out or Path(cfg.get("out", Path.home() / "Downloads" / "Canvas"))).expanduser().resolve()

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"url": url, "out": str(out)}, indent=2))

    session = None
    if args.login:
        clear_session(SESSION_FILE)
    else:
        with console.status("Checking saved session…"):
            session = _try_saved_session(url, args.workers)
    if session:
        api, me = session
        console.print("[green]✓ Using saved session[/green]")
    else:
        console.print(f"Opening [cyan]{url}[/cyan] — log in in the browser window (it closes automatically).")
        cookies, ua = login(url, CONFIG_DIR / "browser-profile", console)
        save_session(SESSION_FILE, url, cookies, ua)
        api = CanvasAPI(url, cookies, ua, workers=args.workers)
        me = api.get("/api/v1/users/self")
    with console.status("Fetching courses…"):
        courses = fetch_courses(api)
    if not courses:
        raise SystemExit("No courses found for this account.")

    result = CoursePicker(courses, me.get("name", ""), str(out)).run()
    if not result:
        console.print("Cancelled.")
        return

    console.print(
        f"Downloading [bold]{len(result.courses)}[/bold] course(s) to [cyan]{out}[/cyan] "
        f"(Canvas videos: {'on' if result.include_videos else 'off'})"
    )
    for course in result.courses:
        CourseDownloader(api, course, out, result.include_videos, args.workers, console).run()
    console.print(f"\n[bold green]All done.[/bold green] Files are in {out}")


if __name__ == "__main__":
    main()
