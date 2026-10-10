#!/usr/bin/env python3
"""Demo 2: shell mode vs exec mode.

The same command is run both ways, side by side, to show what each mode
gives up and what it buys:

  shell mode  the pane runs bash and the command is typed into it. The shell
              outlives the command, so the pane stays interactive -- you can
              rerun by hand, edit the command, poke around. The exit code is
              recovered by asking that shell for '$?'.

  exec mode   the command *is* the pane's process. tmux is its direct parent,
              so tmux records its exit status itself -- unambiguously, with
              signals distinguished from exit codes. There is no shell to
              type into; the pane goes dead (but stays visible) when the
              command ends.

Run with --attach to see the difference: the shell pane sits at a prompt, the
exec pane's border reads 'dead'.
"""

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

    with demo("pane_modes", "Demo 2: shell mode vs exec mode", args) as session:

        step("Run the same failing command in both modes")
        shell_pane = session.new_pane("shell", exits_after(1, code=42))
        exec_pane = session.new_pane("exec", exits_after(1, code=42), mode=PANE_MODE_EXEC)

        check("shell pane mode", session.pane_status(shell_pane).mode, "shell")
        check("exec pane mode", session.pane_status(exec_pane).mode, "exec")
        note("both panes record the pid their command actually started, which")
        note("is what liveness and exit events are judged on")
        check("shell pane has a target pid",
              len(session.pane_status(shell_pane).target_pids) > 0, True)
        check("exec pane has a target pid",
              len(session.pane_status(exec_pane).target_pids) > 0, True)

        step("Wait for both to finish; both report the same exit code")
        session.wait_for_exit(shell_pane, timeout=10)
        session.wait_for_exit(exec_pane, timeout=10)
        check("shell mode exit status", session.exit_status(shell_pane), "exited 42")
        check("exec mode exit status", session.exit_status(exec_pane), "exited 42")

        step("Where they differ: a signal death")
        note("shell mode sees only the shell's 128+N convention, so it cannot")
        note("tell 'killed by SIGINT' from a genuine 'exit 130' -- it says so")
        note("via ExitStatus.inferred. exec mode gets the signal from tmux.")
        shell_killed = session.new_pane("shell_sig", runs_forever())
        exec_killed = session.new_pane("exec_sig", runs_forever(), mode=PANE_MODE_EXEC)
        settle(1.0)
        session.stop_pane(shell_killed, sigint_timeout=5)
        session.stop_pane(exec_killed, sigint_timeout=5)

        shell_status = session.exit_status(shell_killed)
        exec_status = session.exit_status(exec_killed)
        check("shell mode, interrupted", shell_status, "killed by signal 2 (inferred)")
        check("  .inferred", shell_status.inferred, True)
        check("exec mode, interrupted", exec_status, "killed by signal 2")
        check("  .inferred", exec_status.inferred, False)

        step("The panes themselves end up in different states")
        note("the shell pane is back at a usable prompt; the exec pane is dead")
        note("but still shows its final output (thanks to remain-on-exit)")
        note("either way the pane is still addressable, and its status readable")
        check("shell pane still addressable by name",
              session.get_pane(session.get_pane_name(shell_pane)) is not None, True)
        check("exec pane still addressable by name",
              session.get_pane(session.get_pane_name(exec_killed)) is not None, True)
        check("neither reports a running command",
              session.is_running(shell_killed) or session.is_running(exec_killed), False)

        step("Rerunning: respawn_pane() works in either mode")
        note("it retypes the command in a shell pane and respawn-panes an exec")
        note("one, but either way it clears the cached exit status and re-arms")
        note("pid tracking, so the NEW run is what gets reported")
        session.respawn_pane(shell_pane, exits_after(1, code=5))
        session.respawn_pane(exec_killed, exits_after(1, code=5))
        session.wait_for_exit(shell_pane, timeout=10)
        session.wait_for_exit(exec_killed, timeout=10)
        check("shell pane, after rerun", session.exit_status(shell_pane), "exited 5")
        check("exec pane, after respawn", session.exit_status(exec_killed), "exited 5")
        note("plain send_keys() would restart the command too, but the library")
        note("would still be describing the previous run")

        session.print_status()


if __name__ == "__main__":
    main()
