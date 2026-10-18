"""Shared scaffolding for the libtmux_easy tours.

A tour relaunches itself under lte-session: the script runs in the
dashboard's home pane, where you read what it says and press Enter to go on,
and the panes it starts appear in the sidebar. Browse them with the arrows; select
"home" to come back. --auto runs a tour unattended (run_all.py).

Works from a bare checkout: falls back to ../src.
"""

import argparse
import ast
import code
import functools
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
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    import libtmux_easy

from libtmux_easy import PANE_MODE_EXEC, PANE_MODE_SHELL, PANE_MODE_WRAP, TmuxSession
from libtmux_easy.output import Capture, OutputWatch
from libtmux_easy.tmux_lib import _wrap_prefix

HERE = Path(__file__).resolve().parent
MODES = (PANE_MODE_SHELL, PANE_MODE_EXEC, PANE_MODE_WRAP)
_MARKER = "LTE_DEMO_TOUR"  # set once relaunched under lte-session

BOLD, DIM, CYAN, RESET = "\x1b[1m", "\x1b[2m", "\x1b[36m", "\x1b[0m"
PS1, PS2 = "  >>> ", "  ... "


class Echo:
    """Prints each line of the tour script that calls the library, as it runs.

    wrap() a session (or a Capture): a call made from the script prints the
    statement it is in first, like '>>> s.new_pane(...)'. A call in a loop or
    a 'with' block prints the whole block, once.
    """

    QUIET = {"get_pane_name", "get_window_name", "get_pane", "get_window",
             "resolve_target_pane", "resolve_target_window"}

    def __init__(self, script):
        self.script = os.path.realpath(script)
        self.shown = None  # (first, last) line of the block printed last
        self._trees = {}

    def wrap(self, obj):
        return _Echoed(obj, self)

    def reset(self):
        """Forget the last block: its next call prints it again."""
        self.shown = None

    def show(self, text):
        lines = textwrap.dedent(text).strip("\n").splitlines()
        for i, line in enumerate(lines):
            print(f"{DIM}{PS2 if i else PS1}{RESET}{CYAN}{line}{RESET}")

    def called(self, frame):
        filename = frame.f_code.co_filename
        if os.path.realpath(filename) != self.script:
            return
        line = frame.f_lineno
        if self.shown and self.shown[0] <= line <= self.shown[1]:
            return
        source, tree = self._parse(filename)
        node = self._block(tree, line)
        if node is None:
            return
        self.shown = (node.lineno, node.end_lineno)
        self.show("\n".join(source[node.lineno - 1:node.end_lineno]))

    def _parse(self, filename):
        if filename not in self._trees:
            text = Path(filename).read_text()
            self._trees[filename] = (text.splitlines(), ast.parse(text))
        return self._trees[filename]

    @staticmethod
    def _block(tree, line):
        """The outermost loop around 'line', else its innermost statement."""
        around = [n for n in ast.walk(tree) if isinstance(n, ast.stmt)
                  and not isinstance(n, (ast.FunctionDef, ast.ClassDef))
                  and n.lineno <= line <= n.end_lineno]
        loops = [n for n in around if isinstance(n, (ast.For, ast.While))]
        if loops:
            return min(loops, key=lambda n: n.lineno)
        return min(around, key=lambda n: n.end_lineno - n.lineno, default=None)

    def result(self, value):
        return self.wrap(value) if isinstance(value, (Capture, OutputWatch, TmuxSession)) else value


class _Echoed:
    """'target', with its public methods announced by 'echo'."""

    def __init__(self, target, echo):
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_echo", echo)

    def __getattr__(self, name):
        value = getattr(self._target, name)
        if name.startswith("_") or name in Echo.QUIET or not callable(value):
            return value
        echo = self._echo

        @functools.wraps(value)
        def call(*args, **kwargs):
            echo.called(sys._getframe(1))
            return echo.result(value(*args, **kwargs))
        return call

    def __setattr__(self, name, value):
        setattr(self._target, name, value)

    @property
    def __class__(self):  # isinstance() sees the target's class
        return type(self._target)

    def __dir__(self):
        return dir(self._target)

    def __repr__(self):
        return repr(self._target)


def flush_input():
    """Drop keys pressed before a prompt (an early Enter would answer it)."""
    try:
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except (termios.error, ValueError):
        pass


class Console:
    """A Python prompt at each pause: lines typed run with the caller's
    variables in scope, and names defined stay for later pauses. Enter on an
    empty line (or Ctrl-D) goes on."""

    def __init__(self, echo=None):
        self.echo = echo
        self._console = None

    def pause(self, action="continue", frame=None):
        flush_input()
        print(f"\n  {BOLD}[Enter]{RESET} {action}{DIM}, or type Python{RESET}")
        self._repl(frame or sys._getframe(1))
        if self.echo is not None:
            self.echo.reset()

    def _repl(self, frame):
        if self._console is None:
            self._console = code.InteractiveConsole()
            try:
                import readline
                import rlcompleter
                readline.set_completer(rlcompleter.Completer(self._console.locals).complete)
                readline.parse_and_bind("tab: complete")
            except ImportError:
                pass
        console = self._console
        console.locals.update(frame.f_globals)
        console.locals.update(frame.f_locals)
        ps1, ps2 = (f"\001{DIM}\002{p}\001{RESET}\002" for p in (PS1, PS2))
        more = False
        while True:
            try:
                line = input(ps2 if more else ps1)
            except EOFError:
                print()
                console.resetbuffer()
                return
            except KeyboardInterrupt:
                if not more:
                    raise
                print()
                console.resetbuffer()
                more = False
                continue
            if not line and not more:
                return
            more = console.push(line)


class Tour:
    """One tour: its session, narration and pauses.

        with Tour("exits", __doc__) as tour:
            tour.step("...")
            tour.say("...")
            tour.pause("start the panes")
            tour.session.new_pane(...)
    """

    def __init__(self, name, doc, add_args=None, echo=False):
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
        self.echo = self.console = None
        if echo:
            self.echo = Echo(sys.argv[0])
            self.console = Console(self.echo)
            self.session = self.echo.wrap(self.session)

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
        """Wait for Enter. --auto: a short beat instead. With echo, a Python
        prompt first (Console)."""
        if self.auto:
            time.sleep(0.5)
            return
        if self.console is not None:
            self.console.pause(action, sys._getframe(1))
            return
        flush_input()
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
        if self.echo:
            self.say(ECHO_HOW_TO)
            print()
            how = f"create({self.name!r})" if self.auto else "current()  # the session this runs in"
            self.echo.show(f"s = TmuxSession.{how}")
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
    pane: arrows to browse (the right side follows), "home" to come back here,
    Enter to type into the selected pane. Press Enter here to go on.
"""

ECHO_HOW_TO = """
    Lines starting >>> are this script's library calls, shown as they run.
    At each [Enter] prompt you can type Python first, with the script's
    variables (the session, its panes, ...) in scope. Enter on an empty
    line goes on.
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
