#!/usr/bin/env python3
"""Demo 3: how a command ended, being told when it does, and stopping it.

  - the outcomes: clean exit, failing exit, signal death, still running,
    and a SIGINT handler that exits non-zero
  - on_exit callbacks and wait_for_exit(): pushed the moment a command
    ends, for any reason, including a Ctrl-C typed by someone attached
  - stop_pane()'s SIGINT -> SIGTERM -> SIGKILL escalation, and stop_all()

Wrap mode throughout: its status is exact, and never lost.
"""

import threading
import time

from common import (
    PANE_MODE_WRAP,
    check,
    demo,
    exits_after,
    graceful_sigint_exit,
    ignores_sigint,
    ignores_sigint_and_sigterm,
    note,
    parse_args,
    runs_forever,
    settle,
    step,
)


def main():
    args = parse_args(__doc__)

    with demo("exit", "Demo 3: exit status, exit events, stopping", args) as session:
        def pane(name, cmd, **kwargs):
            return session.new_pane(name, cmd, mode=PANE_MODE_WRAP, **kwargs)

        seen = {}
        seen_lock = threading.Lock()

        def record(status):
            # Runs on the watcher thread: keep it short, guard shared state.
            with seen_lock:
                seen[status.name] = str(status.exit_status)

        # ---------------- outcomes ---------------- #

        step("A clean and a failing exit, with a callback each")
        started = time.monotonic()
        clean = pane("clean", exits_after(1, code=0), on_exit=record)
        failing = pane("failing", exits_after(2, code=42))
        session.watch_pane(failing, record)  # or register afterwards
        status = session.wait_for_exit(failing, timeout=10)
        note(f"wait_for_exit returned {time.monotonic() - started:.1f}s in, for a 2s command")
        check("clean", session.exit_status(clean), "exited 0")
        check("  .ok", session.exit_status(clean).ok, True)
        check("failing", status, "exited 42")
        check("  .code / .signal", (status.code, status.signal), (42, None))
        settle(0.3)
        with seen_lock:
            check("callbacks fired", seen, {"demo:clean": "exited 0", "demo:failing": "exited 42"})

        step("Still running: the status is unknown, and a wait can time out")
        lingering = pane("lingering", runs_forever())
        check("wait_for_exit after 1s", session.wait_for_exit(lingering, timeout=1.0), "unknown")
        check("  .known", session.exit_status(lingering).known, False)
        note("test .known before trusting .code or .ok")

        step("A Ctrl-C typed by a person is an exit too")
        interrupted = pane("interrupted", runs_forever(), on_exit=record)
        settle(1.0)
        interrupted.send_keys("C-c", enter=False)  # what a human would press
        check("reported", session.wait_for_exit(interrupted, timeout=10), "killed by signal 2")
        check("  .ok", session.exit_status(interrupted).ok, False)

        step("A SIGINT handler that exits non-zero")
        note("e.g. a supervisor that reports a failure on shutdown: an exit code,")
        note("not a signal death")
        graceful = pane("graceful", graceful_sigint_exit(7))
        settle(1.5)
        session.stop_pane(graceful, sigint_timeout=5)
        check("status", session.exit_status(graceful), "exited 7")

        step("unwatch_pane() stops the callbacks, not the status")
        quiet = pane("quiet", exits_after(1, code=3), on_exit=record)
        session.unwatch_pane(quiet)
        session.wait_for_exit(quiet, timeout=10)
        settle(0.5)
        with seen_lock:
            check("no callback", "demo:quiet" in seen, False)
        check("status still readable", session.exit_status(quiet), "exited 3")

        # ---------------- stopping ---------------- #

        session.new_window("stop")

        step("stop_pane() escalates as far as it has to")
        note("past SIGINT, assume no graceful cleanup happened; .stopped says so")
        for name, cmd, expect, stopped in (
                ("polite", runs_forever(), "killed by signal 2", "SIGINT"),
                ("stubborn", ignores_sigint(), "killed by signal 15", "SIGTERM"),
                ("immortal", ignores_sigint_and_sigterm(), "killed by signal 9", "SIGKILL")):
            p = pane(name, cmd)
            settle(1.0)
            check(f"{name} stopped", session.stop_pane(p, sigint_timeout=1, sigterm_timeout=1), True)
            check("  status", session.exit_status(p), expect)
            check("  .stopped", session.pane_status(p).stopped, stopped)

        step("stop_all(): every running pane, in parallel")
        for i in range(3):
            pane(f"worker{i}", runs_forever())
        settle(1.0)
        start = time.monotonic()
        results = session.stop_all(sigint_timeout=1)
        note(f"took {time.monotonic() - start:.1f}s for {len(results)} panes")
        check("all stopped", all(results.values()), True)
        check("a finished pane is left alone", "demo:clean" in results, False)

        session.print_status()


if __name__ == "__main__":
    main()
