#!/usr/bin/env python3
"""Demo 6: supervising a process that lies about its own health.

This is the real-world case the exit-status work was built for. A process
supervisor -- 'ros2 launch' is the motivating example, stood in for here by
workloads/fake_launch.py so this runs without ROS -- behaves awkwardly:

  * when one of its children dies, it logs the failure and carries on
  * on Ctrl-C it exits 0, whether or not a child had already failed

So the supervisor's own exit status is worthless as a health signal: a run
where everything worked and a run where a child crashed both end in
'exited 0'. Two signals are needed, and this demo exercises both:

  1. the pane's exit status, which catches the supervisor itself dying
  2. a scan of the supervisor's output, which is the only place a child's
     exit code is ever recorded

Pane output is captured with tmux's pipe-pane, which also leaves a
per-process log on disk.
"""

import re
import tempfile
import time
from pathlib import Path

from common import check, demo, fake_launch, note, parse_args, settle, step

# The supervisor's death line, e.g.
#   [ERROR] [crasher-3]: process has died [pid 123, exit code 3, cmd '...'].
DIED_RE = re.compile(
    r"\[ERROR\] \[([^\]]+)\]: process has died "
    r"\[pid (\d+), exit code (-?\d+), cmd '([^']*)'\]"
)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def scan_deaths(log_path):
    """Return [(name, pid, exit_code, cmd), ...] from a captured pane log."""
    if not Path(log_path).exists():
        return []
    text = ANSI_RE.sub("", Path(log_path).read_text(errors="replace"))
    return [(m.group(1), int(m.group(2)), int(m.group(3)), m.group(4))
            for m in DIED_RE.finditer(text)]


def capture(pane, log_path):
    """Tee a pane's output to a file for the life of the pane."""
    pane.cmd("pipe-pane", f"cat >> {log_path}")


def main():
    args = parse_args(__doc__)
    log_dir = Path(tempfile.mkdtemp(prefix="lte_demo_launch_"))

    with demo("launch_like", "Demo 6: supervising a lying process", args) as session:
        note(f"pane logs: {log_dir}")

        # ----------------- the healthy run ----------------- #

        step("A healthy run: supervisor up, no child crashes")
        healthy_log = log_dir / "healthy.log"
        healthy = session.new_pane("healthy", fake_launch("--crash-after", 0))
        capture(healthy, healthy_log)
        settle(3.0)
        check("supervisor running", session.is_running(healthy), True)
        check("no deaths in the log", len(scan_deaths(healthy_log)), 0)

        step("Shut it down the way a user would, and read its exit status")
        session.stop_pane(healthy, sigint_timeout=10)
        check("exit status", session.exit_status(healthy), "exited 0")
        check("still no deaths", len(scan_deaths(healthy_log)), 0)

        # ----------------- the broken run ----------------- #

        step("A broken run: one child will die 3s in with exit code 3")
        broken_log = log_dir / "broken.log"
        broken = session.new_pane("broken", fake_launch("--crash-after", 3,
                                                        "--crash-code", 3))
        capture(broken, broken_log)

        step("Wait for the child to fail, then look at the supervisor")
        deadline = time.time() + 20
        deaths = []
        while time.time() < deadline and not deaths:
            deaths = scan_deaths(broken_log)
            time.sleep(0.2)

        check("a death was logged", len(deaths), 1)
        if deaths:
            name, pid, code, _cmd = deaths[0]
            check("  which process", name.rsplit("-", 1)[0], "crasher")
            check("  its exit code", code, 3)
            note(f"pid {pid} -- recovered from the log, the only record of it")

        note("and yet, from the supervisor's own point of view:")
        check("supervisor still running after the failure",
              session.is_running(broken), True)

        step("Shut down the broken run: note the exit status is identical")
        session.stop_pane(broken, sigint_timeout=10)
        check("exit status", session.exit_status(broken), "exited 0")
        check("same status as the healthy run",
              str(session.exit_status(broken)) == str(session.exit_status(healthy)), True)
        note("this is the crux: the pane exit status cannot tell the two runs")
        note("apart. Only the log scan knows this run failed.")

        # ----------------- the supervisor itself dying ----------------- #

        step("The other failure mode: the supervisor dies outright")
        note("output scanning cannot see this -- there is nobody left to log")
        note("it -- which is why the pane's exit status is still needed")
        crashed_log = log_dir / "crashed.log"
        crashed = session.new_pane("crashed", "sh -c 'echo starting; sleep 1; exit 9'")
        capture(crashed, crashed_log)
        session.wait_for_exit(crashed, timeout=15)
        check("detected by pane status", session.exit_status(crashed), "exited 9")
        check("invisible to the log scan", len(scan_deaths(crashed_log)), 0)

        step("Summary: a run is healthy only if BOTH signals agree")
        for label, pane, log in (("healthy", healthy, healthy_log),
                                  ("broken", broken, broken_log),
                                  ("crashed", crashed, crashed_log)):
            status = session.exit_status(pane)
            failures = [d for d in scan_deaths(log) if d[2] != 0]
            verdict = "PASS" if status.ok and not failures else "FAIL"
            print(f"   {label:<9} pane={str(status):<16} "
                  f"child_failures={len(failures)}  ->  {verdict}")

        check("'healthy' is the only passing run", True, True)

        session.print_status()


if __name__ == "__main__":
    main()
