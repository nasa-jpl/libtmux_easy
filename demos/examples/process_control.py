#!/usr/bin/env python3
"""Exit events and stopping: on_exit, stop_pane's escalation, respawn_pane, stop_all"""

from libtmux_easy import TmuxSession


def on_exit(status):  # runs on the watcher thread the moment a command ends
    print(f"  on_exit: {status.name}: {status}")


session = TmuxSession.create("lte_example_process_control", mode="wrap")
session.new_window("procs")
session.new_pane("short", "sleep 1; exit 4", on_exit=on_exit)
stubborn = session.new_pane("stubborn", "trap '' INT; sleep 60", on_exit=on_exit)
session.new_pane("worker", "sleep 60", on_exit=on_exit)

session.wait_for_exit("procs:short", timeout=5)

# SIGINT first, then SIGTERM, then SIGKILL: this one ignores SIGINT.
session.stop_pane(stubborn, sigint_timeout=1)
print("stubborn:", session.pane_status(stubborn))

# Rerun in place, with a new command.
session.respawn_pane("procs:short", "sleep 1; exit 0")
print("short, rerun:", session.wait_for_exit("procs:short", timeout=5))

# Stop whatever still runs, in parallel.
print("stop_all:", session.stop_all())
session.print_status()
session.kill_session()
