"""Names shared by the dashboard's parts: tmux options holding its state,
the borrowed key, the install hint."""

# Window options on the dashboard window. They hold the viewport's state in
# tmux itself, so a restarted sidebar and the library agree on it.
HOME_OPTION = "@lte_dash_home"   # pane id of the home/placeholder pane
VIEW_OPTION = "@lte_dash_view"   # pane id currently in the viewport
FROM_OPTION = "@lte_dash_from"   # window the viewed pane was swapped out of
HOME_CMD_OPTION = "@lte_dash_home_cmd"
WIDTH_OPTION = "@lte_dash_width"  # the sidebar's width: columns, "N%", or AUTO until sized

# Sidebar width "auto": the app fits it to its widest row once, at start.
# Until then (and never narrower) MIN_WIDTH, which fits the summary line.
AUTO = "auto"
MIN_WIDTH = 36
INITIAL_OPTION = "@lte_dash_initial"  # pane id to show first, instead of home

# "command" when the home pane runs the user's command (lte-session): then
# quitting the sidebar must leave the window, and with it the command, alone.
HOME_KIND_OPTION = "@lte_dash_home_kind"

# Session options, for key bindings outside the dashboard (docs/dashboard.md).
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
# Elsewhere the key keeps its usual binding. "" borrows nothing. Only if
# unbound; ~/.tmux.conf's @lte_key_dash picks another (keys.chosen()).
KEY = "g"
KEY_ROLE = "dash"
KEY_COMMAND = (f"run-shell \"tmux -S '#{{socket_path}}' select-window -t '#{{{WINDOW_OPTION}}}'"
               f" && tmux -S '#{{socket_path}}' select-pane -t '#{{{SIDEBAR_OPTION}}}'\"")

INSTALL_HINT = (
    "lte-dash needs Textual (>= 1.0). Install one of:\n"
    "  sudo apt install ./libtmux-easy-dash_*_all.deb   # Ubuntu, from GitHub Releases\n"
    "  pipx install 'libtmux-easy[dash]'                # puts lte-dash on PATH\n"
    "  pip install 'libtmux-easy[dash]'                 # into the script's venv\n"
)
