# canvas-downloader-full

Download whole Canvas courses — files, modules, pages, assignments, announcements,
discussions, syllabus, and **Canvas-hosted videos** — using your own logged-in browser session.

## Setup

```bash
uv sync
uv run playwright install chromium   # only needed if Google Chrome isn't installed
```

## Usage

```bash
uv run canvas-dl                       # prompts for your Canvas URL the first time
uv run canvas-dl --url canvas.myschool.edu --out ~/Downloads/Canvas --workers 4
```

1. A browser window opens. Log in as usual (SSO/2FA are fine). It closes on its own once you're logged in.
   The session cookies are saved to `~/.config/canvas-downloader-full/session.json` (mode `600`), so later runs
   skip the browser entirely until the session expires. Use `--login` to force a fresh login. The browser
   profile is kept there too, so re-logging in is often instant.
2. The TUI lists your courses:
   - type to search (name, code, term), `↓`/`Enter` to move into the list
   - `space` toggles a course, `Ctrl+A` selects every course currently shown, `Ctrl+N` clears
   - `Ctrl+T` (or the switch) toggles **Include Canvas-hosted videos** (on by default)
   - `Ctrl+D` starts downloading, `Esc` quits
3. Downloads run in parallel with progress bars. Re-running skips files that are already complete.

## Output layout

```
<Course code> - <Course name>/
  files/            course Files tab, with its folder structure
  modules/NN - …/   files only reachable via modules + _module.md index of every item
  pages/            wiki pages as HTML
  assignments/      descriptions as HTML (+ your own submission attachments)
  announcements/  discussions/  Syllabus.html
  media/            Canvas media objects (uploaded/recorded videos)
  linked_files/     files linked from page/assignment HTML that weren't found elsewhere
  _download_log.txt anything locked, missing, or failed
```

## Notes

- "Canvas-hosted videos" means video files and Canvas media objects. Videos in external tools
  (Panopto, Kaltura, YuJa, YouTube, Canvas Studio) aren't downloaded; their links appear in `_module.md`.
- Only content your account can see is downloaded.
