#!/usr/bin/env python3
"""Demo 6: watching a session's panes from outside.

Starts a handful of panes with staggered lifetimes and then just reports on
them, which is what a monitoring view wants to do. Three levels of visibility,
cheapest first:

  1. Pane border titles. The session keeps each pane's title updated with its
     state, so attaching to the session IS the monitor -- the borders read
     '[running]' and flip to '[exit 0]' or '[signal 15]' on their own.

  2. session.status(), in-process: a PaneStatus per pane with its mode,
     command, target pids, liveness and exit status.

  3. A status file: JSON rewritten on every transition, for a genuinely
     separate process that knows nothing about tmux or libtmux.

This one runs until the panes have all finished, or until you Ctrl-C it.

    ./demo_monitor.py                      # print a table as things change
    ./demo_monitor.py --attach             # ...then attach at the end
    ./demo_monitor.py --status-file /tmp/panes.json
                                           # then, elsewhere:
                                           #   watch -n1 cat /tmp/panes.json
"""

import json
import time
from pathlib import Path

from common import (
    PANE_MODE_EXEC,
    check,
    demo,
    exits_after,
    graceful_sigint_exit,
    ignores_sigint,
    note,
    parse_args,
    runs_forever,
    step,
)


def print_table(statuses):
    print(f"\n   {'pane':<22} {'mode':<6} {'pids':<16} {'state':<10} status")
    print("   " + "-" * 68)
    for st in statuses:
        pids = ",".join(str(p) for p in st.target_pids) or "-"
        state = "running" if st.running else "finished"
        status = str(st.exit_status) if st.exit_status.known else ""
        print(f"   {st.name:<22} {st.mode:<6} {pids:<16} {state:<10} {status}")


def main():
    args = parse_args(__doc__)

    with demo("monitor", "Demo 6: monitoring pane state", args) as session:
        if args.status_file:
            note(f"status file: {args.status_file}")
        else:
            note("no --status-file given; levels 1 and 2 only")

        step("Start panes with deliberately staggered lifetimes")
        session.new_pane("fast_ok", exits_after(3, code=0))
        session.new_pane("fast_fail", exits_after(5, code=42))
        session.new_pane("slow_ok", exits_after(12, code=0), mode=PANE_MODE_EXEC)
        session.new_pane("long_lived", runs_forever(), mode=PANE_MODE_EXEC)
        session.new_pane("stubborn", ignores_sigint())
        session.new_pane("graceful", graceful_sigint_exit(7), mode=PANE_MODE_EXEC)

        note("attach in another terminal to watch the pane borders change:")
        note("  tmux attach -t lte_demo_monitor")

        step("Reporting until the short-lived panes have all finished")
        transient = ["demo:fast_ok", "demo:fast_fail", "demo:slow_ok"]
        deadline = time.time() + 40
        last = None
        while time.time() < deadline:
            statuses = session.status()
            fingerprint = [(s.name, s.running) for s in statuses]
            if fingerprint != last:
                print_table(statuses)
                last = fingerprint
            if all(not session.is_running(name) for name in transient):
                break
            time.sleep(0.5)

        step("The transient panes have reported their own outcomes")
        check("fast_ok", session.exit_status("demo:fast_ok"), "exited 0")
        check("fast_fail", session.exit_status("demo:fast_fail"), "exited 42")
        check("slow_ok", session.exit_status("demo:slow_ok"), "exited 0")
        check("long_lived still going", session.is_running("demo:long_lived"), True)

        if args.status_file and Path(args.status_file).exists():
            step("The status file is readable by anything that can parse JSON")
            data = json.loads(Path(args.status_file).read_text())
            check("entries", len(data), len(session.status()))
            names = [entry["name"] for entry in data]
            check("includes 'fast_fail'", "demo:fast_fail" in names, True)
            failed = [e["name"] for e in data
                      if not e["running"] and (e["exit_status"] or {}).get("code") not in (0, None)]
            note(f"a monitor could flag these as failed: {failed}")

        step("Shut the survivors down; each reports how it went")
        for name in ("demo:long_lived", "demo:stubborn", "demo:graceful"):
            clean = session.stop_pane(name, sigint_timeout=3, sigterm_timeout=3)
            status = session.exit_status(name)
            note(f"{name:<22} clean_stop={clean!s:<6} {status}")

        check("graceful exited with its own code", session.exit_status("demo:graceful"),
              "exited 7")
        check("stubborn needed escalating past SIGINT",
              session.exit_status("demo:stubborn").signal, 15)

        step("Final state")
        print_table(session.status())


if __name__ == "__main__":
    main()
