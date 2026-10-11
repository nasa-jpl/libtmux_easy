"""pytest plugin: launch the processes a test needs, watch them, stop them.

Enable it in conftest.py:

    pytest_plugins = ["libtmux_easy.testing"]

A test module says what to launch, as 'window:name' -> command:

    PROCESSES = {"servers:web": "python3 -m http.server 8000"}

    def test_serves(processes):
        ...                      # processes.output("servers:web"), .check()

The 'processes' fixture (module scope) starts them; a test fails if one
exits before teardown, even if the test never noticed. Teardown stops them
(SIGINT, then SIGTERM, SIGKILL) and errors unless each exited 0 on SIGINT.

Modes (--lte-mode; default auto: dash under lte-session, else tmux if
installed, else shell):

  shell  plain subprocesses; output to the terminal, prefixed '[name]'
  tmux   one wrap-mode pane each, in a detached session for the run; only
         test output in the terminal. Kept on failure (--lte-keep)
  dash   under lte-session: the panes join its session and dashboard, and
         stay until you leave

Override these fixtures in conftest.py to adapt it:

  lte_pane_setup     a shell command run before each process, e.g. to
                     source an environment (panes start a fresh login shell)
  lte_output_checks  callables (name, output) -> [problem, ...], for deaths
                     only the output shows, e.g. a child of a launcher

Also 'tmux_session' (function scope): a TmuxSession for the test to fill
itself; under lte-session its windows are kept, renamed '<test>/<window>',
unless --lte-close-windows.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import threading

import pytest

from . import dash
from ._tmux import option
from .status import ExitStatus, PaneStatus
from .tmux_lib import PANE_MODE_WRAP, TmuxSession, _pane_cmd

MODES = ("shell", "tmux", "dash")


def pytest_addoption(parser):
    group = parser.getgroup("libtmux_easy")
    group.addoption("--lte-mode", choices=("auto",) + MODES, default="auto",
                    help="where 'processes' run (default: auto)")
    group.addoption("--lte-keep", choices=("failure", "always", "never"), default="failure",
                    help="tmux mode: when to leave the session up after the run")
    group.addoption("--lte-close-windows", action="store_true",
                    help="close each test's tmux windows after it, even under lte-session")


def under_lte_session() -> bool:
    """True inside a session with a dashboard, e.g. lte-session's."""
    pane = os.environ.get("TMUX_PANE")
    return bool(pane) and bool(option(pane, dash.WINDOW_OPTION))


def pytest_configure(config):
    mode = config.getoption("lte_mode")
    if mode == "auto":
        mode = "dash" if under_lte_session() else "tmux" if shutil.which("tmux") else "shell"
    elif mode == "dash" and not under_lte_session():
        raise pytest.UsageError("--lte-mode=dash needs lte-session: lte-session -- pytest ...")
    config.lte_mode = mode
    config.lte_session = None  # the run's TmuxSession, made on first use
    config.lte_kept = False
    # The real terminal, for shell mode's output: pytest captures fd 1 during
    # tests, but not here.
    config.lte_tty = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)


def _session_name():
    return f"lte_test_{os.getpid()}"


def pytest_report_header(config):
    if config.lte_mode == "tmux":
        return f"lte mode: tmux (watch: tmux attach -t {_session_name()})"
    return f"lte mode: {config.lte_mode}"


def _run_session(config):
    if config.lte_session is None:
        config.lte_session = (TmuxSession.current() if config.lte_mode == "dash"
                              else TmuxSession.create(_session_name()))
    return config.lte_session


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    tmux = getattr(config, "lte_session", None)
    if tmux is None:
        return
    keep = config.getoption("lte_keep")
    if config.lte_mode == "dash" or keep == "always" or (keep == "failure" and exitstatus):
        tmux.release(keep_windows=True)
        config.lte_kept = config.lte_mode == "tmux"
    else:
        tmux.kill_session()


def pytest_terminal_summary(terminalreporter, config):
    if getattr(config, "lte_kept", False):
        terminalreporter.write_sep(
            "-", f"processes kept: tmux attach -t {_session_name()}  (then tmux kill-session)")


# ---------- backends ----------

class _TmuxBackend:
    """Each process in a wrap-mode pane; one window per 'window' group, named
    '<module>/<window>' so modules sharing the session don't clash."""

    def __init__(self, session, prefix):
        self.session = session
        self.prefix = prefix
        self.panes = {}  # name -> Pane

    def start(self, name, command, setup, on_exit):
        window, _, pane_name = name.partition(":")
        if not any(n.partition(":")[0] == window for n in self.panes):
            self.session.new_window(f"{self.prefix}/{window}")
            self.session.send_command_before(setup, window=f"{self.prefix}/{window}")
        names = {}
        pane = self.session.new_pane(pane_name, command, mode=PANE_MODE_WRAP,
                                     on_exit=lambda s: on_exit(names[s.pane_id], s))
        names[pane.id] = name
        self.panes[name] = pane

    def output(self, name):
        return "\n".join(_pane_cmd(self.panes[name], "capture-pane", "-p", "-J", "-S", "-").stdout)

    def stop_all(self, sigint_timeout):
        self.session.stop_all(sigint_timeout=sigint_timeout)

    def status(self, name):
        return self.session.pane_status(self.panes[name])


class _Subprocess:
    def __init__(self, name, command, on_exit, tty, lock):
        self.name = name
        self.command = command
        self.lines = []
        self.stopped = None
        # stdin is a pty, as in a terminal. Some programs act on that: 'ros2
        # launch' with a non-tty stdin re-sends SIGINT to children that
        # already got the group's, and Python children die of the second.
        tty_in, stdin = os.openpty()
        self.proc = subprocess.Popen(
            ["bash", "-c", command], stdin=stdin, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace",
            start_new_session=True,  # a process group of its own, for SIGINT
            env=dict(os.environ, PYTHONUNBUFFERED="1"))
        os.close(stdin)

        def read():
            for line in self.proc.stdout:
                self.lines.append(line)
                with lock:
                    tty.write(f"[{name}] {line}")

        def wait():
            self.proc.wait()
            os.close(tty_in)
            on_exit(name, self.status())

        threading.Thread(target=read, daemon=True).start()
        threading.Thread(target=wait, daemon=True).start()

    def status(self):
        rc = self.proc.poll()
        exit_status = (ExitStatus() if rc is None else
                       ExitStatus(signal=-rc) if rc < 0 else ExitStatus(code=rc))
        return PaneStatus(name=self.name, pane_id="", mode="subprocess", command=self.command,
                          target_pids=(self.proc.pid,), running=rc is None,
                          exit_status=exit_status, stopped=self.stopped)

    def stop(self, sigint_timeout):
        """SIGINT the group (a Ctrl-C), escalating like stop_pane()."""
        for sig, timeout in ((signal.SIGINT, sigint_timeout), (signal.SIGTERM, 5),
                             (signal.SIGKILL, 2)):
            if self.proc.poll() is not None:
                return
            self.stopped = sig.name
            try:
                os.killpg(self.proc.pid, sig)
                self.proc.wait(timeout)
            except ProcessLookupError:
                return
            except subprocess.TimeoutExpired:
                pass


class _ShellBackend:
    """Plain subprocesses, their output in the terminal."""

    def __init__(self, tty):
        self.tty = tty
        self.lock = threading.Lock()
        self.procs = {}

    def start(self, name, command, setup, on_exit):
        if setup:
            command = f"{setup} && {command}"
        self.procs[name] = _Subprocess(name, command, on_exit, self.tty, self.lock)

    def output(self, name):
        return "".join(self.procs[name].lines)

    def stop_all(self, sigint_timeout):
        threads = [threading.Thread(target=p.stop, args=(sigint_timeout,))
                   for p in self.procs.values()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def status(self, name):
        return self.procs[name].status()


# ---------- processes ----------

class Processes:
    """Processes under test and their health, whatever the mode."""

    def __init__(self, backend, commands, setup=None, checks=()):
        self.backend = backend
        self.names = list(commands)
        self.checks = list(checks)
        self.died = []  # (name, PaneStatus) of each exit before shutdown
        self.stopping = False
        for name, command in commands.items():
            backend.start(name, command, setup, self._on_exit)

    def _on_exit(self, name, status):  # a watcher thread
        if not self.stopping:
            self.died.append((name, status))

    def output(self, name) -> str:
        """Everything the process has printed so far."""
        return self.backend.output(name)

    def _check_output(self):
        return [problem for name in self.names for check in self.checks
                for problem in check(name, self.output(name))]

    def problems(self) -> list:
        found = [f"{name} {status.exit_status}" for name, status in self.died]
        found += self._check_output()
        for name, _ in self.died:
            tail = self.output(name).strip().splitlines()[-8:]
            found.append(f"  last output of {name}:\n    " + "\n    ".join(tail))
        return found

    def check(self):
        """Raise AssertionError if anything died. Runs after every test that
        uses 'processes'; call it while waiting on them to fail fast."""
        problems = self.problems()
        if problems:
            raise AssertionError("launched process died early:\n" + "\n".join(problems))

    def shutdown(self, sigint_timeout=10.0) -> list:
        """Stop everything; return what did not shut down cleanly: needed
        more than SIGINT, exited non-zero, or failed an output check."""
        self.stopping = True
        self.backend.stop_all(sigint_timeout)
        unclean = []
        for name in self.names:
            status = self.backend.status(name)
            if status.stopped not in (None, "SIGINT") or not status.exit_status.ok:
                unclean.append(f"{name}: {status}")
        return unclean + self._check_output()


@pytest.fixture(scope="session")
def lte_pane_setup():
    """A shell command run before each process. None: nothing."""
    return None


@pytest.fixture(scope="session")
def lte_output_checks():
    """Callables (name, output) -> list of problems."""
    return []


@pytest.fixture(scope="module")
def processes(request, lte_pane_setup, lte_output_checks):
    config = request.config
    commands = getattr(request.module, "PROCESSES", None)
    if not commands:
        raise pytest.UsageError(f"{request.module.__name__}: no PROCESSES to launch")
    if config.lte_mode == "shell":
        backend = _ShellBackend(config.lte_tty)
    else:
        backend = _TmuxBackend(_run_session(config), request.module.__name__.rpartition(".")[2])
    procs = Processes(backend, commands, lte_pane_setup, lte_output_checks)
    yield procs
    unclean = procs.shutdown(getattr(request.module, "SIGINT_TIMEOUT", 10.0))
    if unclean:
        pytest.fail("did not shut down cleanly:\n" + "\n".join(unclean), pytrace=False)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Fail the test itself, not just teardown, if a process died while it ran."""
    procs = item.funcargs.get("processes")
    outcome = yield
    if procs is not None and outcome.excinfo is None:
        try:
            procs.check()
        except AssertionError as exc:
            outcome.force_exception(exc)


@pytest.fixture
def tmux_session(request):
    if os.environ.get("TMUX_PANE"):
        session = TmuxSession.current()
    else:
        name = re.sub(r"[^A-Za-z0-9_-]", "_", f"lte_test_{request.node.name}")
        session = TmuxSession.create(name, kill_existing=True)
    try:
        yield session
    finally:
        session.stop_all()
        keep = (session._adopted and session._dash_window is not None
                and not request.config.getoption("lte_close_windows"))
        if keep:
            session.release(keep_windows=True, label=request.node.name)
        else:
            session.kill_session()
