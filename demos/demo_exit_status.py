#!/usr/bin/env python3
"""Demo 3: every way a command can end, and stopping ones that resist.

First half: the outcome matrix -- clean exit, failing exit, signal death,
still running -- and what ExitStatus reports for each.

Second half: stop_pane()'s SIGINT -> SIGTERM -> SIGKILL escalation against
processes that ignore the polite signals.
"""

from common import (
    PANE_MODE_EXEC,
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

    with demo("exit_status", "Demo 3: exit outcomes and stop escalation", args) as session:

        # ---------------- the outcome matrix ---------------- #

        step("A clean exit")
        clean = session.new_pane("clean", exits_after(1, code=0), mode=PANE_MODE_EXEC)
        session.wait_for_exit(clean, timeout=10)
        status = session.exit_status(clean)
        check("status", status, "exited 0")
        check("  .ok", status.ok, True)
        check("  .known", status.known, True)

        step("A failing exit")
        failing = session.new_pane("failing", exits_after(1, code=42), mode=PANE_MODE_EXEC)
        session.wait_for_exit(failing, timeout=10)
        status = session.exit_status(failing)
        check("status", status, "exited 42")
        check("  .code", status.code, 42)
        check("  .signal", status.signal, None)
        check("  .ok", status.ok, False)

        step("A signal death")
        killed = session.new_pane("killed", runs_forever(), mode=PANE_MODE_EXEC)
        settle(1.0)
        session.stop_pane(killed, sigint_timeout=5)
        status = session.exit_status(killed)
        check("status", status, "killed by signal 2")
        check("  .code", status.code, None)
        check("  .signal", status.signal, 2)
        check("  .ok", status.ok, False)
        note("a signal death is never .ok, and has no exit code at all")

        step("Still running: the status is simply unknown yet")
        running = session.new_pane("running", runs_forever(), mode=PANE_MODE_EXEC)
        settle(1.0)
        status = session.exit_status(running)
        check("is_running", session.is_running(running), True)
        check("status", status, "unknown")
        check("  .known", status.known, False)
        note("callers should test .known before trusting .code or .ok --")
        note("an unknown status has code None, so .ok is False either way")

        step("Caught SIGINT with a non-zero exit -- the 'ros2 launch' shape")
        note("this is why a non-zero exit after Ctrl-C has to stay")
        note("distinguishable from a signal death")
        graceful = session.new_pane("graceful", graceful_sigint_exit(7), mode=PANE_MODE_EXEC)
        settle(1.5)
        session.stop_pane(graceful, sigint_timeout=5)
        status = session.exit_status(graceful)
        check("status", status, "exited 7")
        check("  .signal", status.signal, None)

        # ---------------- stop escalation ---------------- #

        step("stop_pane() on a well-behaved process: SIGINT is enough")
        polite = session.new_pane("polite", runs_forever(), mode=PANE_MODE_EXEC)
        settle(1.0)
        check("stopped cleanly", session.stop_pane(polite, sigint_timeout=5), True)
        check("status", session.exit_status(polite), "killed by signal 2")

        step("A process that ignores SIGINT: escalate to SIGTERM")
        note("stop_pane logs a warning at each escalation; a caller that gets")
        note("past SIGINT should assume no graceful cleanup happened")
        stubborn = session.new_pane("stubborn", ignores_sigint(), mode=PANE_MODE_EXEC)
        settle(1.0)
        check("stopped", session.stop_pane(stubborn, sigint_timeout=2, sigterm_timeout=5), True)
        check("status", session.exit_status(stubborn), "killed by signal 15")

        step("A process that ignores SIGINT and SIGTERM: escalate to SIGKILL")
        immortal = session.new_pane("immortal", ignores_sigint_and_sigterm(),
                                    mode=PANE_MODE_EXEC)
        settle(1.0)
        check("stopped", session.stop_pane(immortal, sigint_timeout=2,
                                           sigterm_timeout=2, sigkill_timeout=5), True)
        check("status", session.exit_status(immortal), "killed by signal 9")

        step("Stopping an already-finished pane is a no-op that still reports")
        check("stop_pane on a dead pane", session.stop_pane(clean), True)
        check("status unchanged", session.exit_status(clean), "exited 0")

        session.print_status()


if __name__ == "__main__":
    main()
