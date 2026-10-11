"""The lte-dash sidebar UI. Requires Textual; see the package docstring for the rest."""

import signal
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Markdown, Static, Tree

from .._tmux import option
from .control import read_status
from .options import HOME_KIND_OPTION, HOME_STATUS_OPTION

HOME = "home"  # tree node data for the home entry

HELP_TEXT = """\
# lte-dash

Select a pane to show it on the right. It is the real pane, swapped in:
type into it, run vim, scroll back.

| key | action |
|---|---|
| `j` / `k`, arrows | browse; the viewport follows |
| | the top line counts all panes; window rows read `running/total`, then stopped and failed |
| `Enter` / `l` | focus the viewport |
| `g` | go to the pane in its own window (`prefix g` comes back, see README) |
| `h` | home: the script's output |
| `x` | stop the pane (SIGINT, then SIGTERM, SIGKILL) |
| `r` | rerun the pane's command |
| `X` | stop **all** panes, same escalation; panes stay up to inspect |
| `Q` | stop all panes, then kill the session |
| `s` | zoom the viewport (`prefix z` unzooms) |
| `[` / `]` | narrow / widen the sidebar |
| `q` | quit: put every pane back, close this window |

Back to the sidebar: click it, `prefix Left`, or `prefix g`.
Colors: green running / exit 0, red failed, yellow stopped by `x` / `X`
("stopped TERM" or "KILL" if SIGINT was not enough), magenta unknown.

`X` and `Q` ask for a second press. Stop, rerun, `X` and `Q` need the
session's script to still be running. Killing the session any other way
only sends SIGHUP.
"""

STATUS_COLORS = {"running": "green", "ok": "green", "failed": "red",
                 "stopped": "#e5c847", "unknown": "magenta"}  # hex: the theme maps "yellow" to orange


def describe(entry):
    """(short label, color class) for one status-file entry."""
    if entry.get("running"):
        return "running", "running"
    stopped = entry.get("stopped")
    if stopped:
        # Escalation past SIGINT is worth seeing: it means no clean shutdown.
        return ("stopped" if stopped == "SIGINT" else f"stopped {stopped[3:]}"), "stopped"
    exit_status = entry.get("exit_status") or {}
    code, sig = exit_status.get("code"), exit_status.get("signal")
    if sig is not None:
        try:
            name = signal.Signals(sig).name
        except ValueError:
            name = f"signal {sig}"
        return name + ("?" if exit_status.get("inferred") else ""), "failed"
    if code is not None:
        return f"exit {code}", "ok" if code == 0 else "failed"
    return "unknown", "unknown"


def describe_long(entry):
    if entry.get("running"):
        return "running"
    if entry.get("stopped"):
        plain = dict(entry, stopped=None)
        return f"stopped, needed {entry['stopped']}: {describe_long(plain)}"
    exit_status = entry.get("exit_status") or {}
    code, sig = exit_status.get("code"), exit_status.get("signal")
    if sig is not None:
        text = f"killed by {describe(entry)[0].rstrip('?')} ({sig})"
        return text + ", inferred from 128+N" if exit_status.get("inferred") else text
    if code is not None:
        return f"exited {code}"
    return "unknown"


class HelpScreen(ModalScreen):
    """Key reference. The sidebar is narrow, so its pane is zoomed while open."""

    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > Vertical {
        width: 80; max-width: 100%; height: auto; max-height: 100%;
        background: $panel; border: thick $primary;
    }
    HelpScreen Markdown { padding: 0 1; }
    """

    BINDINGS = [
        Binding("q,escape,question_mark", "close", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Markdown(HELP_TEXT)

    def action_close(self):
        self.app.pop_screen()


class PaneTree(Tree):
    """Enter focuses the viewport instead of Tree's select/toggle."""

    BINDINGS = [
        Binding("enter,l,right", "app.focus_view", "Focus", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]


class DashApp(App):
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    #summary { height: auto; padding: 0 1; border-bottom: hkey $primary; }
    #panes { height: 1fr; scrollbar-size-vertical: 1; }
    #details { height: auto; max-height: 9; padding: 0 1; border-top: hkey $primary; }
    """

    BINDINGS = [
        Binding("h", "home", "Home", show=False),
        Binding("g", "go_to", "Go to"),
        Binding("x", "stop", "Stop"),
        Binding("r", "rerun", "Rerun", show=False),
        Binding("X", "stop_all", "Stop all", show=False),
        Binding("Q", "shutdown", "Shutdown", show=False),
        Binding("s", "zoom", "Zoom", show=False),
        Binding("left_square_bracket", "resize(-4)", "Narrow", show=False),
        Binding("right_square_bracket", "resize(4)", "Widen", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    POLL_INTERVAL = 0.5
    ONLINE_INTERVAL = 2.0
    CONFIRM_WINDOW = 3.0  # seconds to press X / Q a second time
    SHOW_DELAY = 0.15  # debounce: holding j should not swap every pane in passing

    def __init__(self, viewport, status_path, control):
        super().__init__()
        self.viewport = viewport
        self.status_path = status_path
        self.control = control
        self.entries = {}       # pane id -> status-file entry
        self.pane_nodes = {}    # pane id -> TreeNode
        self.window_nodes = {}  # window name -> TreeNode
        self.alive = set()      # pane ids tmux still has
        self.online = control.online()
        self._show_timer = None
        self._path = None       # status file read last poll
        self._armed = None  # (action, time) awaiting a confirming press

    def compose(self) -> ComposeResult:
        tree = PaneTree("session", id="panes")
        tree.show_root = False
        tree.auto_expand = False
        tree.guide_depth = 2
        yield Static(id="summary")
        yield tree
        yield Static(id="details")
        yield Footer()

    def on_mount(self):
        self.home_kind = self.viewport.option(HOME_KIND_OPTION)
        self.home_status = ""
        tree = self.query_one(PaneTree)
        self.home_node = tree.root.add_leaf(self._home_label(), data=HOME)
        tree.root.expand()
        tree.cursor_line = 0
        tree.focus()
        self.poll()
        self.set_interval(self.POLL_INTERVAL, self.poll)
        self.set_interval(self.ONLINE_INTERVAL, self.check_online)

    # ---------- status ----------

    def poll(self):
        self.viewport.repair()
        self.alive = set(self.viewport.panes())
        path = self.status_path() if callable(self.status_path) else self.status_path
        if path != self._path and self._path:
            # A new TmuxSession took over (the next test). Its predecessor's
            # last write can land within one poll of the switch: read it once more.
            for entry in read_status(self._path) or ():
                self.entries[entry["pane_id"]] = entry
        self._path = path
        entries = read_status(path) if path else None
        if entries is not None:
            # Merge, don't replace: under lte-session each test's
            # TmuxSession.current() writes a new file, and the panes of earlier
            # tests stay listed with how they ended.
            self.entries.update({e["pane_id"]: e for e in entries})
        tree = self.query_one(PaneTree)
        for entry in self.entries.values():
            pane_id = entry["pane_id"]
            window, _, name = entry["name"].rpartition(":")
            node = self.pane_nodes.get(pane_id)
            if node is not None and node.parent is not self.window_nodes.get(window):
                # Its window was renamed (lte-session keeps a finished test's
                # windows as '<test>/<window>'): move it to the new group.
                old_group = node.parent
                node.remove()
                del self.pane_nodes[pane_id]
                if not old_group.children:
                    self.window_nodes = {w: n for w, n in self.window_nodes.items()
                                         if n is not old_group}
                    old_group.remove()
            if pane_id not in self.pane_nodes:
                wnode = self.window_nodes.get(window)
                if wnode is None:
                    wnode = tree.root.add(window or "(unnamed)", data=None, expand=True)
                    self.window_nodes[window] = wnode
                self.pane_nodes[pane_id] = wnode.add_leaf(name or pane_id, data=pane_id)
        self._relabel()
        self._show_details()

    def check_online(self):
        online = self.control.online()
        home_status = (option(self.viewport.window, HOME_STATUS_OPTION)
                       if self.home_kind == "command" else "")
        if (online, home_status) != (self.online, self.home_status):
            self.online, self.home_status = online, home_status
            self.home_node.set_label(self._home_label())

    def _home_label(self):
        label = Text("home", style="bold")
        if self.home_kind == "command":
            # lte-session: the command's own state. Between tests no script
            # is listening, which is normal, so that's not shown.
            status = self.home_status or "running"
            color = ("running" if status == "running" else
                     "ok" if status == "exited 0" else "failed")
            label.append(f"  {status}", style=STATUS_COLORS[color])
        elif not self.online:
            label.append("  script exited", style="dim")
        return label

    def _relabel(self):
        total = {}
        for window, wnode in self.window_nodes.items():
            pane_ids = [n.data for n in wnode.children]
            names = [self.entries[p]["name"].rpartition(":")[2] for p in pane_ids]
            width = min(max(len(n) for n in names), 18)
            counts = {}
            for pane_id, name in zip(pane_ids, names):
                short, kind = describe(self.entries[pane_id])
                if pane_id not in self.alive and kind == "running":
                    short, kind = "gone", "unknown"  # its last status was never written
                counts[kind] = counts.get(kind, 0) + 1
                total[kind] = total.get(kind, 0) + 1
                color = STATUS_COLORS[kind]
                label = Text()
                label.append("\u25cf ", style=color)
                label.append(name.ljust(width))
                label.append(f" {short}", style=color)
                if pane_id not in self.alive:
                    label.append(" closed", style="dim")
                self.pane_nodes[pane_id].set_label(label)
            # A kept test window is '<test>/<window>': lead with the window
            # and counts, which fit; the test name trails, dimmed.
            test, _, short_window = (window or "(unnamed)").rpartition("/")
            summary = Text(short_window, style="bold")
            summary.append(f"  {counts.get('running', 0)}/{len(pane_ids)}", style="dim")
            if counts.get("stopped"):
                summary.append(f"  {counts['stopped']} stopped", style=STATUS_COLORS["stopped"])
            if counts.get("failed"):
                summary.append(f"  {counts['failed']} failed", style="red")
            if test:
                summary.append(f"  {test}", style="dim italic")
            wnode.set_label(summary)
        self._summarize(total)

    def _summarize(self, counts):
        """The top line: running, stopped and failed always; exit 0 and
        unknown when there are any."""
        text = Text()
        for kind, word, always in (("running", "running", True), ("stopped", "stopped", True),
                                   ("failed", "failed", True), ("ok", "ok", False),
                                   ("unknown", "unknown", False)):
            n = counts.get(kind, 0)
            if n or always:
                if text:
                    text.append("  ")
                text.append(f"{n} {word}", style=STATUS_COLORS[kind] if n else "dim")
        self.query_one("#summary", Static).update(text)

    def _selected(self):
        node = self.query_one(PaneTree).cursor_node
        return node.data if node is not None else None

    def _show_details(self):
        pane_id = self._selected()
        details = self.query_one("#details", Static)
        entry = self.entries.get(pane_id)
        if pane_id == HOME:
            if self.home_kind == "command":
                text = "The command's own terminal (lte-session). Enter to type into it."
            else:
                text = "The script's stdout, stderr and logging."
            details.update(Text(text, style="dim"))
            return
        if entry is None:
            node = self.query_one(PaneTree).cursor_node
            window = next((w for w, n in self.window_nodes.items() if n is node), None)
            details.update(Text(window, style="bold") if window else "")
            return
        _, kind = describe(entry)
        text = Text()
        text.append(entry["name"], style="bold")
        text.append(f"  pane {pane_id}, {entry.get('mode', '')} mode\n", style="dim")
        text.append(describe_long(entry) + "\n", style=STATUS_COLORS[kind])
        pids = ",".join(str(p) for p in entry.get("target_pids") or ()) or "-"
        text.append(f"pid {pids}\n", style="dim")
        text.append(entry.get("command", ""))
        details.update(text)

    # ---------- viewport ----------

    def on_tree_node_highlighted(self, event):
        self._show_details()
        if self._show_timer is not None:
            self._show_timer.stop()
        self._show_timer = self.set_timer(self.SHOW_DELAY, self._show_selected)

    def _target(self):
        """The pane id the selection maps to, or None for a window node or a
        pane tmux no longer has."""
        data = self._selected()
        if data == HOME:
            return self.viewport.home
        if data in self.alive:
            return data
        return None

    def _show_selected(self):
        self._show_timer = None
        target = self._target()
        if target:
            self.viewport.show(target)

    def action_focus_view(self):
        self._show_selected()
        view = self.viewport.view
        if view:
            self.viewport.run("select-pane", "-t", view)

    def action_go_to(self):
        target = self._target()
        if self._selected() == HOME or not target:
            return
        if self._show_timer is not None:
            self._show_timer.stop()
            self._show_timer = None
        self.viewport.go_to(target)

    def action_home(self):
        self.query_one(PaneTree).move_cursor(self.home_node)

    def action_zoom(self):
        self.action_focus_view()
        view = self.viewport.view
        if view:
            self.viewport.run("resize-pane", "-Z", "-t", view)

    def action_resize(self, delta):
        self.viewport.run("resize-pane", "-t", self.viewport.sidebar,
                           "-R" if delta > 0 else "-L", str(abs(delta)))

    def action_help(self):
        self.viewport.run("resize-pane", "-Z", "-t", self.viewport.sidebar)
        self.push_screen(HelpScreen(), lambda _: self.viewport.run(
            "resize-pane", "-Z", "-t", self.viewport.sidebar))

    # ---------- actions ----------

    def _action(self, cmd, verb):
        pane_id = self._selected()
        entry = self.entries.get(pane_id)
        if entry is None:
            return
        if self.control.send(cmd, pane_id):
            self.notify(f"{verb} {entry['name']}")
        else:
            self.notify("The session's script is not running.", severity="warning")

    def action_stop(self):
        self._action("stop", "stopping")

    def action_rerun(self):
        self._action("respawn", "rerunning")

    def _confirmed(self, action, prompt):
        """True on the second press within CONFIRM_WINDOW."""
        now = time.monotonic()
        if self._armed and self._armed[0] == action and now - self._armed[1] < self.CONFIRM_WINDOW:
            self._armed = None
            return True
        self._armed = (action, now)
        self.notify(prompt, timeout=self.CONFIRM_WINDOW)
        return False

    def _session_action(self, cmd, done):
        if not self.control.send(cmd, None):
            self.notify("The session's script is not running.", severity="warning")
            return
        self.notify(done)

    def action_stop_all(self):
        running = sum(1 for e in self.entries.values() if e.get("running"))
        if self._confirmed("stop_all", f"Press X again to stop all {running} running panes."):
            self._session_action("stop_all", "Stopping all panes: SIGINT, then SIGTERM, SIGKILL.")

    def action_shutdown(self):
        if self._confirmed("shutdown", "Press Q again to stop all panes and kill the session."):
            self._session_action("shutdown", "Stopping all panes, then killing the session.")
