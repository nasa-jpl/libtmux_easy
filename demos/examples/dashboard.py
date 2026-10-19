#!/usr/bin/env python3
"""A dashboard: every pane in a sidebar, the selected one beside it (needs Textual)

In the sidebar: Up/Down browse, Enter focuses the pane, x stops it, r reruns
it, ? shows the keys. prefix d detaches; prefix Q closes the session.
"""

from libtmux_easy import TmuxSession

session = TmuxSession.create("lte_example_dashboard", mode="wrap")

session.new_window("servers")
session.new_pane("web", "python3 -m http.server 8766")
session.new_pane("clock", "while true; do date; sleep 1; done")

session.new_window("jobs")
session.new_pane("passes", "sleep 3; exit 0")
session.new_pane("fails", "sleep 5; exit 1")

session.dashboard()
print("The script's output shows in the dashboard's home entry.")
session.attach()  # the sidebar's actions work while this script runs
