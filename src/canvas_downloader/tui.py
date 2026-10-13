"""Textual course picker: search, multi-select, video toggle."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, Footer, Header, Input, Label, SelectionList, Static, Switch
from textual.widgets.selection_list import Selection


@dataclass
class PickResult:
    courses: list[dict]
    include_videos: bool


def _term(c: dict) -> str:
    return (c.get("term") or {}).get("name") or ""


def _label(c: dict) -> Text:
    t = Text(c.get("name", "?"), style="bold")
    code = c.get("course_code")
    if code and code != c.get("name"):
        t.append(f"  {code}", style="cyan")
    term = _term(c)
    if term:
        t.append(f"  · {term}", style="dim")
    return t


class CoursePicker(App[PickResult | None]):
    TITLE = "Canvas Downloader"
    CSS = """
    #search { margin: 1 1 0 1; }
    #opts { height: auto; padding: 0 1; align: left middle; }
    #opts Label { padding: 1 1 0 0; }
    #opts Button { margin-left: 1; }
    #spacer { width: 1fr; }
    #courses { height: 1fr; margin: 0 1; border: round $primary; }
    #status { padding: 0 2; color: $text-muted; }
    """
    BINDINGS = [
        Binding("ctrl+a", "select_all", "Select all shown", priority=True),
        Binding("ctrl+n", "select_none", "Clear selection", priority=True),
        Binding("ctrl+t", "toggle_videos", "Toggle videos", priority=True),
        Binding("ctrl+d", "download", "Download", priority=True),
        Binding("escape", "quit", "Quit"),
    ]

    def __init__(self, courses: list[dict], user_name: str, out_dir: str):
        super().__init__()
        self.courses = {c["id"]: c for c in courses}
        self.order = [c["id"] for c in courses]
        self.selected: set[int] = set()
        self.shown: list[int] = []
        self.sub_title = f"{user_name} → {out_dir}"

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="Search courses (name, code, term)…  ↓/Enter to jump to list", id="search")
        with Horizontal(id="opts"):
            yield Label("Include Canvas-hosted videos")
            yield Switch(value=True, id="videos")
            yield Static(id="spacer")
            yield Button("Select all", id="all")
            yield Button("Clear", id="none")
            yield Button("Download", id="go", variant="success")
        yield SelectionList[int](id="courses")
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_list("")
        self.query_one("#search", Input).focus()

    # --- list management -------------------------------------------------
    def refresh_list(self, query: str) -> None:
        terms = query.lower().split()
        self.shown = []
        for cid in self.order:
            c = self.courses[cid]
            hay = f"{c.get('name', '')} {c.get('course_code', '')} {_term(c)}".lower()
            if all(t in hay for t in terms):
                self.shown.append(cid)
        sl = self.query_one("#courses", SelectionList)
        sl.clear_options()
        sl.add_options([Selection(_label(self.courses[cid]), cid, cid in self.selected) for cid in self.shown])
        self.update_status()

    def update_status(self) -> None:
        vids = "on" if self.query_one("#videos", Switch).value else "off"
        self.query_one("#status", Static).update(
            f"{len(self.selected)} selected · {len(self.shown)}/{len(self.order)} shown · videos {vids}"
            "   (space toggles, Ctrl+D downloads)"
        )

    def on_input_changed(self, event: Input.Changed) -> None:
        self.refresh_list(event.value)

    def focus_list(self) -> None:
        sl = self.query_one("#courses", SelectionList)
        if sl.option_count and sl.highlighted is None:
            sl.highlighted = 0
        sl.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.focus_list()

    def on_key(self, event) -> None:
        if event.key == "down" and self.focused is self.query_one("#search", Input):
            self.focus_list()
            event.stop()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        current = set(event.selection_list.selected)
        for cid in self.shown:
            if cid in current:
                self.selected.add(cid)
            else:
                self.selected.discard(cid)
        self.update_status()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        self.update_status()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        {"all": self.action_select_all, "none": self.action_select_none, "go": self.action_download}[event.button.id]()

    # --- actions -----------------------------------------------------------
    def action_select_all(self) -> None:
        self.selected.update(self.shown)
        self.query_one("#courses", SelectionList).select_all()

    def action_select_none(self) -> None:
        self.selected.clear()
        self.query_one("#courses", SelectionList).deselect_all()

    def action_toggle_videos(self) -> None:
        sw = self.query_one("#videos", Switch)
        sw.value = not sw.value

    def action_download(self) -> None:
        if not self.selected:
            self.notify("Select at least one course (space), or Ctrl+A for all shown.", severity="warning")
            return
        chosen = [self.courses[cid] for cid in self.order if cid in self.selected]
        self.exit(PickResult(chosen, self.query_one("#videos", Switch).value))
