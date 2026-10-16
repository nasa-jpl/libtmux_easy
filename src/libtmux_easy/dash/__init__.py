"""lte-dash: a status sidebar for a libtmux_easy session.

    lte-dash --status FILE [--ctl SOCKET]

Runs as the left pane of the session's dashboard window (see
TmuxSession.dashboard()). The right pane is a viewport: selecting a pane in
the sidebar swaps the real pane into it with 'tmux swap-pane', so it is fully
interactive. The "home" pane (a 'tail -F' of the script's output) is the
placeholder that fills the selected pane's slot in its own window while it is
on show, so every window keeps its layout.

Stdlib-only, like wrap.py, except app.py (Textual): only cli.main() imports
it, so the library can use Viewport without Textual.

  options.py   tmux option names holding the dashboard's state
  window.py    open_window(), launcher()
  viewport.py  Viewport: swap panes in and out
  control.py   Control (actions to the script), read_status()
  cli.py       lte-dash
  app.py       the sidebar UI (Textual)
  keys.py      lte-key: borrow a prefix key
  homelog.py   the tee behind TmuxSession.tee_home()

A program that is not a TmuxSession can open a dashboard too: 'lte-dash
--open SESSION ...', or open_window(). It then writes
the status file itself and answers the control socket's commands.

    lte-dash --open SESSION --status FILE --ctl SOCKET --home-log FILE
"""

from .control import Control, read_status
from .options import (AUTO, FROM_OPTION, HOME_CMD_OPTION, HOME_KIND_OPTION, HOME_LOG,
                      HOME_OPTION, HOME_STATUS_OPTION, INITIAL_OPTION, INSTALL_HINT, KEY,
                      KEY_COMMAND, MIN_WIDTH, RUN_DIR_OPTION, SIDEBAR_OPTION, STATUS_OPTION,
                      VIEW_OPTION, WIDTH_OPTION, WINDOW_OPTION)
from .viewport import Viewport
from .window import launcher, open_window

__all__ = [
    "Control", "Viewport", "launcher", "open_window", "read_status",
    "AUTO", "FROM_OPTION", "HOME_CMD_OPTION", "HOME_KIND_OPTION", "HOME_LOG", "HOME_OPTION",
    "HOME_STATUS_OPTION", "INITIAL_OPTION", "INSTALL_HINT", "KEY", "KEY_COMMAND", "MIN_WIDTH",
    "RUN_DIR_OPTION", "SIDEBAR_OPTION", "STATUS_OPTION", "VIEW_OPTION", "WIDTH_OPTION",
    "WINDOW_OPTION",
]
