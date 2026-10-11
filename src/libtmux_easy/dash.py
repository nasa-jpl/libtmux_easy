"""lte-dash: a status sidebar for a libtmux_easy session.

    lte-dash --status FILE [--ctl SOCKET]

Runs as the left pane of the session's dashboard window (see
TmuxSession.dashboard()). The right pane is a viewport: selecting a pane in
the sidebar swaps the real pane into it with 'tmux swap-pane', so it is fully
interactive. The "home" pane (a 'tail -F' of the script's output) is the
placeholder that fills the selected pane's slot in its own window while it is
on show, so every window keeps its layout.

This module is stdlib-only, like wrap.py. The UI lives in dash_app.py and is
imported only by main(), so the library can use Viewport without Textual.

A program that is not a TmuxSession can open a dashboard too: 'lte-dash
--open SESSION ...', or open_window(). It then writes
the status file itself and answers the control socket's commands.

    lte-dash --open SESSION --status FILE --ctl SOCKET --home-log FILE
"""

import argparse
import importlib.util
import json
import os
import shlex
import shutil
import signal
import socket
import sys
from pathlib import Path

from . import keys
from ._tmux import option, tmux as _tmux
from .wrap import DIR_OPTION, SOCKET_NAME

# Window options on the dashboard window. They hold the viewport's state in
# tmux itself, so a restarted sidebar and the library agree on it.
HOME_OPTION = "@lte_dash_home"   # pane id of the home/placeholder pane
VIEW_OPTION = "@lte_dash_view"   # pane id currently in the viewport
FROM_OPTION = "@lte_dash_from"   # window the viewed pane was swapped out of
HOME_CMD_OPTION = "@lte_dash_home_cmd"
WIDTH_OPTION = "@lte_dash_width"  # the sidebar's initial width

# "command" when the home pane runs the user's command (lte-session): then
# quitting the sidebar must leave the window, and with it the command, alone.
HOME_KIND_OPTION = "@lte_dash_home_kind"

# Session options, for key bindings outside the dashboard (README "Dashboard").
WINDOW_OPTION = "@lte_dash_window"    # the dashboard window's id
SIDEBAR_OPTION = "@lte_dash_sidebar"  # the sidebar pane's id

# Session option: the current status file. A sidebar started without
# --status follows it (and @lte_dir's control socket), so it keeps working as
# one TmuxSession.current() after another adopts the session (lte-session).
STATUS_OPTION = "@lte_status"

# Session options set by lte-session: a dir that outlives each test (status
# files go there, so the sidebar sees each test's final state), and the
# command's own state for the home entry ("running", then e.g. "exited 1").
RUN_DIR_OPTION = "@lte_run_dir"
HOME_STATUS_OPTION = "@lte_home_status"

HOME_LOG = "home.log"  # in the session's control dir; see TmuxSession.tee_home()

# The prefix key the sidebar borrows while it runs (keys.py): back to the
# dashboard from any window of its session, with no ~/.tmux.conf entry.
# Elsewhere the key keeps its usual binding. "" borrows nothing.
KEY = "g"
KEY_COMMAND = (f"run-shell \"tmux -S '#{{socket_path}}' select-window -t '#{{{WINDOW_OPTION}}}'"
               f" && tmux -S '#{{socket_path}}' select-pane -t '#{{{SIDEBAR_OPTION}}}'\"")

INSTALL_HINT = (
    "lte-dash needs Textual (>= 1.0). Install one of:\n"
    "  pipx install 'libtmux-easy[dash]'    # puts lte-dash on PATH\n"
    "  pip install textual                  # into the script's venv\n"
)


def launcher():
    """How the dashboard window runs lte-dash: under this interpreter when it
    has Textual (e.g. a venv), else an installed lte-dash (e.g. pipx), which
    brings its own. Raises RuntimeError when neither exists."""
    if importlib.util.find_spec("textual") is not None:
        src = Path(__file__).resolve().parents[1]
        return (f"env PYTHONPATH={shlex.quote(str(src))} "
                f"{shlex.quote(sys.executable)} -m libtmux_easy.dash")
    if shutil.which("lte-dash"):
        return "lte-dash"
    raise RuntimeError(INSTALL_HINT)


def open_window(session, status_file, home_log, ctl=None, *, name="dash", width=36,
                mouse=True, run=_tmux, sidebar_cmd=None, home_cmd=None, key=KEY):
    """Create a dashboard window in 'session' (an id like '$1', or a name; not
    '=name', which tmux 3.4's set-option rejects as "no such session").
    Returns (window id, sidebar pane id).

    The home pane follows 'home_log'; the sidebar reads 'status_file' and
    sends actions to 'ctl'. The caller keeps both up to date. 'sidebar_cmd'
    is how to run lte-dash (default: launcher()). Raises RuntimeError, before
    touching the session, if lte-dash cannot run.

    'status_file' None: the sidebar follows the session's @lte_status and
    @lte_dir options instead. 'home_cmd' replaces the home pane's tail of
    'home_log' (which may then be None) with a command of the caller's; the
    sidebar then never closes the window (see HOME_KIND_OPTION). 'key': the
    prefix key the sidebar borrows while it runs (KEY; "" for none)."""
    sidebar_cmd = sidebar_cmd or launcher()
    home_kind = "command" if home_cmd else "log"
    if home_cmd is None:
        Path(home_log).touch()
        # Ctrl-C in the home pane must not end it; SIG_IGN survives exec.
        tail = f"trap '' INT; exec tail -n +1 -F {shlex.quote(home_log)}"
        home_cmd = f"sh -c {shlex.quote(tail)}"
    # First, before the session's other windows ('-b' shifts them up).
    out = run("new-window", "-b", "-d", "-t", f"{session}:^", "-n", name,
              "-P", "-F", "#{window_id} #{pane_id}", home_cmd)
    window_id, home = out[0].split()
    for opt, value in ((HOME_OPTION, home), (VIEW_OPTION, home),
                       (HOME_CMD_OPTION, home_cmd), (WIDTH_OPTION, str(width)),
                       (HOME_KIND_OPTION, home_kind)):
        run("set-option", "-wq", "-t", window_id, opt, value)

    if status_file:
        sidebar_cmd = f"{sidebar_cmd} --status {shlex.quote(status_file)}"
    if ctl:
        sidebar_cmd += f" --ctl {shlex.quote(ctl)}"
    sidebar_cmd += f" --key {shlex.quote(key)}"
    sidebar = run("split-window", "-h", "-b", "-d", "-l", str(width), "-t", home,
                  "-P", "-F", "#{pane_id}", sidebar_cmd)[0].strip()
    run("select-pane", "-t", home, "-T", "home")
    run("select-pane", "-t", sidebar, "-T", "lte-dash")
    run("set-option", "-q", "-t", session, WINDOW_OPTION, window_id)
    run("set-option", "-q", "-t", session, SIDEBAR_OPTION, sidebar)
    if mouse:
        run("set-option", "-q", "-t", session, "mouse", "on")
    return window_id, sidebar


class Viewport:
    """The dashboard window's viewport slot.

    State, in window options: HOME is the placeholder pane, VIEW is whichever
    pane is in the viewport. When VIEW != HOME, HOME sits in VIEW's original
    slot (in window FROM). show() always swaps the occupant home first, so at
    most one pane is ever away from its window.

    'run' executes a tmux command and returns its stdout lines. The default
    uses the tmux binary (right for a process inside the session); the
    library passes its own server's cmd()."""

    def __init__(self, window, run=_tmux, sidebar=None):
        self.window = window
        self.run = run
        self.sidebar = sidebar or option(window, SIDEBAR_OPTION, run=run) or None
        self._layout = None  # (pane count, window_layout), last seen intact

    def option(self, name):
        """A window option of the dashboard window, or ''."""
        return option(self.window, name, "-wq", run=self.run)

    def _set(self, name, value):
        self.run("set-option", "-wq", "-t", self.window, name, value)

    @property
    def home(self):
        return self.option(HOME_OPTION)

    @property
    def view(self):
        return self.option(VIEW_OPTION)

    def panes(self):
        """pane id -> window id, for every pane in the session."""
        out = self.run("list-panes", "-s", "-t", self.window, "-F", "#{pane_id} #{window_id}")
        return dict(line.split() for line in out if len(line.split()) == 2)

    def show(self, pane_id):
        """Put 'pane_id' in the viewport. The home pane id shows home."""
        self.repair()
        home, view = self.home, self.view
        if pane_id == view:
            return
        if view != home:
            self._put_back(home, view)
            home = self.home  # _put_back may have replaced it
        if pane_id != home:
            from_window = self.panes().get(pane_id, "")
            self.run("swap-pane", "-d", "-s", pane_id, "-t", home)
            self._set(FROM_OPTION, from_window)
        self._set(VIEW_OPTION, pane_id)

    def go_to(self, pane_id):
        """Put 'pane_id' back in its own window and switch to it there."""
        self.restore()
        self.run("select-window", "-t", pane_id)
        self.run("select-pane", "-t", pane_id)

    def restore(self):
        """Send the viewed pane back to its window, leaving home in view."""
        self.repair()
        home, view = self.home, self.view
        if view and view != home:
            self._put_back(home, view)
            self._set(VIEW_OPTION, home)

    def _put_back(self, home, view):
        panes = self.panes()
        if home in panes:
            self.run("swap-pane", "-d", "-s", home, "-t", view)
            return
        # Home is gone (killed while parked), so 'view' has no slot to swap
        # back into. Rejoin its window as a new split, then make a new home.
        from_window = self.option(FROM_OPTION)
        if from_window:
            self.run("join-pane", "-d", "-s", view, "-t", from_window)
        self._new_home()

    def repair(self):
        """Recover from panes vanishing under the dashboard: the viewed pane
        exiting (its viewport slot goes with it) or home being killed."""
        panes = self.panes()
        home, view = self.home, self.view
        if view in panes and panes[view] == self.window:
            # Intact (a missing home is handled by _put_back). Remember the
            # layout, mouse resizes and extra panes included, for _fit().
            count = sum(1 for w in panes.values() if w == self.window)
            layout = self.run("display-message", "-p", "-t", self.window, "#{window_layout}")
            if layout:
                self._layout = (count, layout[0].strip())
            return
        if panes.get(home) == self.window:
            pass  # home already back in the viewport
        elif home in panes:
            # The viewed pane is gone and home is stranded in its old slot.
            self.run("join-pane", "-d", "-h", "-s", home, "-t", self.sidebar or self.window)
            self._fit()
        elif home not in panes:
            self._new_home()
            return
        self._set(VIEW_OPTION, home)

    def _new_home(self):
        cmd = self.option(HOME_CMD_OPTION) or "cat"
        out = self.run("split-window", "-d", "-h", "-t", self.sidebar or self.window,
                       "-P", "-F", "#{pane_id}", cmd)
        if not out:
            return
        home = out[0].strip()
        self.run("select-pane", "-t", home, "-T", "home")
        self._set(HOME_OPTION, home)
        self._set(VIEW_OPTION, home)
        self._fit()

    def _fit(self):
        """join-pane/split-window put home back as a split of the sidebar.
        Restore the last intact layout; failing that, the sidebar's width."""
        count = sum(1 for w in self.panes().values() if w == self.window)
        if self._layout and self._layout[0] == count:
            self.run("select-layout", "-t", self.window, self._layout[1])
            return
        width = self.option(WIDTH_OPTION)
        if width and self.sidebar:
            self.run("resize-pane", "-t", self.sidebar, "-x", width)


class Control:
    """Sends actions to the session's script over its control socket (the
    one lte-run reports to). A fresh connection per action: the script may
    have exited, or restarted, since the last one."""

    def __init__(self, path):
        self._path = path  # a path, or a callable returning one (discovery)

    @property
    def path(self):
        return self._path() if callable(self._path) else self._path

    def online(self):
        return self._send(None)

    def send(self, cmd, pane_id):
        return self._send({"cmd": cmd, "pane": pane_id})

    def _send(self, msg):
        if not self.path:
            return False
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(1.0)
            sock.connect(self.path)
            if msg is not None:
                sock.sendall((json.dumps(msg) + "\n").encode())
            return True
        except OSError:
            return False
        finally:
            sock.close()


def read_status(path):
    """The session's status file as a list of dicts (see PaneStatus), or
    None when it can't be read (not written yet, or mid-rewrite)."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lte-dash", description=__doc__.splitlines()[0])
    parser.add_argument("--status", default=None,
                        help="the session's status file (default: follow @lte_status)")
    parser.add_argument("--ctl", default=None,
                        help="the session's control socket (default with no --status: "
                        "follow @lte_dir)")
    parser.add_argument("--open", metavar="SESSION", default=None,
                        help="open a dashboard window in SESSION, print its window "
                        "and sidebar pane ids, and exit (see open_window())")
    parser.add_argument("--home-log", help="with --open: the file the home pane follows")
    parser.add_argument("--name", default="dash", help="with --open: window name")
    parser.add_argument("--width", type=int, default=36, help="with --open: sidebar columns")
    parser.add_argument("--no-mouse", action="store_true",
                        help="with --open: leave the session's mouse option alone")
    parser.add_argument("--key", default=KEY,
                        help=f"prefix key the sidebar borrows, back to it from any window "
                        f"of the session (default {KEY}; '' for none). See keys.py")
    args = parser.parse_args(argv)

    if args.open:
        if not args.home_log or not args.status:
            parser.error("--open needs --status and --home-log")
        try:
            window, sidebar = open_window(args.open, args.status, args.home_log, args.ctl,
                                          name=args.name, width=args.width,
                                          mouse=not args.no_mouse, key=args.key)
        except RuntimeError as exc:
            sys.stderr.write(f"{exc}\n")
            return 1
        print(window, sidebar)
        return 0

    pane = os.environ.get("TMUX_PANE")
    window = _tmux("display-message", "-p", "-t", pane, "#{window_id}") if pane else []
    if not window:
        sys.stderr.write("lte-dash: run it inside the session's dashboard window "
                         "(TmuxSession.dashboard(), or lte-dash --open)\n")
        return 2

    try:
        from .dash_app import DashApp
    except ImportError:
        sys.stderr.write(INSTALL_HINT)
        input("Press Enter to close. ")
        return 1

    viewport = Viewport(window[0].strip(), sidebar=pane)
    status, ctl = args.status, args.ctl
    if status is None:
        status = lambda: option(viewport.window, STATUS_OPTION)
        if ctl is None:
            ctl = lambda: os.path.join(option(viewport.window, DIR_OPTION) or "/nonexistent",
                                       SOCKET_NAME)
    app = DashApp(viewport, status, Control(ctl))
    key = args.key
    if key:
        try:
            keys.borrow(key, WINDOW_OPTION, KEY_COMMAND, "no lte-dash in this session")
        except ValueError as exc:
            sys.stderr.write(f"lte-dash: {exc}\n")
            key = ""

    def hung_up(signum, _frame):
        # The window or session was killed: leave through the finally below,
        # so the key is given back.
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGHUP, hung_up)
    signal.signal(signal.SIGTERM, hung_up)
    try:
        app.run()
        if app.return_code:
            input("lte-dash crashed. Press Enter to close. ")
        # However it ended, put every pane back and close the dashboard window --
        # unless its home pane is the user's command, which must keep running.
        viewport.restore()
        for name in (WINDOW_OPTION, SIDEBAR_OPTION):
            _tmux("set-option", "-uq", "-t", viewport.window, name)
        if viewport.option(HOME_KIND_OPTION) != "command":
            _tmux("kill-window", "-t", viewport.window)
    finally:
        if key:
            keys.give_back(key)
    return app.return_code or 0


if __name__ == "__main__":
    sys.exit(main())
