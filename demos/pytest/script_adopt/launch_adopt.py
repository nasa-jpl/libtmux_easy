#!/usr/bin/env python3
"""A session script as many are: top to bottom, then attach. The test runs
it as it is and adopts what it made (test_adopt.py).

    python3 launch_adopt.py [--port N]
"""
import argparse
import sys

from libtmux_easy import TmuxSession

PY = sys.executable
parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8000)
args = parser.parse_args()

# capture=True: each pane's output to a file from its first byte. Also keeps
# wait_for_output() below from missing a line (docs/KNOWN_ISSUES.md).
session = TmuxSession.create("demo_adopt", mode="wrap", capture=True)
session.new_window("servers")
session.new_pane("web", f"{PY} -u -m http.server {args.port} --bind 127.0.0.1")
session.wait_for_output("servers:web", r"Serving HTTP", timeout=10)
session.new_window("workers")
session.new_pane("ticker", f"{PY} -u -c 'import time\n"
                           "try:\n    [print(\"tick\", i) or time.sleep(0.5) for i in range(10**6)]\n"
                           "except KeyboardInterrupt: pass'")
session.new_window("jobs")
session.new_pane("migrate", "echo migrating; sleep 1; echo migrated")
session.attach()  # a test: nothing (LTE_HEADLESS)
