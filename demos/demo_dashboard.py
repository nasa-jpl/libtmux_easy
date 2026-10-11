#!/usr/bin/env python3
"""Demo 7: the dashboard (interactive).

Opens a session with a few wrap-mode panes in two windows -- some keep
running, some fail, one finishes cleanly -- then opens the dashboard and
attaches. Browse with j/k, Enter to type into a pane, g to go to it in its
own window (prefix g comes back), ? for help. The "load" pane under the
viewport shows dashboard_pane().

The script stays alive while you are attached, so the home entry shows its
output live and stop (x) / rerun (r) work. Detach (prefix d) to end the demo.

Needs Textual in this interpreter (a venv) or an installed lte-dash:
    python3 -m venv --system-site-packages .venv && .venv/bin/pip install textual
    .venv/bin/python demos/demo_dashboard.py
"""

import argparse
import logging
import os
import subprocess

from common import PANE_MODE_WRAP, TmuxSession, exits_after, runs_forever

SESSION = "lte_demo_dashboard"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--many", action="store_true",
                        help="also add 6 windows of 6 panes, more than the sidebar fits")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("demo")

    session = TmuxSession.create(SESSION)
    session.new_window("sim")
    for name, cmd in (("clock", "sh -c 'exec vmstat 2'"),
                      ("planner", runs_forever()),
                      ("crashy", exits_after(5, code=3))):
        session.new_pane(name, cmd, mode=PANE_MODE_WRAP,
                         on_exit=lambda s: log.info(f"{s.name} ended: {s.exit_status}"))
    session.new_window("tools")
    session.new_pane("shell", "echo 'an ordinary shell, try typing here'", mode=PANE_MODE_WRAP)
    session.new_pane("build", exits_after(3, code=0), mode=PANE_MODE_WRAP,
                     on_exit=lambda s: log.info(f"{s.name} ended: {s.exit_status}"))

    if args.many:
        for w in range(6):
            session.new_window(f"bulk{w}")
            for p in range(6):
                session.new_pane(f"p{p}", runs_forever(), mode=PANE_MODE_WRAP)

    session.dashboard()
    # Customizing: a persistent pane under the viewport.
    session.dashboard_pane("watch -t -n 5 uptime", title="load", size=4)
    log.info("dashboard open; this line is on the home entry")

    try:
        if os.environ.get("TMUX"):
            subprocess.run(["tmux", "switch-client", "-t", SESSION])
            input(f"Switched to {SESSION}. Press Enter here to end the demo. ")
        else:
            session.attach()
    finally:
        session.kill_session()


if __name__ == "__main__":
    main()
