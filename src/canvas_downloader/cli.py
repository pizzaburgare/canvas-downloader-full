"""Entry point: login → pick courses → download."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rich.console import Console

from .api import CanvasAPI, fetch_courses
from .auth import login
from .download import CourseDownloader
from .tui import CoursePicker

CONFIG_DIR = Path.home() / ".config" / "canvas-downloader-full"
CONFIG_FILE = CONFIG_DIR / "config.json"


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


def main() -> None:
    ap = argparse.ArgumentParser(description="Download full Canvas courses, including Canvas-hosted videos.")
    ap.add_argument("--url", help="Canvas base URL, e.g. https://canvas.myschool.edu (remembered)")
    ap.add_argument("--out", type=Path, help="Output directory (default: ~/Downloads/Canvas, remembered)")
    ap.add_argument("--workers", type=int, default=4, help="Parallel downloads (default 4)")
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

    console.print(f"Opening [cyan]{url}[/cyan] — log in in the browser window (it closes automatically).")
    cookies, ua = login(url, CONFIG_DIR / "browser-profile", console)

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
