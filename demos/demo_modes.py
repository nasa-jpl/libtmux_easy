#!/usr/bin/env python3
"""Demo 2: the three pane modes, side by side.

  shell  bash runs the command; '$?' is read back. Interactive, but 128+N
         means 'exit 130' and a SIGINT death look the same.
  exec   the command is the pane process; tmux records its status. Exact,
         not interactive, and tmux intermittently loses the status
         (README "Known issues: exec mode").
  wrap   bash runs 'lte-run <command>', which reports the exact status over
         a socket. Interactive, exact, and reruns typed by hand are tracked.

Exec-mode results are printed but not checked: the tmux issue would make
this demo flaky for reasons it is not about.
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

    with demo("modes", "Demo 2: shell, exec and wrap mode", args) as session:
        table = []

        step("The same failing command in all three modes")
        failed = {m: session.new_pane(f"exit42_{m}", exits_after(1, code=42), mode=m)
                  for m in MODES}
        for pane in failed.values():
            check(f"{session.pane_status(pane).mode} has a target pid",
                  len(session.pane_status(pane).target_pids) > 0, True)
        for pane in failed.values():
            session.wait_for_exit(pane, timeout=10)
        statuses = {m: session.exit_status(p) for m, p in failed.items()}
        check("shell", statuses[PANE_MODE_SHELL], "exited 42")
        check("exec", statuses[PANE_MODE_EXEC])
        check("wrap", statuses[PANE_MODE_WRAP], "exited 42")
        table.append(("exit 42", statuses))

        step("A genuine 'exit 130'")
        exited = {m: session.new_pane(f"exit130_{m}", exits_after(1, code=130), mode=m)
                  for m in MODES}
        for pane in exited.values():
            session.wait_for_exit(pane, timeout=10)
        statuses = {m: session.exit_status(p) for m, p in exited.items()}
        check("shell", statuses[PANE_MODE_SHELL], "killed by signal 2 (inferred)")
        check("exec", statuses[PANE_MODE_EXEC])
        check("wrap", statuses[PANE_MODE_WRAP], "exited 130")
        note("shell mode guesses wrong: 130 = 128+2 reads as SIGINT")
        table.append(("exit 130", statuses))

        step("A real SIGINT death")
        killed = {m: session.new_pane(f"sigint_{m}", runs_forever(), mode=m) for m in MODES}
        settle(1.0)
        for pane in killed.values():
            session.stop_pane(pane, sigint_timeout=5)
        statuses = {m: session.exit_status(p) for m, p in killed.items()}
        check("shell", statuses[PANE_MODE_SHELL], "killed by signal 2 (inferred)")
        check("exec", statuses[PANE_MODE_EXEC])
        check("wrap", statuses[PANE_MODE_WRAP], "killed by signal 2")
        note("shell mode says the same as above; wrap mode does not")
        table.append(("SIGINT", statuses))

        step("respawn_pane() reruns in any mode, and the new run is reported")
        note("plain send_keys() would restart the command too, but the library")
        note("would still describe the previous run")
        for pane in failed.values():
            session.respawn_pane(pane, exits_after(1, code=5))
        for pane in failed.values():
            session.wait_for_exit(pane, timeout=10)
        check("shell, after rerun", session.exit_status(failed[PANE_MODE_SHELL]), "exited 5")
        check("wrap, after rerun", session.exit_status(failed[PANE_MODE_WRAP]), "exited 5")

        step("A wrap pane is still a shell: a rerun typed by hand is tracked")
        pane = exited[PANE_MODE_WRAP]
        old_pids = session.pane_status(pane).target_pids
        note(f"typing: {_wrap_prefix()} {exits_after(1, code=7)}")
        session.send_keys(f"{_wrap_prefix()} {exits_after(1, code=7)}", target_pane=pane)
        settle(0.5)
        check("new run picked up (new target pid)",
              session.pane_status(pane).target_pids != old_pids, True)
        session.wait_for_exit(pane, timeout=10)
        check("the rerun's own status", session.exit_status(pane), "exited 7")
        session.send_keys("false", target_pane=pane)
        settle(0.5)
        check("more typing doesn't clobber it", session.exit_status(pane), "exited 7")

        step("Summary")
        print(f"   {'':<10}" + "".join(f"{m:<32}" for m in MODES))
        for label, row in table:
            print(f"   {label:<10}" + "".join(f"{str(row[m]):<32}" for m in MODES))
        print()
        session.print_status()


if __name__ == "__main__":
    main()
