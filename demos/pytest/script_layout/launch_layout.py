#!/usr/bin/env python3
"""A session script whose system is data: panes() lists what to run, main()
lays it out. The test starts the same list itself (test_layout.py).

    python3 launch_layout.py [--port N]       # then: tmux attach -t demo_layout
"""
import argparse
import itertools
import sys

from libtmux_easy import TmuxSession

PY = sys.executable


def panes(port):
    """The system: ('window:pane', command), in start order."""
    return [
        ("servers:web", f"{PY} -u -m http.server {port} --bind 127.0.0.1"),
        ("workers:ticker", f"{PY} -u -c 'import time\n"
                           "try:\n    [print(\"tick\", i) or time.sleep(0.5) for i in range(10**6)]\n"
                           "except KeyboardInterrupt: pass'"),
        ("jobs:migrate", "echo migrating; sleep 1; echo migrated"),
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    session = TmuxSession.create("demo_layout", mode="wrap")
    for window, group in itertools.groupby(panes(args.port), lambda p: p[0].split(":")[0]):
        session.new_window(window)
        for name, cmd in group:
            session.new_pane(name.split(":")[1], cmd)
    print("tmux attach -t demo_layout")


if __name__ == "__main__":
    main()
