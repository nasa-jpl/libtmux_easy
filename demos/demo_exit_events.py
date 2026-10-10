#!/usr/bin/env python3
"""Demo 4: being told when a process exits, instead of asking.

on_exit callbacks fire the moment a pane's target command ends -- for any
reason: a clean exit, a crash, a signal, or someone attaching to the session
and pressing Ctrl-C. The mechanism underneath is os.pidfd_open(), so there is
no polling loop and nothing to tune.

The callback is also what makes the exit code trustworthy. In shell mode the
code lives in the shell's '$?', which the *next* command to run in that pane
overwrites. Reading it lazily is therefore a race against anything else
touching the pane; capturing it at the moment of exit is not. The last check
below demonstrates exactly that.
"""

import threading
import time

from common import (
    PANE_MODE_EXEC,
    check,
    demo,
    exits_after,
    note,
    parse_args,
    runs_forever,
    settle,
    step,
)


def main():
    args = parse_args(__doc__)

    with demo("exit_events", "Demo 4: exit events", args) as session:
        seen = []
        seen_lock = threading.Lock()

        def record(status):
            # Callbacks run on the watcher thread, so keep them short and
            # guard anything shared.
            with seen_lock:
                seen.append((status.name, str(status.exit_status), time.monotonic()))

        step("Register a callback at pane creation")
        started = time.monotonic()
        session.new_pane("quick", exits_after(2, code=42), on_exit=record)

        step("Or afterwards, on an existing pane")
        slow = session.new_pane("slow", exits_after(4, code=0))
        session.watch_pane(slow, record)

        step("Waiting for the first event (the command sleeps 2s)")
        with seen_lock:
            check("no event yet", len(seen), 0)
        status = session.wait_for_exit("demo:quick", timeout=15)
        elapsed = time.monotonic() - started
        check("wait_for_exit returned", status, "exited 42")
        note(f"fired {elapsed:.1f}s after launch, for a 2s command -- no polling involved")

        settle(0.4)  # let the callback run
        with seen_lock:
            names = [n for n, _, _ in seen]
        check("callback fired for 'quick'", "demo:quick" in names, True)

        step("The second pane's event arrives on its own schedule")
        check("'slow' still running", session.is_running(slow), True)
        session.wait_for_exit(slow, timeout=15)
        settle(0.4)
        with seen_lock:
            events = dict((n, s) for n, s, _ in seen)
        check("events received", len(events), 2)
        check("'quick' status", events.get("demo:quick"), "exited 42")
        check("'slow' status", events.get("demo:slow"), "exited 0")

        step("A Ctrl-C typed by a person is an exit too, and must be reported")
        note("this is what a user attaching to the session and interrupting")
        note("a process looks like -- the event has to fire regardless of cause")
        interrupted = session.new_pane("interrupted", runs_forever(), on_exit=record)
        settle(1.0)
        interrupted.send_keys("C-c", enter=False)  # exactly what a human would press
        status = session.wait_for_exit(interrupted, timeout=10)
        check("reported", status, "killed by signal 2 (inferred)")

        step("wait_for_exit() on something that outlives the timeout")
        lingering = session.new_pane("lingering", runs_forever())
        settle(1.0)
        status = session.wait_for_exit(lingering, timeout=1.0)
        check("status after timeout", status, "unknown")
        check("  .known", status.known, False)
        note("a timeout is not an error -- the process is simply still going")

        step("The exit code survives someone else using the pane afterwards")
        note("'$?' is overwritten by the next command to run in a shell pane.")
        note("Because the status was captured when the process exited, a later")
        note("command cannot corrupt it -- read lazily, this would say 'exited 0'.")
        racy = session.new_pane("racy", exits_after(1, code=42), on_exit=record)
        session.wait_for_exit(racy, timeout=10)
        racy.send_keys("ls /tmp > /dev/null", enter=True)   # a user poking around
        settle(1.0)
        check("status after an unrelated command ran",
              session.exit_status(racy), "exited 42")

        step("unwatch_pane() stops the callbacks")
        quiet = session.new_pane("quiet", exits_after(1, code=3), on_exit=record)
        session.unwatch_pane(quiet)
        session.wait_for_exit(quiet, timeout=10)
        settle(1.0)
        with seen_lock:
            names = [n for n, _, _ in seen]
        check("no callback for the unwatched pane", "demo:quiet" in names, False)
        note("its status is still readable on demand -- only the push stopped")
        check("status still available", session.exit_status(quiet), "exited 3")

        session.print_status()


if __name__ == "__main__":
    main()
