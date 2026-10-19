#!/usr/bin/env python3
"""Tour 5: monitoring -- status from outside, and a supervisor that hides failures.

Pane titles, status() / print_status(), and the status file: JSON rewritten
on every change, for any other process. Then a 'ros2 launch'-like supervisor
whose exit status says nothing about its children.
"""

import re
import time

from common import HERE, PANE_MODE_WRAP, Tour, exits_after, fake_launch, runs_forever

DIED = re.compile(r"\[ERROR\] \[([^\]]+)\]: process has died \[pid \d+, exit code (-?\d+)")


def output(pane):
    """The pane's text, scrollback included."""
    return "\n".join(pane.capture_pane(start="-"))


def main():
    with Tour("monitoring", __doc__) as tour:
        s = tour.session

        def pane(name, cmd):
            return s.new_pane(name, cmd, mode=PANE_MODE_WRAP, on_exit=tour.event)

        tour.step("The status file")
        tour.say(f"""
            This session's status file:

                {s.status_file}

            The monitor pane runs workloads/status_monitor.py on it: a separate
            program that only reads JSON. The sidebar reads the same file.
        """)
        tour.pause("start the monitor and three workers with different lifetimes")
        s.new_window("monitor")
        pane("monitor", f"python3 -u {HERE / 'workloads' / 'status_monitor.py'} {s.status_file}")
        s.new_window("workers")
        pane("quick", exits_after(5, code=0))
        pane("failing", exits_after(10, code=42))
        pane("steady", runs_forever())
        tour.say("""
            Select monitor:monitor and watch the states change; quick finishes at
            5 s, failing at 10 s. Each pane's border title says the same.
        """)
        tour.pause("print_status() here")
        tour.status()

        tour.step("A supervisor that hides failures")
        tour.say("""
            'ros2 launch' (stood in for by workloads/fake_launch.py) supervises
            child processes. When a child dies, it logs an error and keeps
            running; on Ctrl-C it exits 0 anyway. Its exit status can't tell a
            good run from a bad one: only its output can.
        """)
        tour.pause("start it; one child crashes 5 s in with exit code 3")
        s.new_window("launch")
        launch = pane("supervisor", fake_launch("--crash-after", 5, "--crash-code", 3))
        tour.say("Watching its output for 'process has died' ...")
        deadline = time.time() + 30
        while time.time() < deadline and not DIED.search(output(launch)):
            time.sleep(0.5)
        for name, code in DIED.findall(output(launch)):
            print(f"  found in the output: {name} exited {code}")
        print(f"  the supervisor itself: {s.pane_status(launch)}")
        tour.pause("stop the supervisor")
        s.stop_pane(launch, sigint_timeout=10)
        tour.say(f"""
            {s.exit_status(launch)}, like a healthy run. So watch both: the exit
            status (the supervisor dying) and the output (its children dying).
            The pytest plugin does both: see lte_output_checks in docs/pytest-plugin.md.
        """)


if __name__ == "__main__":
    main()
