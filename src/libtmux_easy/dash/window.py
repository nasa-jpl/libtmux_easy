"""Opening the dashboard window."""

import importlib.util
import shlex
import shutil
import sys
from pathlib import Path

from .._tmux import tmux as _tmux
from .options import (AUTO, HOME_CMD_OPTION, HOME_KIND_OPTION, HOME_OPTION, INITIAL_OPTION,
                      INSTALL_HINT, MIN_WIDTH, SIDEBAR_OPTION, VIEW_OPTION, WIDTH_OPTION,
                      WINDOW_OPTION)


def launcher():
    """How the dashboard window runs lte-dash: under this interpreter when it
    has Textual (e.g. a venv), else an installed lte-dash (e.g. pipx), which
    brings its own. Raises RuntimeError when neither exists."""
    if importlib.util.find_spec("textual") is not None:
        src = Path(__file__).resolve().parents[2]
        return (f"env PYTHONPATH={shlex.quote(str(src))} "
                f"{shlex.quote(sys.executable)} -m libtmux_easy.dash")
    if shutil.which("lte-dash"):
        return "lte-dash"
    raise RuntimeError(INSTALL_HINT)


def open_window(session, status_file, home_log, ctl=None, *, name="dash", width=AUTO,
                mouse=True, run=_tmux, sidebar_cmd=None, home_cmd=None, key=None,
                initial=None):
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
    prefix key the sidebar borrows while it runs ("" for none; None: lte-dash
    --key's default, KEY only if unbound). 'initial'
    is the pane id the sidebar selects (and shows) first, instead of home.

    'width': columns, a percentage like "25%", or AUTO: the sidebar sizes
    itself once, at start, to fit its widest row (the panes it lists then)."""
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
                       (HOME_KIND_OPTION, home_kind), (INITIAL_OPTION, initial or "")):
        run("set-option", "-wq", "-t", window_id, opt, value)

    if status_file:
        sidebar_cmd = f"{sidebar_cmd} --status {shlex.quote(status_file)}"
    if ctl:
        sidebar_cmd += f" --ctl {shlex.quote(ctl)}"
    if key is not None:
        sidebar_cmd += f" --key {shlex.quote(key)}"
    start = MIN_WIDTH if width == AUTO else width
    sidebar = run("split-window", "-h", "-b", "-d", "-l", str(start), "-t", home,
                  "-P", "-F", "#{pane_id}", sidebar_cmd)[0].strip()
    run("select-pane", "-t", home, "-T", "home")
    run("select-pane", "-t", sidebar, "-T", "lte-dash")
    run("set-option", "-q", "-t", session, WINDOW_OPTION, window_id)
    run("set-option", "-q", "-t", session, SIDEBAR_OPTION, sidebar)
    if mouse:
        run("set-option", "-q", "-t", session, "mouse", "on")
    return window_id, sidebar
