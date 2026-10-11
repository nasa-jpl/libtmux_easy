#!/usr/bin/env python3
"""Demo 4: staying correct when somebody is using the pane.

Shell-mode panes are deliberately left interactive -- that is the whole point
of them. The cost is that the pane you are measuring is also a pane a person
can attach to and type into, so the library must not confuse their commands
with the one it launched.

This demo simulates that interference by sending keys to the pane exactly as
an attached user would, and checks that:

  - a user's command does not hide the target's death
  - a user's command is not mistaken for the target still running
  - the target's exit code is not corrupted by whatever they ran
  - a user destroying the pane entirely does not raise

The mechanism that makes this work is recording the pid the target command
actually spawned (PaneStatus.target_pids) and judging liveness on that,
rather than on "does this pane have any child processes".
"""

from common import (
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

    with demo("interference", "Demo 4: surviving pane interference", args) as session:

        step("Launch a long-running target and note the pid it spawned")
        target = session.new_pane("target", runs_forever())
        settle(1.0)
        pids = session.pane_status(target).target_pids
        check("target pid recorded", len(pids), 1)
        check("reported as running", session.is_running(target), True)
        note(f"watching pid {pids[0]} specifically, not 'any child of this pane'")

        step("A user attaches and starts something unrelated in the same pane")
        target.send_keys("sleep 400", enter=True)
        settle(1.0)
        check("target still correctly running", session.is_running(target), True)
        note("the user's 'sleep 400' is a child of this pane too, but it is not")
        note("the target, so it neither counts as the target running...")

        step("Now the target dies while the user's command keeps going")
        session.stop_pane(target, sigint_timeout=5)
        settle(0.5)
        check("death detected despite the user's command",
              session.is_running(target), False)
        note("...nor does it mask the target's death. A liveness test based on")
        note("'any child process' would still say True here.")

        step("A user's command cannot corrupt the recorded exit code")
        racy = session.new_pane("racy", exits_after(1, code=42))
        session.wait_for_exit(racy, timeout=10)
        for noise in ("ls /tmp > /dev/null", "true", "echo hello"):
            racy.send_keys(noise, enter=True)
        settle(1.0)
        check("exit code after three user commands",
              session.exit_status(racy), "exited 42")
        note("each of those overwrote the shell's '$?'; the status was already")
        note("captured at the moment the target exited")

        step("A user typing 'exit' destroys the pane outright")
        doomed = session.new_pane("doomed", runs_forever())
        settle(1.0)
        doomed.send_keys("C-c", enter=False)
        settle(0.5)
        doomed.send_keys("exit", enter=True)
        settle(1.0)
        note("the pane object now refers to something tmux no longer has")
        try:
            running = session.is_running(doomed)
            check("is_running on a destroyed pane", running, False)
        except Exception as exc:  # noqa: BLE001  -- the point is that it should not happen
            check("is_running on a destroyed pane",
                  f"raised {type(exc).__name__}", False)
        try:
            status = session.exit_status(doomed)
            check("exit_status on a destroyed pane does not raise", True, True)
            note(f"reported: {status}")
        except Exception as exc:  # noqa: BLE001
            check("exit_status on a destroyed pane does not raise",
                  f"raised {type(exc).__name__}", True)

        step("Pipelines spawn several processes; all of them are tracked")
        piped = session.new_pane("piped", "sleep 300 | cat")
        settle(1.0)
        piped_pids = session.pane_status(piped).target_pids
        check("more than one target pid", len(piped_pids) > 1, True)
        check("reported as running", session.is_running(piped), True)
        note(f"tracking {len(piped_pids)} pids: {list(piped_pids)}")
        session.stop_pane(piped, sigint_timeout=5)
        check("stopped", session.is_running(piped), False)

        session.print_status()


if __name__ == "__main__":
    main()
