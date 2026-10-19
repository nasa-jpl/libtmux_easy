#!/usr/bin/env python3
"""Monitor panes from another program: status_file= is JSON, rewritten on every change"""

import json
import os
import tempfile
import time

from libtmux_easy import TmuxSession

status_file = os.path.join(tempfile.mkdtemp(), "status.json")
session = TmuxSession.create("lte_example_monitoring", mode="wrap", status_file=status_file)
session.new_window("jobs")
session.new_pane("fast", "sleep 1; exit 0")
session.new_pane("slow", "sleep 3; exit 2")
session.new_pane("server", "sleep 60")


def state(entry):
    return "running" if entry["running"] else f"exit {entry['exit_status']['code']}"


# What any other program sees: plain JSON, no libtmux needed.
for _ in range(5):
    with open(status_file) as f:
        print("  ".join(f"{e['name']}: {state(e)}" for e in json.load(f)))
    time.sleep(1)

session.stop_all()
session.kill_session()
