#!/usr/bin/env python3
"""Demo 8: wrap mode -- shell mode's interactivity, exact exit status.

The same commands run in all three pane modes, side by side:

  shell mode  bash runs the command; '$?' is scraped back. 128+N means a
              genuine 'exit 130' and a SIGINT death look the same.

  exec mode   the command is the pane process; tmux records the status.
              Exact, but not interactive, and tmux intermittently loses it
              (README "Known issues: exec mode").

  wrap mode   bash runs 'lte-run <command>'. lte-run is the command's real
              parent, so it waitpid()s it and reports the exact status over
              a socket. The pane stays a normal shell, and reruns typed by
              hand are tracked automatically.

Exec-mode results are printed but not asserted on, since the tmux issue
would make this demo flaky for reasons it is not about.
"""

from common import (
    PANE_MODE_EXEC,
    PANE_MODE_SHELL,
    PANE_MODE_WRAP,
    check,
    demo,
    exits_after,
    note,
    parse_args,
    runs_forever,
    settle,
    step,
)
from libtmux_easy.tmux_lib import _wrap_prefix

MODES = (PANE_MODE_SHELL, PANE_MODE_EXEC, PANE_MODE_WRAP)


def main():
    args = parse_args(__doc__)

    with demo("wrap_mode", "Demo 8: wrap mode", args) as session:
        table = []

        step("A genuine 'exit 130' in all three modes")
        exited = {m: session.new_pane(f"exit130_{m}", exits_after(1, code=130), mode=m)
                  for m in MODES}
        for m, pane in exited.items():
            session.wait_for_exit(pane, timeout=10)
        statuses = {m: session.exit_status(p) for m, p in exited.items()}
        check("shell", statuses[PANE_MODE_SHELL], "killed by signal 2 (inferred)")
        check("exec", statuses[PANE_MODE_EXEC])
        check("wrap", statuses[PANE_MODE_WRAP], "exited 130")
        note("shell mode guesses wrong: 130 = 128+2 reads as SIGINT")
        table.append(("exit 130", statuses))

        step("A real SIGINT death in all three modes")
        killed = {m: session.new_pane(f"sigint_{m}", runs_forever(), mode=m) for m in MODES}
        settle(1.0)
        for pane in killed.values():
            session.stop_pane(pane, sigint_timeout=5)
        statuses = {m: session.exit_status(p) for m, p in killed.items()}
        check("shell", statuses[PANE_MODE_SHELL], "killed by signal 2 (inferred)")
        check("exec", statuses[PANE_MODE_EXEC])
        check("wrap", statuses[PANE_MODE_WRAP], "killed by signal 2")
        check("  wrap .inferred", statuses[PANE_MODE_WRAP].inferred, False)
        note("shell mode prints the same answer as above; wrap mode does not")
        table.append(("SIGINT", statuses))

        step("The wrap pane is still a shell: rerun by hand")
        pane = exited[PANE_MODE_WRAP]
        old_pids = session.pane_status(pane).target_pids
        note(f"typing: {_wrap_prefix()} {exits_after(1, code=7)}")
        session.send_keys(f"{_wrap_prefix()} {exits_after(1, code=7)}", target_pane=pane)
        settle(0.5)
        check("new run picked up (new target pid)",
              session.pane_status(pane).target_pids != old_pids, True)
        check("status reset while it runs", session.exit_status(pane), "unknown")
        session.wait_for_exit(pane, timeout=10)
        check("the rerun's own status", session.exit_status(pane), "exited 7")

        step("Typing more commands does not clobber the result")
        session.send_keys("false", target_pane=pane)
        settle(0.5)
        check("still the rerun's status", session.exit_status(pane), "exited 7")
        note("in shell mode, '$?' is one-shot; wrap mode never reads it")

        step("Summary")
        width = 10
        print(f"   {'':<{width}}" + "".join(f"{m:<32}" for m in MODES))
        for label, row in table:
            print(f"   {label:<{width}}" + "".join(f"{str(row[m]):<32}" for m in MODES))

        print()
        session.print_status()


if __name__ == "__main__":
    main()
