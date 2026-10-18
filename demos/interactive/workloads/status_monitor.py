#!/usr/bin/env python3
"""A monitor that knows nothing about tmux: reads a libtmux_easy status file
(JSON, rewritten on every change) and prints a table every second.

    status_monitor.py STATUS_FILE
"""

import json
import sys
import time


def state(entry):
    if entry["running"]:
        return "running"
    if entry.get("stopped"):
        return f"stopped ({entry['stopped']})"
    st = entry.get("exit_status") or {}
    if st.get("signal") is not None:
        return f"failed (signal {st['signal']})"
    if st.get("code") is None:
        return "unknown"
    return "ok" if st["code"] == 0 else f"failed (exit {st['code']})"


def main(path):
    while True:
        try:
            with open(path) as f:
                entries = json.load(f)
        except (OSError, ValueError):
            entries = None
        sys.stdout.write("\x1b[H\x1b[2J")  # home, clear
        print(f"{path}  {time.strftime('%H:%M:%S')}\n")
        for e in entries or []:
            print(f"  {e['name']:<24} {state(e)}")
        sys.stdout.flush()
        time.sleep(1)


if __name__ == "__main__":
    main(sys.argv[1])
