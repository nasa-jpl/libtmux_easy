#!/usr/bin/env python3
"""Tour 2: how a command ended -- the three pane modes.

  shell  typed into bash; the status is read back from $?
  exec   the command is the pane process; tmux records its status
  wrap   typed as 'lte-run CMD', which reports the exact status
"""

from common import MODES, PANE_MODE_SHELL, PANE_MODE_WRAP, Tour, exits_after, lte_run, runs_forever


def main():
    with Tour("exit_status", __doc__) as tour:
        s = tour.session

        tour.step("The same failing command in each mode")
        tour.say("""
            Three panes, one per mode, each counting down and then exiting 42.
            Watch them in the sidebar: green while running, red once they fail.
        """)
        tour.pause("start them")
        s.new_window("exit42")
        panes = {m: s.new_pane(m, exits_after(5, code=42), mode=m) for m in MODES}
        for pane in panes.values():
            tour.wait(pane, timeout=30)
        tour.say("""
            All three agree here. Exec mode is the odd one out in practice: tmux
            sometimes loses its status (docs/KNOWN_ISSUES.md), so it may say unknown.
        """)

        tour.step("A real Ctrl-C, and a command that exits 130")
        tour.say("""
            A shell can only report 130 for both: 128 + SIGINT(2). Shell mode marks
            its guess "(inferred)". Wrap mode knows which one happened.
        """)
        tour.pause("start a long-running pane and an 'exit 130' pane in shell and wrap mode")
        s.new_window("sigint")
        forever = {m: s.new_pane(f"ctrl-c_{m}", runs_forever(), mode=m)
                   for m in (PANE_MODE_SHELL, PANE_MODE_WRAP)}
        exit130 = {m: s.new_pane(f"exit130_{m}", exits_after(3, code=130), mode=m)
                   for m in (PANE_MODE_SHELL, PANE_MODE_WRAP)}
        for pane in exit130.values():
            tour.wait(pane, timeout=30)
        tour.pause("send Ctrl-C to the long-running ones (stop_pane)")
        for pane in forever.values():
            s.stop_pane(pane, sigint_timeout=5)
            print(f"  {s.get_pane_name(pane)}: {s.exit_status(pane)}")
        tour.status()

        tour.step("Rerunning")
        tour.say("""
            respawn_pane() reruns a pane in place, in any mode, and the new run is
            what gets reported.
        """)
        tour.pause("rerun the exit42 panes, exiting 0 this time")
        for pane in panes.values():
            s.respawn_pane(pane, exits_after(3, code=0))
        for pane in panes.values():
            tour.wait(pane, timeout=30)

        tour.step("Your turn: a wrap pane is still a shell")
        cmd = f"{lte_run()} sh -c 'sleep 3; exit 5'"
        tour.say(f"""
            Select exit42:wrap in the sidebar, Enter to focus it, and type:

                {cmd}

            The sidebar follows the new run: running, then exited 5. Anything you run
            with lte-run in this session's panes is tracked, Ctrl-C included.
            Plain commands (no lte-run) don't change the reported status.
        """)
        if tour.auto:
            s.send_keys(cmd, target_pane=panes[PANE_MODE_WRAP])
        tour.pause("continue once you've tried it")
        tour.status()


if __name__ == "__main__":
    main()
