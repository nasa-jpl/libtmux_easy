#!/usr/bin/env python3
"""A session script in parts: build() makes the system, monitor() watches
it at runtime, main() puts them together with a dashboard. The test calls
build() and monitor() itself (test_build.py).

    python3 launch_build.py [--port N]       # needs Textual for the dashboard
"""
import argparse
import sys

from libtmux_easy import TmuxSession

PY = sys.executable
TICKER = (f"{PY} -u -c 'import time\n"
          "try:\n    [print(\"tick\", i) or time.sleep(0.5) for i in range(10**6)]\n"
          "except KeyboardInterrupt: pass'")


def build(session, port=8000):
    """The system. 'session' is any TmuxSession: main()'s, or a test's."""
    session.set_default_mode("wrap")
    session.new_window("servers")
    session.new_pane("web", f"{PY} -u -m http.server {port} --bind 127.0.0.1")
    session.wait_for_output("servers:web", r"Serving HTTP", timeout=10)  # up before the rest
    session.new_window("workers")
    session.new_pane("ticker", TICKER)
    session.new_window("jobs")
    session.new_pane("migrate", "echo migrating; sleep 1; echo migrated")


def monitor(session, alert):
    """Runtime watches: alert() on a crash, or a request for a missing page."""
    session.watch_pane("workers:ticker", on_exit=lambda status: alert(f"ticker {status}"))
    session.watch_output("servers:web", r'" 404 ', lambda match: alert(f"web: {match.line}"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    # capture=True: piped from the first byte, so build()'s wait_for_output()
    # can't miss its line (docs/KNOWN_ISSUES.md). A test's session pipes them.
    session = TmuxSession.create("demo_build", capture=True)
    build(session, args.port)
    monitor(session, alert=lambda what: print("ALERT", what, flush=True))
    session.dashboard()
    session.attach()


if __name__ == "__main__":
    main()
