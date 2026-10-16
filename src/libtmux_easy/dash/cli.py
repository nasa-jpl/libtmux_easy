"""lte-dash: the sidebar's entry point, or --open to create the window."""

import argparse
import os
import signal
import sys

from . import keys
from .._tmux import option, tmux as _tmux
from ..wrap import DIR_OPTION, SOCKET_NAME
from .control import Control
from .options import AUTO, HOME_KIND_OPTION, INSTALL_HINT, KEY, KEY_COMMAND, SIDEBAR_OPTION, \
    STATUS_OPTION, WINDOW_OPTION
from .viewport import Viewport
from .window import open_window


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lte-dash", description="a status sidebar for a libtmux_easy session")
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
    parser.add_argument("--width", default=AUTO,
                        help="with --open: sidebar columns, N%%, or auto (fit the panes, at start)")
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
        from .app import DashApp
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
