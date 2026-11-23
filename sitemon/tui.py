"""Terminal dashboard: ``sitemon tui`` for every site on a hub, or for the agent
on this Pi. Needs the ``tui`` extra (Textual)."""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import quote

import aiohttp
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.worker import Worker
from textual.widgets import Button, DataTable, Footer, Input, Label, Sparkline, Static

from ._commands import Command
from ._duration import human_duration
from ._events import SigningKey
from .client import ApiError, get_json, send_command

NAMES = {
    "v": ("voltage", "V"),
    "a": ("current", "A"),
    "w": ("power", "W"),
    "temp_c": ("temperature", "°C"),
    "rh": ("humidity", "%"),
    "hpa": ("pressure", "hPa"),
    "link_count": ("links", ""),
    "rf_links": ("RF links", ""),
    "queue": ("queued events", ""),
    "fwd_w": ("forward power", "W"),
    "ref_w": ("reflected power", "W"),
    "rl_db": ("return loss", "dB"),
    "swr": ("SWR", ""),
}
RANGES = (7, 30, 90, 365)
STATUS_WORDS = {"critical": "CRITICAL", "silent": "NO CONTACT", "warn": "WARN", "ok": "OK"}
WORST_FIRST = {"critical": 0, "silent": 1, "warn": 2, "ok": 3}
HUB_REFRESH = 15.0
AGENT_REFRESH = 5.0
_CONTROLS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")

CSS = """
Screen { background: ansi_default; }
#top { padding: 0 1; text-style: bold; }
#problem { padding: 0 1; color: $error; }
.heading { padding: 1 1 0 1; text-style: bold; }
.note { padding: 0 1; color: $text-muted; }
.none { display: none; }
DataTable > .datatable--header { background: ansi_default; text-style: bold underline; }
DataTable { height: auto; max-height: 24; margin: 0 1; }
#sites { height: 1fr; max-height: 100%; }
#charts { height: auto; }
.chart { height: 1; margin: 0 1; }
.chart .name { width: 24; }
.chart Sparkline { width: 1fr; min-width: 10; }
.chart .val { width: auto; padding-left: 2; }
#dialog { width: 64; height: auto; padding: 1 2; border: round $primary; background: ansi_default; }
#dialog Input { margin: 1 0 0 0; }
#dialog Horizontal { height: auto; margin-top: 1; }
#dialog Button { margin-right: 2; }
#result { margin-top: 1; }
ConfirmCommand { align: center middle; }
"""


def plain(s: object) -> Text:
    """Text from a hub, a site or an error, shown as it is: no markup, no escape sequences."""
    return Text(_CONTROLS.sub("", str(s)))


def label(metric: str) -> str:
    src, _, key = metric.partition(".")
    name = NAMES.get(key)
    return f"{src} {name[0]}" if name else metric


def fmt(v: Any, metric: str = "") -> str:
    unit = NAMES.get(metric.rpartition(".")[2], ("", ""))[1]
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int) and not unit:
        return str(v)
    if isinstance(v, (int, float)):
        # 12.05 V and 12.0 V can be the difference between fine and a warning
        if unit == "V":
            s = f"{v:.2f}"
        else:
            s = f"{v:.0f}" if abs(v) >= 100 else f"{v:.1f}" if abs(v) >= 10 else f"{v:.2f}"
        return f"{s} {unit}" if unit else s
    if isinstance(v, list):
        return ", ".join(map(str, v)) or "none"
    if isinstance(v, dict):
        return "; ".join(f"{k}: {fmt(x)}" for k, x in v.items()) or "none"
    return "n/a" if v is None else str(v)


def ago(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    return "just now" if seconds < 5 else f"{human_duration(seconds)} ago"


def status_text(status: str, dark: bool) -> Text:
    warn = "yellow" if dark else "red"
    style = {"critical": "bold red", "silent": "bold magenta", "warn": f"bold {warn}"}
    return Text(STATUS_WORDS.get(status, status.upper()), style=style.get(status, "green"))


def theme_for(env: Mapping[str, str]) -> str:
    """The ANSI theme draws on the terminal's own colours and background; this
    only picks contrast for the accents, from COLORFGBG ("fg;bg") where the
    terminal sets it."""
    bg = env.get("COLORFGBG", "").rpartition(";")[2]
    return "ansi-light" if bg in ("7", "15") else "ansi-dark"


def readings(latest: Mapping[str, Any], key: str) -> str:
    """Every source's ``key`` reading, named by source when there's more than one."""
    found = [
        (metric.partition(".")[0], fmt(info["value"], metric))
        for metric, info in sorted(latest.items())
        if metric.endswith("." + key) and isinstance(info.get("value"), (int, float))
    ]
    if len(found) == 1:
        return found[0][1]
    return "  ".join(f"{src} {val}" for src, val in found)


def fill(table: DataTable[Any], rows: list[tuple[str, list[Any]]]) -> None:
    """Replace a table's rows, keeping the cursor on the same row if it's still there."""
    keep = None
    if table.row_count and table.cursor_row < table.row_count:
        keep = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
    table.clear()
    for key, cells in rows:
        table.add_row(*(plain(c) if isinstance(c, str) else c for c in cells), key=key)
    keys = [k for k, _ in rows]
    if keep in keys:
        table.move_cursor(row=keys.index(keep))
    # A table with nothing in it gives way to its "-none" note, where it has one.
    for note in table.screen.query(f"#{table.id}-none"):
        note.display = not rows
        table.display = bool(rows)


class Base(App[None]):
    CSS = CSS
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, url: str) -> None:
        super().__init__(ansi_color=True)
        self.url = url.rstrip("/")
        self.session: aiohttp.ClientSession | None = None

    @property
    def dark(self) -> bool:
        return self.current_theme.dark

    async def on_mount(self) -> None:
        self.theme = theme_for(os.environ)
        self.session = aiohttp.ClientSession()
        self.push_screen(self.first_screen())

    async def on_unmount(self) -> None:
        if self.session is not None:
            await self.session.close()

    def first_screen(self) -> Screen[None]:
        raise NotImplementedError

    async def fetch(self, path: str) -> Any:
        assert self.session is not None
        return await get_json(self.session, self.url + path)


class Refreshing(Screen[None]):
    """A screen that loads in the background now and every ``every`` seconds,
    and says so when the source can't be reached."""

    every = HUB_REFRESH
    BINDINGS: ClassVar[list[BindingType]] = [Binding("r", "reload", "Refresh")]
    app: Base

    def on_mount(self) -> None:
        self.loaded_at: float | None = None
        self.worker: Worker[None] | None = None
        self.action_reload()
        self.set_interval(self.every, self.tick)

    def tick(self) -> None:
        # A slow load is left to finish rather than restarted, or it never lands.
        if self.worker is None or not self.worker.is_running:
            self.action_reload()

    def action_reload(self) -> None:
        self.worker = self.run_worker(self._load(), exclusive=True, group="load")

    async def _load(self) -> None:
        problem = self.query_one("#problem", Static)
        try:
            await self.load()
        except (KeyError, TypeError) as e:
            # Some other server, or a hub too old for this TUI.
            problem.update(
                plain(f"{self.app.url} answered, but not like a sitemon hub or agent ({e!r}).")
            )
            return
        except ApiError as e:
            if self.loaded_at is None:
                for w in self.query(".loading").results(Static):
                    w.update("")
            since = (
                ""
                if self.loaded_at is None
                else f" Showing what was there at {time.strftime('%H:%M:%S', time.localtime(self.loaded_at))}."
            )
            problem.update(plain(f"{e}. Trying again every {self.every:g} s.{since}"))
            return
        self.loaded_at = time.time()
        problem.update("")

    async def load(self) -> None:
        raise NotImplementedError


class SitesScreen(Refreshing):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("q", "app.quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Static(plain(f"sitemon  {self.app.url}"), id="top")
        yield Static("", id="problem")
        yield Static("Loading sites...", id="empty", classes="note loading")
        yield DataTable(id="sites", cursor_type="row")
        yield Footer()

    async def load(self) -> None:
        groups = await self.app.fetch("/api/groups")
        sites = []
        for g in groups:
            data = await self.app.fetch(f"/api/group/{quote(g['slug'], safe='')}")
            sites += [(data["name"], s) for s in data["sites"]]
        sites.sort(key=lambda gs: (WORST_FIRST.get(gs[1]["status"], 9), gs[1]["name"]))
        table = self.query_one("#sites", DataTable)
        if not table.columns:
            cols = ["Status", "Site", "Group", "Supply", "Temp", "Last contact", "Problem"]
            if len(groups) < 2:
                cols.remove("Group")
            table.add_columns(*cols)
        rows = []
        for group, s in sites:
            alerts = sorted(
                s["alerts"], key=lambda a: (a["rule"] != "silence", a["severity"] != "critical")
            )
            cells = [
                status_text(s["status"], self.app.dark),
                s["name"],
                group,
                readings(s["latest"], "v"),
                readings(s["latest"], "temp_c"),
                ago(s["last_contact_s"]),
                alerts[0]["message"] if alerts else "",
            ]
            if len(groups) < 2:
                del cells[2]
            rows.append((s["id"], cells))
        fill(table, rows)
        empty = self.query_one("#empty", Static)
        empty.update("" if rows else "No sites are configured on this hub yet.")
        empty.display = not rows

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        site = event.row_key.value
        if site is not None:
            self.app.push_screen(SiteScreen(site))


class SiteScreen(Refreshing):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("d", "range", "Range"),
        Binding("p", "relay('pulse')", "Pulse relay"),
        Binding("o", "relay('on')", "Relay on"),
        Binding("f", "relay('off')", "Relay off"),
    ]
    app: HubApp

    def __init__(self, site: str) -> None:
        super().__init__()
        self.site = site
        self.days = RANGES[0]
        self.name_ = site
        self.relays: list[str] = []

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(plain(self.site), id="top")
            yield Static("", id="problem")
            yield Static("Loading...", id="status", classes="note loading")
            yield Label("", id="range", classes="heading")
            yield Vertical(id="charts")
            yield Label("Relays", classes="heading")
            yield Static("", id="relay-note", classes="note")
            yield DataTable(id="relays", cursor_type="row")
            yield Static("This site has no relays.", id="relays-none", classes="note none")
            yield Label("Commands", classes="heading")
            yield DataTable(id="commands", cursor_type="row")
            yield Static("None sent yet.", id="commands-none", classes="note none")
            yield Label("Latest values", classes="heading")
            yield DataTable(id="latest", cursor_type="row")
            yield Static("Nothing reported yet.", id="latest-none", classes="note none")
            yield Label("Alerts, last 30 days", classes="heading")
            yield DataTable(id="history", cursor_type="row")
            yield Static("None.", id="history-none", classes="note none")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#relays", DataTable).add_columns("Relay", "State")
        self.query_one("#commands", DataTable).add_columns("Command", "Result", "Detail", "Sent")
        self.query_one("#latest", DataTable).add_columns("Reading", "Value", "Age")
        self.query_one("#history", DataTable).add_columns("Severity", "Alert", "Opened", "Resolved")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "relay":
            return bool(self.relays) and self.app.key is not None
        return True

    def action_range(self) -> None:
        self.days = RANGES[(RANGES.index(self.days) + 1) % len(RANGES)]
        self.action_reload()

    def tick(self) -> None:
        # A year of points is costly for the hub to draw; long ranges refresh on r or d.
        if self.days == RANGES[0]:
            super().tick()

    async def load(self) -> None:
        s = await self.app.fetch(f"/api/site/{quote(self.site, safe='')}?days={self.days}")
        self.name_ = s["name"]
        self.query_one("#top", Static).update(plain(f"{s['group_name']} / {s['name']}"))
        st = Text.assemble(
            status_text(s["status"], self.app.dark), f"  last contact {ago(s['last_contact_s'])}"
        )
        for a in s["alerts"]:
            st.append(plain(f"\n{a['severity']}: {a['message']}"))
        self.query_one("#status", Static).update(st)
        self.query_one("#range", Label).update(
            f"Last {round(s['days'])} days  (d: change range, r: refresh)"
        )
        await self._charts(s)
        self._relays(s)
        fill(
            self.query_one("#commands", DataTable),
            [
                (str(i), [_command_text(c), c["status"], c["detail"] or "", _when(c["created"])])
                for i, c in enumerate(s["commands"])
            ],
        )
        latest = s["latest"]
        fill(
            self.query_one("#latest", DataTable),
            [
                (m, [m, fmt(info["value"], m), ago(info["age_s"])])
                for m, info in sorted(latest.items())
                if not m.startswith("_") and not (m.endswith(".fault") and info["value"] is None)
            ],
        )
        fill(
            self.query_one("#history", DataTable),
            [
                (
                    str(i),
                    [
                        a["severity"],
                        a["message"],
                        _when(a["opened"]),
                        _when(a["resolved"]) if a["resolved"] else "open",
                    ],
                )
                for i, a in enumerate(reversed(s["recent_alerts"]))
            ],
        )

    async def _charts(self, s: dict[str, Any]) -> None:
        charts = self.query_one("#charts", Vertical)
        await charts.remove_children()
        rows: list[Widget] = []
        for m, pts in s["spark"].items():
            cur = s["latest"].get(m)
            val = fmt(cur["value"], m) if cur else ""
            trend = s["trends"].get(m)
            if trend is not None:
                val += f"  ({_trend_text(trend)})"
            rows.append(
                Horizontal(
                    Label(plain(label(m)), classes="name"),
                    Sparkline([p[1] for p in pts]),
                    Label(plain(val), classes="val"),
                    classes="chart",
                )
            )
        if not rows:
            rows.append(Static("No readings in this range.", classes="note"))
        await charts.mount_all(rows)

    def _relays(self, s: dict[str, Any]) -> None:
        relays = {
            m.partition(".")[2]: info["value"]
            for m, info in s["latest"].items()
            if m.startswith("relay.") and isinstance(info["value"], bool)
        }
        self.relays = sorted(relays)
        fill(
            self.query_one("#relays", DataTable),
            [(r, [r, "on" if relays[r] else "off"]) for r in self.relays],
        )
        note = self.query_one("#relay-note", Static)
        if not relays:
            note.update("")
        elif self.app.key is None:
            note.update("Start with --key <your operator key> to send commands.")
        else:
            note.update("Pick a relay, then p to pulse it, o to switch it on, f to switch it off.")
        self.refresh_bindings()

    def action_relay(self, action: str) -> None:
        table = self.query_one("#relays", DataTable)
        if not table.row_count:
            return
        relay = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        if relay is not None:
            self.app.push_screen(
                ConfirmCommand(self.site, self.name_, relay, action), lambda _: self.action_reload()
            )


def _command_text(c: dict[str, Any]) -> str:
    args = c.get("args", {})

    def arg(k: str) -> str:
        v = args.get(k)
        return str(v) if isinstance(v, (str, int, float)) and not isinstance(v, bool) else ""

    if c.get("kind") == "announce":
        return f'say "{arg("text")}"'
    secs = f" {arg('seconds')} s" if arg("seconds") else ""
    return f"{arg('relay') or '?'} {arg('action') or '?'}{secs}"


def _trend_text(per_week: float) -> str:
    if abs(per_week) < 0.01:
        return "nightly low steady"
    return f"nightly low {'falling' if per_week < 0 else 'rising'} {abs(per_week):.2f} V a week"


def _when(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


class ConfirmCommand(ModalScreen[None]):
    """Asks before a relay command goes out, then waits for the site's answer."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "close", "Close")]
    app: HubApp
    WAIT = 90.0

    def __init__(self, site: str, site_name: str, relay: str, action: str) -> None:
        super().__init__()
        self.site, self.site_name, self.relay, self.action = site, site_name, relay, action

    def compose(self) -> ComposeResult:
        verb = {"pulse": "Pulse", "on": "Switch on", "off": "Switch off"}[self.action]
        with Vertical(id="dialog"):
            where = (
                self.site_name if self.site_name == self.site else f"{self.site_name} ({self.site})"
            )
            yield Label(plain(f"{verb} {self.relay} at {where}?"), classes="heading")
            yield Static(
                "This switches equipment at the site. The command is signed with your key, "
                "and the site ignores it if it hasn't collected it within 2 minutes."
            )
            if self.action != "off":
                yield Input(placeholder="Seconds (blank for the relay's own limit)", id="seconds")
            with Horizontal():
                yield Button("Send", id="send", variant="primary")
                yield Button("Cancel", id="cancel")
            yield Static("", id="result")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send":
            self.send()
        else:
            self.dismiss(None)

    def on_input_submitted(self) -> None:
        self.send()

    def action_close(self) -> None:
        self.dismiss(None)

    def send(self) -> None:
        if self.query_one("#send", Button).disabled:
            return
        result = self.query_one("#result", Static)
        args: dict[str, Any] = {"relay": self.relay, "action": self.action}
        inputs = self.query("#seconds")
        raw = inputs.first(Input).value.strip() if inputs else ""
        if raw:
            try:
                seconds = float(raw)
            except ValueError:
                seconds = math.nan
            if not (math.isfinite(seconds) and seconds > 0):
                result.update(plain(f"{raw!r} isn't a number of seconds."))
                return
            args["seconds"] = seconds
        assert self.app.key is not None
        cmd = Command.create(self.site, "relay", args).signed(self.app.key)
        self.query_one("#send", Button).disabled = True
        self.query_one("#cancel", Button).label = "Close"
        result.update(
            plain(f"Sent. Waiting for {self.site_name} to collect it on its next sync...")
        )
        self.run_worker(self._send(cmd), exclusive=True)

    async def _send(self, cmd: Command) -> None:
        result = self.query_one("#result", Static)
        assert self.app.session is not None
        try:
            status, detail = await send_command(self.app.session, self.app.url, cmd, self.WAIT)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            result.update(
                plain(
                    f"No confirmation from the hub: {e or type(e).__name__}. The command may be "
                    "queued, so check Commands before sending again."
                )
            )
            return
        result.update(plain(f"{status}: {detail}" if detail else status))


class HubApp(Base):
    TITLE = "sitemon"

    def __init__(self, url: str, key: SigningKey | None = None) -> None:
        super().__init__(url)
        self.key = key

    def first_screen(self) -> Screen[None]:
        return SitesScreen()


class AgentScreen(Refreshing):
    every = AGENT_REFRESH
    BINDINGS: ClassVar[list[BindingType]] = [Binding("q", "app.quit", "Quit")]

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(plain(f"sitemon agent  {self.app.url}"), id="top")
            yield Static("", id="problem")
            yield Static("Loading...", id="status", classes="note loading")
            yield Label("Hubs", classes="heading")
            yield DataTable(id="hubs", cursor_type="row")
            yield Static("No hubs configured.", id="hubs-none", classes="note none")
            yield Label("Sensor faults", classes="heading")
            yield Static("", id="faults", classes="note")
            yield Label("Readings", classes="heading")
            yield DataTable(id="readings", cursor_type="row")
            yield Static("Nothing read yet.", id="readings-none", classes="note none")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#hubs", DataTable).add_columns("Hub", "To send", "Last sync", "Error")
        self.query_one("#readings", DataTable).add_columns("Reading", "Value", "Age")

    async def load(self) -> None:
        st = await self.app.fetch("/status.json")
        self.query_one("#top", Static).update(plain(f"{st['site']}  sitemon {st['version']}"))
        line = (
            f"up {human_duration(st['uptime_s'])}  queue {st['queue']}  dropped {st['dropped']}  "
            f"last sync {ago(st['last_sync_age_s'])}"
        )
        if st["last_sync_error"]:
            line += f"\nlast sync failed: {st['last_sync_error']}"
        self.query_one("#status", Static).update(plain(line))
        fill(
            self.query_one("#hubs", DataTable),
            [
                (url, [url, str(h["lag"]), ago(h["last_ok_age_s"]), h["error"]])
                for url, h in st["hubs"].items()
            ],
        )
        faults = st["faults"]
        self.query_one("#faults", Static).update(
            plain("\n".join(f"{k}: {v}" for k, v in faults.items()) if faults else "None.")
        )
        now = time.time()
        rows = []
        for src, vals in sorted(st["latest"].items()):
            t = vals.get("_t")
            for k, v in sorted(vals.items()):
                if not k.startswith("_"):
                    m = f"{src}.{k}"
                    rows.append(
                        (m, [m, fmt(v, m), ago(now - t) if isinstance(t, (int, float)) else ""])
                    )
        fill(self.query_one("#readings", DataTable), rows)


class AgentApp(Base):
    TITLE = "sitemon agent"

    def first_screen(self) -> Screen[None]:
        return AgentScreen()
