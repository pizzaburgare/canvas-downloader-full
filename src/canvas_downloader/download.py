"""Crawl a Canvas course and download everything reachable."""

from __future__ import annotations

import html
import json
import mimetypes
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    ProgressColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.text import Text

from .api import CanvasAPI, CanvasError

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp"}

FILE_RE = re.compile(r"/files/(\d+)")
MEDIA_ATTACHMENT_RE = re.compile(r"media_attachments_iframe/(\d+)")
MEDIA_ID_RE = re.compile(r"""(?:media_comment_|data-media-id=["']|media_objects_iframe/)([\w-]+)""")

HTML_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title><base href="{base}/">
<style>body{{font-family:system-ui,sans-serif;max-width:900px;margin:2em auto;padding:0 1em;line-height:1.5}}
img,video,iframe{{max-width:100%}}</style></head>
<body><h1>{title}</h1>
{body}
</body></html>
"""


def safe(name: str, maxlen: int = 120) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name)).strip().strip(".")
    return name[:maxlen].strip() or "untitled"


def safe_filename(name: str, maxlen: int = 150) -> str:
    name = safe(name, maxlen=10_000)
    p = Path(name)
    stem, suffix = p.stem, p.suffix if len(p.suffix) <= 10 else ""
    if not suffix:
        stem = name
    return stem[: maxlen - len(suffix)].strip() + suffix


def is_video(f: dict) -> bool:
    ct = (f.get("content-type") or f.get("content_type") or "").lower()
    name = f.get("display_name") or f.get("filename") or ""
    return ct.startswith("video/") or f.get("mime_class") == "video" or Path(name).suffix.lower() in VIDEO_EXT


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def course_dir_name(c: dict) -> str:
    name, code = c.get("name") or f"course_{c['id']}", c.get("course_code")
    return f"{code} - {name}" if code and code != name else name


@dataclass
class Job:
    url: str
    dest: Path
    size: int | None


class SizeColumn(ProgressColumn):
    """Bytes for file tasks, file counts for the course-level task."""

    def __init__(self):
        super().__init__()
        self._dl = DownloadColumn()

    def render(self, task):
        if task.fields.get("count"):
            return Text(f"{int(task.completed)}/{int(task.total or 0)} files", style="progress.download")
        return self._dl.render(task)


class SpeedColumn(TransferSpeedColumn):
    def render(self, task):
        return Text("") if task.fields.get("count") else super().render(task)


class CourseDownloader:
    def __init__(self, api: CanvasAPI, course: dict, out_root: Path, include_videos: bool, workers: int, console: Console):
        self.api = api
        self.course = course
        self.cid = course["id"]
        self.root = out_root / safe(course_dir_name(course))
        self.include_videos = include_videos
        self.workers = workers
        self.console = console

        self.jobs: list[Job] = []
        self.file_paths: dict[int, Path | None] = {}  # file id -> destination (None = skipped)
        self.claimed: set[Path] = set()
        self.saved_pages: dict[str, Path | None] = {}
        self.linked_files: set[int] = set()
        self.linked_media: set[str] = set()
        self.done_media: set[str] = set()
        self.queued_video_names: set[str] = set()
        self.skipped_videos = 0
        self.log_lines: list[str] = []
        self._lock = threading.Lock()

    # --- helpers ---------------------------------------------------------------
    def log(self, msg: str) -> None:
        with self._lock:
            self.log_lines.append(msg)

    def c(self, path: str) -> str:
        return f"/api/v1/courses/{self.cid}{path}"

    def api_list(self, path: str, **params) -> list:
        try:
            return self.api.get_all(path, **params)
        except CanvasError as e:
            self.log(f"skip {path}: {e}")
            return []

    def api_get(self, path: str, quiet: bool = False, **params):
        try:
            return self.api.get(path, **params)
        except CanvasError as e:
            if not quiet:
                self.log(f"skip {path}: {e}")
            return None

    def claim(self, directory: Path, name: str, tag) -> Path:
        name = safe_filename(name)
        p = directory / name
        if p in self.claimed:
            p = directory / f"{Path(name).stem} ({tag}){Path(name).suffix}"
        self.claimed.add(p)
        return p

    def rel(self, p: Path | None) -> str:
        return str(p.relative_to(self.root)) if p else "(not downloaded)"

    def queue_file(self, f: dict, directory: Path) -> Path | None:
        fid = f.get("id")
        if fid in self.file_paths:
            return self.file_paths[fid]
        name = f.get("display_name") or f.get("filename") or f"file_{fid}"
        if is_video(f) and not self.include_videos:
            self.skipped_videos += 1
            self.file_paths[fid] = None
            return None
        url = f.get("url")
        if not url or f.get("locked_for_user"):
            self.log(f"locked/unavailable file: {name} ({f.get('lock_explanation', '')})")
            self.file_paths[fid] = None
            return None
        dest = self.claim(directory, name, fid)
        self.file_paths[fid] = dest
        if is_video(f):
            self.queued_video_names.add(name.lower())
        self.jobs.append(Job(url, dest, f.get("size")))
        return dest

    def save_html(self, directory: Path, title: str, body: str | None, tag) -> Path:
        title = title or "untitled"
        dest = self.claim(directory, f"{title}.html", tag)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            HTML_TEMPLATE.format(title=html.escape(title), base=self.api.base, body=body or ""), encoding="utf-8"
        )
        self.scan_html(body or "")
        return dest

    def scan_html(self, body: str) -> None:
        self.linked_files.update(int(m) for m in FILE_RE.findall(body))
        self.linked_files.update(int(m) for m in MEDIA_ATTACHMENT_RE.findall(body))
        self.linked_media.update(MEDIA_ID_RE.findall(body))

    # --- crawl steps ---------------------------------------------------------------
    def do_info(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "_course.json").write_text(json.dumps(self.course, indent=2), encoding="utf-8")

    def do_syllabus(self) -> None:
        c = self.api_get(self.c(""), **{"include[]": ["syllabus_body"]})
        if c and c.get("syllabus_body"):
            self.save_html(self.root, "Syllabus", c["syllabus_body"], "syllabus")

    def do_files(self) -> None:
        folders = {f["id"]: f.get("full_name", "") for f in self.api_list(self.c("/folders"))}
        for f in self.api_list(self.c("/files")):
            full = folders.get(f.get("folder_id"), "course files")
            d = self.root / "files"
            for part in full.split("/")[1:]:  # drop the "course files" root
                if part:
                    d = d / safe(part)
            self.queue_file(f, d)

    def save_page(self, slug: str) -> Path | None:
        if slug not in self.saved_pages:
            p = self.api_get(self.c(f"/pages/{quote(slug, safe='')}"))
            self.saved_pages[slug] = (
                self.save_html(self.root / "pages", p.get("title") or slug, p.get("body"), slug) if p else None
            )
        return self.saved_pages[slug]

    def do_pages(self) -> None:
        for p in self.api_list(self.c("/pages")):
            self.save_page(p["url"])

    def do_modules(self) -> None:
        for m in self.api_list(self.c("/modules"), **{"include[]": ["items"]}):
            mdir = self.root / "modules" / safe(f"{_int(m.get('position')):02d} - {m.get('name', '')}")
            items = m.get("items") or self.api_list(self.c(f"/modules/{m['id']}/items"))
            lines = [f"# {m.get('name', '')}", ""]
            for it in items:
                kind, title = it.get("type"), it.get("title", "")
                indent = "  " * _int(it.get("indent"))
                if kind == "SubHeader":
                    lines += ["", f"## {title}"]
                    continue
                if kind == "File":
                    fid = it.get("content_id")
                    if fid in self.file_paths:
                        ref = self.rel(self.file_paths[fid])
                    else:
                        f = self.api_get(self.c(f"/files/{fid}"))
                        ref = self.rel(self.queue_file(f, mdir)) if f else "(unavailable)"
                elif kind == "Page" and it.get("page_url"):
                    ref = self.rel(self.save_page(it["page_url"]))
                elif kind in ("ExternalUrl", "ExternalTool"):
                    ref = it.get("external_url") or it.get("html_url", "")
                else:
                    ref = it.get("html_url", "")
                lines.append(f"{indent}- [{kind}] {title} — {ref}")
            mdir.mkdir(parents=True, exist_ok=True)
            (mdir / "_module.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def do_assignments(self) -> None:
        for a in self.api_list(self.c("/assignments"), **{"include[]": ["submission"]}):
            name = a.get("name") or f"assignment_{a['id']}"
            due = a.get("due_at") or "no due date"
            body = f"<p><em>Due: {due} · Points: {a.get('points_possible')}</em></p>{a.get('description') or ''}"
            self.save_html(self.root / "assignments", name, body, a["id"])
            for att in (a.get("submission") or {}).get("attachments") or []:
                self.queue_file(att, self.root / "assignments" / safe(f"{name} - my submission"))

    def _topics(self, folder: str, **params) -> None:
        for t in self.api_list(self.c("/discussion_topics"), **params):
            title = t.get("title") or f"topic_{t['id']}"
            body = f"<p><em>{t.get('posted_at') or ''} · {(t.get('author') or {}).get('display_name', '')}</em></p>{t.get('message') or ''}"
            self.save_html(self.root / folder, title, body, t["id"])
            for att in t.get("attachments") or []:
                self.queue_file(att, self.root / folder / "attachments")

    def do_announcements(self) -> None:
        self._topics("announcements", only_announcements="true")

    def do_discussions(self) -> None:
        self._topics("discussions")

    def queue_media(self, mo: dict) -> None:
        mid = mo.get("media_id") or mo.get("id")
        if not mid or mid in self.done_media:
            return
        self.done_media.add(mid)
        title = mo.get("user_entered_title") or mo.get("title") or mid
        if title.lower() in self.queued_video_names:
            return  # same video already queued as a course file
        sources = [s for s in mo.get("media_sources") or [] if s.get("url") or s.get("src")]
        if not sources:
            self.log(f"no downloadable sources for media {mid} ({title})")
            return
        best = max(sources, key=lambda s: (_int(s.get("height")), _int(s.get("bitrate")), _int(s.get("size"))))
        ext = (
            ("." + best["fileExt"]) if best.get("fileExt")
            else mimetypes.guess_extension((best.get("content_type") or "").split(";")[0]) or ".mp4"
        )
        name = title if Path(title).suffix.lower() in VIDEO_EXT else f"{title}{ext}"
        dest = self.claim(self.root / "media", name, mid)
        self.jobs.append(Job(best.get("url") or best["src"], dest, None))

    def media_info(self, mid: str) -> dict | None:
        for path in (f"/media_objects/{mid}/info", f"/api/v1/media_objects/{mid}"):
            d = self.api_get(path, quiet=True)
            if isinstance(d, dict) and d.get("media_sources"):
                d.setdefault("media_id", mid)
                return d
        self.log(f"could not resolve media object {mid}")
        return None

    def do_media(self) -> None:
        if not self.include_videos:
            return
        for mo in self.api_list(self.c("/media_objects")):
            self.queue_media(mo)

    def do_linked(self) -> None:
        for fid in sorted(self.linked_files - self.file_paths.keys()):
            f = self.api_get(self.c(f"/files/{fid}"), quiet=True) or self.api_get(f"/api/v1/files/{fid}", quiet=True)
            if f:
                self.queue_file(f, self.root / "linked_files")
            else:
                self.log(f"linked file {fid} not accessible")
        if self.include_videos:
            for mid in sorted(self.linked_media - self.done_media):
                info = self.media_info(mid)
                if info:
                    self.queue_media(info)

    # --- downloading ---------------------------------------------------------------
    def fetch(self, job: Job, progress: Progress) -> None:
        if job.dest.exists() and (not job.size or job.dest.stat().st_size == job.size):
            return
        job.dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = job.dest.with_name(job.dest.name + ".part")
        label = job.dest.name if len(job.dest.name) <= 45 else job.dest.name[:42] + "…"
        task = progress.add_task(label, total=job.size)
        try:
            for attempt in range(3):
                try:
                    progress.reset(task, total=job.size)
                    with self.api.client.stream("GET", job.url) as r:
                        r.raise_for_status()
                        total = _int(r.headers.get("content-length")) or job.size
                        progress.update(task, total=total)
                        with open(tmp, "wb") as fh:
                            for chunk in r.iter_bytes(1 << 20):
                                fh.write(chunk)
                                progress.advance(task, len(chunk))
                    tmp.replace(job.dest)
                    return
                except Exception as e:
                    if attempt == 2:
                        tmp.unlink(missing_ok=True)
                        raise
                    self.log(f"retry {job.dest.name}: {e}")
                    time.sleep(2 * (attempt + 1))
        finally:
            progress.remove_task(task)

    def do_downloads(self) -> int:
        if not self.jobs:
            return 0
        failed = 0
        progress = Progress(
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            SizeColumn(),
            SpeedColumn(),
            TimeRemainingColumn(),
            console=self.console,
        )
        with progress:
            overall = progress.add_task("[bold]Total", total=len(self.jobs), count=True)

            def work(job: Job):
                nonlocal failed
                try:
                    self.fetch(job, progress)
                except Exception as e:
                    failed += 1
                    self.log(f"FAILED {self.rel(job.dest)}: {e}")
                finally:
                    progress.advance(overall)

            with ThreadPoolExecutor(self.workers) as ex:
                list(ex.map(work, self.jobs))
        return failed

    # --- entry ---------------------------------------------------------------
    def run(self) -> None:
        self.console.rule(f"[bold]{course_dir_name(self.course)}")
        steps = [
            ("course info", self.do_info),
            ("files", self.do_files),
            ("modules", self.do_modules),
            ("pages", self.do_pages),
            ("assignments", self.do_assignments),
            ("announcements", self.do_announcements),
            ("discussions", self.do_discussions),
            ("syllabus", self.do_syllabus),
            ("media / videos", self.do_media),
            ("linked files & embedded media", self.do_linked),
        ]
        with self.console.status("") as status:
            for label, fn in steps:
                status.update(f"Scanning {label}… ({len(self.jobs)} files queued)")
                try:
                    fn()
                except Exception as e:
                    self.log(f"step '{label}' failed: {e!r}")

        self.console.print(f"Queued [bold]{len(self.jobs)}[/bold] files"
                           + (f", skipped {self.skipped_videos} videos" if self.skipped_videos else ""))
        failed = self.do_downloads()

        if self.log_lines:
            (self.root / "_download_log.txt").write_text("\n".join(self.log_lines) + "\n", encoding="utf-8")
        msg = f"[green]✓ Done[/green] → {self.root}"
        if failed:
            msg += f"  [red]{failed} failed[/red]"
        if self.log_lines:
            msg += "  (see _download_log.txt)"
        self.console.print(msg)
