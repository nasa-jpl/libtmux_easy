#!/usr/bin/env python3
"""Build a tmux session with two windows of panes, then attach.

    python3 basic.py                          # prefix d detaches; the session keeps running
    tmux kill-session -t lte_example_basic    # when you're done

Run it outside tmux (inside, attaching would nest sessions).
"""

from libtmux_easy import TmuxSession

session = TmuxSession.create("lte_example_basic")

# A window, then its panes. Each new pane splits the window.
session.new_window("work")
session.new_pane("shell", "echo 'a plain shell'")
session.new_pane("top", "top")

# Another window, another layout. mode="wrap" tracks the exact exit status.
session.new_window("server", layout="even-vertical")
session.new_pane("http", "python3 -m http.server 8765", mode="wrap")
session.new_pane("client", "while sleep 2; do curl -s -o /dev/null -w '%{http_code}\\n' localhost:8765; done")

# Panes are named window:pane.
session.send_keys("echo 'typed into work:shell'", target_pane="work:shell")

session.select_first()
session.attach()
