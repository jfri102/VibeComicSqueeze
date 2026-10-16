"""Terminal UI for ComicSqueeze - a Textual app over engine.py."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Static,
)

from . import engine
from .engine import PdfPlan, human

SUFFIX_SAFE = re.compile(r"[^A-Za-z0-9._\- ()\[\]]")


@dataclass
class Row:
    path: Path
    plan: PdfPlan | None = None
    status: str = "queued"
    detail: str = ""
    out_bytes: int = 0
    ratio: float = 0.0


class Summary(Static):
    """Totals line above the table."""

    text = reactive("")

    def render(self) -> str:
        return self.text


class SqueezeApp(App):
    CSS = """
    Screen { layers: base; }
    #controls {
        height: auto;
        padding: 1 2 0 2;
    }
    #controls Horizontal { height: auto; }
    .field { width: 34; margin-right: 3; }
    .field Label { color: $text-muted; }
    #ratio-hint { color: $text-muted; padding: 0 2; height: 1; }
    #buttons { padding: 1 2; height: auto; }
    #buttons Button { margin-right: 2; }
    #summary { padding: 0 2; height: 1; color: $accent; }
    #table { height: 1fr; margin: 1 2 0 2; }
    #progress { padding: 0 2; height: auto; }
    #log { height: 10; margin: 1 2; border: round $primary; }
    .warn { color: $warning; }
    """

    # priority=True keeps a focused Input from swallowing the control keys.
    # Several combinations are unreliable on Windows, so each important action
    # has more than one way in:
    #   Ctrl+Q is XON flow control in many terminals and never reaches the app.
    #   Shift+Tab is often downgraded to plain Tab by legacy conhost.
    BINDINGS = [
        Binding("ctrl+r", "start", "Start", priority=True),
        Binding("f5", "start", "Start", priority=True, show=False),
        Binding("ctrl+x", "cancel", "Cancel", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("f10", "quit", "Quit", priority=True, show=False),
        Binding("ctrl+c", "quit", "Quit", priority=True, show=False),
        # Not priority: a bare q must still type normally inside the text fields.
        Binding("q", "quit_letter", "Quit", show=False),
        Binding("shift+tab", "prev_field", "Prev field", show=False),
        # priority: Input has its own handlers for these, and would eat them.
        Binding("ctrl+p", "prev_field", "Prev field", priority=True, show=False),
        Binding("ctrl+n", "next_field", "Next field", priority=True, show=False),
        Binding("escape", "focus_table", "Back to list", priority=True, show=False),
        Binding("space", "toggle_row", "Toggle file"),
        Binding("a", "toggle_all", "All/none"),
    ]

    TITLE = "ComicSqueeze"
    SUB_TITLE = "recompress comic PDFs to a target size ratio"

    running = reactive(False)

    def __init__(self, directory: Path, ratio: float = 0.5) -> None:
        super().__init__()
        self.directory = directory
        self.initial_ratio = ratio
        self.rows: list[Row] = []
        self.selected: set[int] = set()
        self._cancel = False

    # ---------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="controls"):
            with Horizontal():
                with Vertical(classes="field"):
                    yield Label("Target ratio (out / in)")
                    yield Input(
                        value=f"{self.initial_ratio:g}",
                        placeholder="0.5",
                        id="ratio",
                        restrict=r"[0-9.%]*",
                    )
                with Vertical(classes="field"):
                    yield Label("Output name suffix")
                    yield Input(value="_compressed", id="suffix")
                with Vertical(classes="field"):
                    yield Label("Output folder (blank = ./compressed)")
                    yield Input(placeholder="compressed", id="outdir")
        yield Static(id="ratio-hint")
        with Horizontal(id="buttons"):
            yield Button("Start", variant="success", id="start")
            yield Button("Cancel", variant="error", id="cancel", disabled=True)
            yield Button("Select all", id="all")
            # Clickable escape hatch: some terminals eat Ctrl+Q entirely.
            yield Button("Quit", id="quit")
        yield Summary(id="summary")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        with Vertical(id="progress"):
            yield ProgressBar(total=100, show_eta=False, id="bar")
        yield RichLog(id="log", markup=True, wrap=True, max_lines=500)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns("", "File", "Pages", "Size", "Result", "Ratio", "Status")
        self.query_one("#bar", ProgressBar).display = False
        self._update_hint()
        self.scan()

    # ------------------------------------------------------------- scanning
    @work(thread=True, exclusive=True)
    def scan(self) -> None:
        paths = engine.find_pdfs(self.directory)
        if not paths:
            self.call_from_thread(
                self.log_line, f"[red]No PDFs found in {self.directory}[/]"
            )
            return
        self.call_from_thread(self.log_line, f"Scanning {len(paths)} PDF(s)…")
        rows: list[Row] = []
        for path in paths:
            try:
                plan = engine.inspect(path)
                row = Row(path=path, plan=plan)
                if not plan.images:
                    row.status = "no images"
                elif plan.passthrough:
                    row.detail = f"{len(plan.passthrough)} page(s) copied as-is"
            except Exception as exc:  # keep scanning the rest
                row = Row(path=path, status="unreadable", detail=str(exc))
            rows.append(row)
            self.call_from_thread(self._set_rows, list(rows))
        self.call_from_thread(self.log_line, "[green]Scan complete.[/] Space toggles a file, Ctrl+R starts.")

    def _set_rows(self, rows: list[Row]) -> None:
        self.rows = rows
        self.selected = {
            i for i, r in enumerate(rows) if r.plan and r.plan.images
        }
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#table", DataTable)
        cursor = table.cursor_row
        table.clear()
        for i, row in enumerate(self.rows):
            plan = row.plan
            table.add_row(
                "[green]✓[/]" if i in self.selected else " ",
                row.path.name,
                str(plan.page_count) if plan else "-",
                human(plan.file_bytes) if plan else "-",
                human(row.out_bytes) if row.out_bytes else "-",
                f"{row.ratio:.3f}" if row.ratio else "-",
                row.detail or row.status,
                key=str(i),
            )
        if 0 <= cursor < len(self.rows):
            table.move_cursor(row=cursor)
        self._update_summary()

    def _update_summary(self) -> None:
        total_in = sum(
            r.plan.file_bytes for i, r in enumerate(self.rows) if r.plan and i in self.selected
        )
        done_in = sum(r.plan.file_bytes for r in self.rows if r.out_bytes and r.plan)
        done_out = sum(r.out_bytes for r in self.rows if r.out_bytes)
        parts = [f"{len(self.selected)} selected · {human(total_in)}"]
        if done_out:
            saved = done_in - done_out
            parts.append(
                f"done {human(done_out)} (saved {human(saved)}, "
                f"ratio {done_out / done_in:.3f})"
            )
        self.query_one("#summary", Summary).text = "   ".join(parts)

    # -------------------------------------------------------------- helpers
    def log_line(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)

    def _parse_ratio(self) -> float | None:
        raw = self.query_one("#ratio", Input).value.strip()
        if not raw:
            return None
        try:
            if raw.endswith("%"):
                val = float(raw[:-1]) / 100
            else:
                val = float(raw)
                if val > 1.0:  # user typed "60" meaning 60%
                    val /= 100
        except ValueError:
            return None
        if not 0.01 <= val <= 1.0:
            return None
        return val

    def _update_hint(self) -> None:
        hint = self.query_one("#ratio-hint", Static)
        val = self._parse_ratio()
        if val is None:
            hint.update("[red]Enter a ratio between 0.01 and 1.0 (e.g. 0.4 or 40%).[/]")
            return
        note = ""
        if val < 0.3:
            note = " — needs downscaling, pages will lose resolution"
        elif val > 0.9:
            note = " — very little to gain at this ratio"
        hint.update(f"Aiming for {val * 100:.0f}% of original size{note}")

    @on(Input.Changed, "#ratio")
    def _ratio_changed(self) -> None:
        self._update_hint()

    # -------------------------------------------------------------- actions
    def _move_focus(self, delta: int) -> None:
        """Step through the focus chain explicitly.

        Textual's focus_previous/focus_next are relative to the screen's own
        notion of focus and returned None here, so the index is computed by hand.
        """
        chain = [w for w in self.screen.focus_chain]
        if not chain:
            return
        cur = self.focused
        idx = chain.index(cur) if cur in chain else 0
        self.set_focus(chain[(idx + delta) % len(chain)])

    def action_prev_field(self) -> None:
        """Explicit backward focus, for terminals that mangle Shift+Tab."""
        self._move_focus(-1)

    def action_next_field(self) -> None:
        self._move_focus(1)

    def action_focus_table(self) -> None:
        self.set_focus(self.query_one("#table", DataTable))

    def _typing(self) -> bool:
        """True when a text field has focus, so letter keys must type normally."""
        return isinstance(self.focused, Input)

    async def action_quit(self) -> None:
        """Quit, but never mid-write: cancel first so no .part file is orphaned."""
        if self.running:
            self._cancel = True
            self.log_line("[yellow]Cancelling, then quitting…[/]")
            for _ in range(100):  # ~10s grace for the worker to notice
                if not self.running:
                    break
                await asyncio.sleep(0.1)
        self.exit()

    async def action_quit_letter(self) -> None:
        """Bare `q`. Separate action so check_action can disable it while typing
        without also disabling Ctrl+Q, which must work everywhere."""
        await self.action_quit()

    def check_action(self, action: str, parameters) -> bool:
        """Disable the bare-letter shortcuts while a text field has focus.

        Without this, typing "a" or "q" into the ratio box would toggle the
        selection or quit the app. The Ctrl/F-key bindings stay live because
        they are unambiguous.
        """
        if action in ("toggle_all", "toggle_row", "quit_letter") and self._typing():
            return False
        return True

    def action_toggle_row(self) -> None:
        table = self.query_one("#table", DataTable)
        i = table.cursor_row
        if not (0 <= i < len(self.rows)):
            return
        row = self.rows[i]
        if not (row.plan and row.plan.images):
            return
        self.selected.symmetric_difference_update({i})
        self._refresh_table()

    def action_toggle_all(self) -> None:
        eligible = {i for i, r in enumerate(self.rows) if r.plan and r.plan.images}
        self.selected = set() if self.selected == eligible else eligible
        self._refresh_table()

    @on(Button.Pressed, "#all")
    def _all_pressed(self) -> None:
        self.action_toggle_all()

    @on(Button.Pressed, "#start")
    def _start_pressed(self) -> None:
        self.action_start()

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#quit")
    async def _quit_pressed(self) -> None:
        await self.action_quit()

    def action_cancel(self) -> None:
        if self.running:
            self._cancel = True
            self.log_line("[yellow]Cancelling after the current page…[/]")

    def action_start(self) -> None:
        if self.running:
            return
        target = self._parse_ratio()
        if target is None:
            self.log_line("[red]Fix the target ratio first.[/]")
            return
        if not self.selected:
            self.log_line("[red]Nothing selected.[/]")
            return
        outdir_raw = self.query_one("#outdir", Input).value.strip()
        outdir = Path(outdir_raw) if outdir_raw else self.directory / "compressed"
        if not outdir.is_absolute():
            outdir = self.directory / outdir
        suffix = SUFFIX_SAFE.sub("", self.query_one("#suffix", Input).value)
        self._cancel = False
        self.run_batch(target, outdir, suffix)

    # ----------------------------------------------------------- the worker
    @work(thread=True, exclusive=True)
    def run_batch(self, target: float, outdir: Path, suffix: str) -> None:
        def ui(fn, *a):
            self.call_from_thread(fn, *a)

        ui(self._set_running, True)
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            ui(self.log_line, f"[red]Cannot create {outdir}: {exc}[/]")
            ui(self._set_running, False)
            return

        ui(self.log_line, f"[bold]Target ratio {target:.2f}[/] → {outdir}")
        indices = sorted(self.selected)
        started = time.time()

        for n, i in enumerate(indices, 1):
            if self._cancel:
                break
            row = self.rows[i]
            plan = row.plan
            if plan is None or not plan.images:
                continue

            dst = outdir / f"{plan.path.stem}{suffix}{plan.path.suffix}"
            if dst.resolve() == plan.path.resolve():
                row.status = "skipped (would overwrite source)"
                ui(self._refresh_table)
                ui(self.log_line, f"[red]{plan.path.name}: refusing to overwrite the source file.[/]")
                continue

            row.status = "calibrating"
            row.detail = ""
            ui(self._refresh_table)
            ui(self.log_line, f"[cyan]({n}/{len(indices)}) {plan.path.name}[/] calibrating…")

            try:
                cal = engine.calibrate(
                    plan,
                    target,
                    progress=lambda m: ui(self.log_line, f"    [dim]{m}[/]"),
                )
            except Exception as exc:
                row.status = "calibration failed"
                row.detail = str(exc)[:60]
                ui(self._refresh_table)
                ui(self.log_line, f"[red]{plan.path.name}: {exc}[/]")
                continue

            note = "" if cal.exact else " [yellow](closest reachable)[/]"
            ui(
                self.log_line,
                f"    settings [b]{cal.params.label()}[/] → predicted "
                f"{cal.predicted_ratio:.3f}{note}",
            )

            row.status = f"encoding {cal.params.label()}"
            ui(self._refresh_table)
            ui(self._show_bar, plan.page_count)

            def on_page(done: int, total: int, _i=i) -> None:
                ui(self._bar_to, done)
                self.rows[_i].status = f"page {done}/{total}"

            try:
                res = engine.compress(
                    plan,
                    cal.params,
                    dst,
                    on_page=on_page,
                    should_stop=lambda: self._cancel,
                )
            except Exception as exc:
                row.status = "failed"
                row.detail = str(exc)[:60]
                ui(self._refresh_table)
                ui(self.log_line, f"[red]{plan.path.name} failed: {exc}[/]")
                continue

            if res is None:
                row.status = "cancelled"
                ui(self._refresh_table)
                break

            row.out_bytes = res.out_bytes
            row.ratio = res.ratio
            row.status = "done"
            row.detail = (
                f"done · {cal.params.label()}"
                + (f" · {res.passthrough} copied" if res.passthrough else "")
            )
            ui(self._refresh_table)
            ui(
                self.log_line,
                f"    [green]✓[/] {human(res.in_bytes)} → [b]{human(res.out_bytes)}[/] "
                f"(ratio {res.ratio:.3f}) → {dst.name}",
            )

        ui(self._hide_bar)
        ui(self._set_running, False)
        elapsed = time.time() - started
        if self._cancel:
            ui(self.log_line, f"[yellow]Stopped after {elapsed:.0f}s.[/]")
        else:
            ui(self.log_line, f"[bold green]Finished in {elapsed:.0f}s.[/]")

    # ------------------------------------------------------- ui-thread bits
    def _set_running(self, value: bool) -> None:
        self.running = value
        self.query_one("#start", Button).disabled = value
        self.query_one("#cancel", Button).disabled = not value
        for wid in ("#ratio", "#suffix", "#outdir"):
            self.query_one(wid, Input).disabled = value

    def _show_bar(self, total: int) -> None:
        bar = self.query_one("#bar", ProgressBar)
        bar.display = True
        bar.update(total=total, progress=0)

    def _bar_to(self, done: int) -> None:
        self.query_one("#bar", ProgressBar).update(progress=done)
        self._update_summary()

    def _hide_bar(self) -> None:
        self.query_one("#bar", ProgressBar).display = False
        self._refresh_table()
