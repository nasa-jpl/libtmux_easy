"""Shared scaffolding for the libtmux_easy tours.

A tour relaunches itself under lte-session: the script runs in the
dashboard's home pane, where you read what it says and press Enter to go on,
and the panes it starts appear in the sidebar. Browse them with j/k; select
"home" to come back. --auto runs a tour unattended (run_all.py).

Works from a bare checkout: falls back to ../src.
"""

import argparse
import logging
import os
import subprocess
import sys
import tempfile
import termios
import textwrap
import time
import traceback
from pathlib import Path

try:
    import libtmux_easy
except ImportError:  # not installed: run from the checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import libtmux_easy

from libtmux_easy import PANE_MODE_EXEC, PANE_MODE_SHELL, PANE_MODE_WRAP, TmuxSession
from libtmux_easy.tmux_lib import _wrap_prefix

HERE = Path(__file__).resolve().parent
MODES = (PANE_MODE_SHELL, PANE_MODE_EXEC, PANE_MODE_WRAP)
_MARKER = "LTE_DEMO_TOUR"  # set once relaunched under lte-session

BOLD, DIM, RESET = "\x1b[1m", "\x1b[2m", "\x1b[0m"


class Tour:
    """One tour: its session, narration and pauses.

        with Tour("exits", __doc__) as tour:
            tour.step("...")
            tour.say("...")
            tour.pause("start the panes")
            tour.session.new_pane(...)
    """

    def __init__(self, name, doc, add_args=None):
        parser = argparse.ArgumentParser(description=doc,
                                         formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument("--auto", action="store_true",
                            help="no prompts, no dashboard: run straight through (smoke test)")
        parser.add_argument("--no-dash", action="store_true",
                            help="home pane only, no sidebar (also the fallback without Textual)")
        if add_args:
            add_args(parser)
        self.args = parser.parse_args()
        self.auto = self.args.auto
        self.name = f"lte_demo_{name}"
        self.title = doc.strip().splitlines()[0]
        if not self.auto and not os.environ.get(_MARKER):
            self._relaunch()
        logging.basicConfig(level=logging.WARNING, format=f"{DIM}  log: %(message)s{RESET}")
        if self.auto:
            status_file = os.path.join(tempfile.mkdtemp(prefix="lte_demo_"), "status.json")
            self.session = TmuxSession.create(self.name, status_file=status_file)
            self.session.new_window("home")
        else:
            self.session = TmuxSession.current()
        out = subprocess.run(["tmux", "show-options", "-qv", "-t", self.name, "@lte_dash_window"],
                             capture_output=True, text=True).stdout
        self.has_dash = bool(out.strip())

    def _relaunch(self):
        """Run this script again as the home pane of a new dashboard session."""
        subprocess.run(["tmux", "kill-session", "-t", f"={self.name}"], capture_output=True)
        src = str(Path(libtmux_easy.__file__).resolve().parents[1])
        env = dict(os.environ, **{_MARKER: "1"})
        env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH")) if p)
        cmd = [sys.executable, "-m", "libtmux_easy.launch", "--name", self.name]
        if self.args.no_dash:
            cmd.append("--no-dash")
        cmd += ["--", sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:]]
        os.execvpe(cmd[0], cmd, env)

    # ----- narration -----

    def step(self, title):
        print(f"\n{BOLD}== {title}{RESET}")

    def say(self, text):
        print(textwrap.indent(textwrap.dedent(text).strip("\n"), "  "))

    def pause(self, action="continue"):
        """Wait for Enter. --auto: a short beat instead."""
        if self.auto:
            time.sleep(0.5)
            return
        try:
            termios.tcflush(sys.stdin, termios.TCIFLUSH)  # drop Enters pressed early
        except (termios.error, ValueError):
            pass
        try:
            input(f"\n  {BOLD}[Enter]{RESET} {action} ")
        except EOFError:
            pass

    def wait(self, pane, timeout=None):
        """Block until 'pane' exits; print how."""
        status = self.session.wait_for_exit(pane, timeout=timeout)
        print(f"  {self.session.get_pane_name(pane)}: {status}")
        return status

    def event(self, status):
        """An on_exit callback that announces the exit here, as it happens."""
        print(f"\n  {BOLD}event:{RESET} {status.name} {status.exit_status}")

    def status(self):
        print()
        self.session.print_status()

    # ----- lifecycle -----

    def __enter__(self):
        print(f"{BOLD}{self.title}{RESET}\n")
        if not self.auto:
            self.say(HOW_TO)
        return self

    def __exit__(self, exc_type, exc, tb):
        code = 0
        if exc_type is KeyboardInterrupt:
            code = 130
        elif exc_type is not None:
            traceback.print_exception(exc_type, exc, tb)
            code = 1
        if code == 0:
            self.step("Done")
            self.say("Explore as long as you like: the panes stay until the tour ends.")
            self.pause("to end the tour (stops its panes and closes the session)")
        self.session.stop_all(sigint_timeout=2, sigterm_timeout=2)
        self.session.kill_session()
        if not self.auto:
            # lte-session finishes once nobody is attached.
            subprocess.run(["tmux", "detach-client", "-s", self.name], capture_output=True)
        sys.exit(code)


HOW_TO = """
    You are in the dashboard's home pane. The sidebar on the left lists every
    pane: j/k to browse (the right side follows), "home" to come back here,
    Enter to type into the selected pane. Press Enter here to go on.
"""


def lte_run():
    """How to type lte-run in a pane: the bare name if installed."""
    return _wrap_prefix()


# --------------------------------------------------------------------------- #
# Workloads. Single-line: a shell-mode command is typed, and a newline would
# submit it early.
# --------------------------------------------------------------------------- #

def exits_after(seconds, code=0):
    """Prints a countdown, then exits with 'code'."""
    return (f"sh -c 'for i in $(seq {seconds} -1 1); do echo \"exit {code} in $i\"; sleep 1; done;"
            f" exit {code}'")


def runs_forever(label="working"):
    """Prints a line a second until signalled."""
    return (f"python3 -u -c 'import time; "
            f"[(print(\"{label}\", i), time.sleep(1)) for i in range(10**9)]'")


def ignores_sigint():
    """Ignores SIGINT: stop_pane() has to escalate to SIGTERM."""
    return "sh -c 'trap \"\" INT; echo \"ignoring SIGINT\"; exec sleep 86400'"


def ignores_sigint_and_sigterm():
    """Ignores both: stop_pane() has to go all the way to SIGKILL."""
    return "sh -c 'trap \"\" INT TERM; echo \"ignoring SIGINT and SIGTERM\"; exec sleep 86400'"


def fake_launch(*extra):
    """A stand-in for 'ros2 launch'; see workloads/fake_launch.py."""
    script = HERE / "workloads" / "fake_launch.py"
    return " ".join(["python3", "-u", str(script), *(str(a) for a in extra)])
