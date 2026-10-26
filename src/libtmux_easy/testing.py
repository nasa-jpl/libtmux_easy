"""pytest plugin: launch the processes a test needs, watch them, stop them.

Enable it in conftest.py:

    pytest_plugins = ["libtmux_easy.testing"]

A test class brings its processes up in setup_class and down in
teardown_class:

    from libtmux_easy.testing import Processes, check_health

    class TestSystem:
        @classmethod
        def setup_class(cls):
            cls.procs = Processes(cls, fail_on={"*": r"Traceback"})
            cls.procs.start("sim:server", "./server", ready=r"listening")
            cls.procs.wait_until(server_answers)   # any gate

        @classmethod
        def teardown_class(cls):
            cls.procs.close()      # fails unless each exits 0 on SIGINT

        def test_x(self):
            with Processes() as procs:   # this test's own, stopped after it
                procs.start("tools:probe", "./probe")

A process that dies, or prints a fail_on line, fails the test running
when it happened. A failed test stops the class's processes at once and
skips the rest of the class.

Where processes run: subprocesses (default; output to log files,
--echo PATTERN: also to the terminal), --tmux (the dashboard,
attached), --tmux --no-attach (a detached tmux session). --keep-open: never
stop anything. --arg NAME=VALUE: read with arg().

Reference: docs/pytest-plugin.md.
"""

import contextlib
import fnmatch
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

import pytest

from . import dash
from ._tmux import option
from .output import PaneExited
from .status import ExitStatus, PaneStatus
from .tmux_lib import PANE_MODE_WRAP, TmuxSession, _pane_cmd
from .wrap import plain_line


def pytest_addoption(parser):
    group = parser.getgroup("libtmux_easy")
    group.addoption("--tmux", action="store_true",
                    help="run processes in tmux and attach: pytest reruns under lte-session, "
                         "in the dashboard's home pane")
    group.addoption("--no-attach", action="store_true",
                    help="as --tmux, in a detached tmux session instead (tmux attach to watch)")
    group.addoption("--keep-open", action="store_true",
                    help="never stop processes; leave the tmux session up (implies --tmux)")
    group.addoption("--arg", action="append", default=[], metavar="NAME=VALUE",
                    help="a value for the tests to read with arg(NAME), like launch_test's "
                         "NAME:=VALUE (repeatable)")
    group.addoption("--echo", action="append", default=[], metavar="PATTERN",
                    help="subprocesses: also print the output of those whose 'window:name' "
                         "matches, prefixed [name]; '*' for all (repeatable)")


def under_lte_session() -> bool:
    """True inside a session with a dashboard, e.g. lte-session's."""
    pane = os.environ.get("TMUX_PANE")
    return bool(pane) and bool(option(pane, dash.WINDOW_OPTION))


def _wants_tmux(config):
    return any(config.getoption(o) for o in ("tmux", "no_attach", "keep_open"))


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    """--tmux in a terminal: rerun this pytest under lte-session, as the
    dashboard's home pane, and attach. Its exit status is pytest's."""
    if (not _wants_tmux(config) or config.getoption("no_attach")
            or under_lte_session() or not shutil.which("tmux")):
        return None
    # pytest captures fds 0-2 by now: look at, and exec with, the real ones.
    capman = config.pluginmanager.getplugin("capturemanager")
    with capman.global_and_fixture_disabled() if capman else contextlib.nullcontext():
        if not (os.isatty(1) and _in_foreground()):
            return None
    if capman:
        capman.suspend(in_=True)
    cmd = [sys.executable, "-m", "libtmux_easy.launch",
           "--name", _free_session_name(f"lte_test_{os.getpid()}")]
    if config.getoption("keep_open"):
        cmd.append("--keep")
    cmd += ["--", sys.executable, "-m", "pytest", *config.invocation_params.args]
    os.chdir(config.invocation_params.dir)
    os.execv(sys.executable, cmd)


def _in_foreground():
    """This process owns its terminal: stdin is a tty too (checked without
    it, as pytest swaps fd 0 for /dev/null)."""
    try:
        fd = os.open("/dev/tty", os.O_RDONLY)
    except OSError:
        return False
    try:
        return os.tcgetpgrp(fd) == os.getpgrp()
    finally:
        os.close(fd)


def pytest_configure(config):
    _args.clear()
    for item in config.getoption("arg"):
        name, eq, value = item.partition("=")
        if not (eq and name):
            raise pytest.UsageError(f"--arg {item}: expected NAME=VALUE")
        _args[name] = value
    # Under lte-session: its session and dashboard. Else --tmux & co: a
    # detached session ('--tmux' in a terminal reran pytest under
    # lte-session already). Else subprocesses.
    if under_lte_session():
        mode = "dash"
    elif _wants_tmux(config):
        if not shutil.which("tmux"):
            raise pytest.UsageError("--tmux: tmux is not installed")
        mode = "tmux"
    else:
        mode = "subprocess"
    config.lte_mode = mode
    config.lte_keep_open = config.getoption("keep_open")
    config.lte_session = None  # the run's TmuxSession, made on first use
    config.lte_session_name = (_free_session_name(f"lte_test_{os.getpid()}")
                               if mode == "tmux" else None)
    config.lte_kept = False
    config.lte_log_dirs = []
    config.lte_echo = config.getoption("echo")
    # The real terminal, for --echo: pytest captures fd 1 during tests,
    # but not here.
    config.lte_tty = (os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)
                      if config.lte_echo else None)


def _free_session_name(base):
    """'base', or 'base-N' if a session has it: one kept by --keep-open, from
    a run whose pid this one reuses."""
    name, n = base, 1
    while subprocess.run(["tmux", "has-session", "-t", f"={name}"],
                         capture_output=True).returncode == 0:
        n += 1
        name = f"{base}-{n}"
    return name


def pytest_report_header(config):
    logs = f"process logs: {config._tmp_path_factory.getbasetemp()}"
    if config.lte_mode == "tmux":
        return [f"lte mode: tmux (watch: tmux attach -t {config.lte_session_name})", logs]
    return [f"lte mode: {config.lte_mode}", logs]


def _run_session(config):
    # Processes get pytest's environment, as subprocesses do: the tmux
    # server's may predate an activated venv or a sourced workspace.
    if config.lte_session is None:
        config.lte_session = (TmuxSession.current(env=os.environ) if config.lte_mode == "dash"
                              else TmuxSession.create(config.lte_session_name, kill_existing=False,
                                                   env=os.environ))
    return config.lte_session


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    for procs in list(_open):  # never closed: don't leave them running
        procs.close(check=False)
    tmux = getattr(config, "lte_session", None)
    if tmux is None:
        return
    # A detached session is kept on failure, to attach and look.
    if config.lte_mode == "dash" or config.lte_keep_open or exitstatus:
        tmux.release(keep_windows=True)
        config.lte_kept = config.lte_mode == "tmux"
    else:
        tmux.kill_session()


_args = {}  # --arg NAME=VALUE, set before collection
_args_read = set()


def arg(name, default=None):
    """The value of --arg NAME=VALUE, converted to the type of 'default'
    if one is given (bools take 1/0, true/false, yes/no). Without the
    option: 'default', or a usage error if there is none. Usable at
    import time."""
    _args_read.add(name)
    if name not in _args:
        if default is None:
            raise pytest.UsageError(f"this test needs --arg {name}=VALUE")
        return default
    value = _args[name]
    if default is None or isinstance(default, str):
        return value
    if isinstance(default, bool):
        if value.lower() not in ("1", "0", "true", "false", "yes", "no"):
            raise pytest.UsageError(f"--arg {name}={value}: expected true or false")
        return value.lower() in ("1", "true", "yes")
    try:
        return type(default)(value)
    except ValueError:
        raise pytest.UsageError(f"--arg {name}={value}: expected {type(default).__name__}") from None


def pytest_terminal_summary(terminalreporter, config):
    unread = sorted(set(_args) - _args_read)
    if unread:
        terminalreporter.write_line(f"warning: no test read --arg {', '.join(unread)}", yellow=True)
    for d in getattr(config, "lte_log_dirs", ()):
        terminalreporter.write_line(f"process logs: {d}")
    if getattr(config, "lte_kept", False):
        state = "still running" if config.lte_keep_open else "kept"
        terminalreporter.write_sep(
            "-", f"processes {state}: tmux attach -t {config.lte_session_name}  (then tmux kill-session)")


# ---------- backends ----------

def _unique_prefix(session, prefix):
    """'prefix', or 'prefix#N' if a window '<prefix>/...' exists already:
    another run in the same session, or windows a past run kept."""
    names = [w.name for w in session.windows]
    candidate, n = prefix, 1
    while any(name.startswith(candidate + "/") for name in names):
        n += 1
        candidate = f"{prefix}#{n}"
    return candidate


class _TmuxBackend:
    """Each process in a wrap-mode pane; one window per 'window' group, named
    '<prefix>/<window>' so classes sharing the session don't clash. All
    groups of a class share one. A restart gets a new pane, '<name>~N',
    next to the old one's output."""

    def __init__(self, session, prefix):
        self.session = session
        self.prefix = _unique_prefix(session, prefix)
        self.panes = {}  # name -> Pane
        self.runs = {}  # name -> how many times started
        self.logs = {}  # name -> its log, which each start rewrites (lte-run --tee)
        self.windows = set()

    def start(self, name, command, setup, on_exit, on_line=None, log=None):
        window, _, pane_name = name.partition(":")
        full_window = f"{self.prefix}/{window}"
        if window not in self.windows:  # this class's: its groups share this backend
            self.session.new_window(full_window)
            self.session.send_command_before(setup, window=full_window)
            self.windows.add(window)
        else:  # new_pane() goes in the current window; don't move the viewer
            w = self.session.current_window = self.session.resolve_target_window(full_window)
            self.session.current_pane = w.panes[-1]
        self.runs[name] = self.runs.get(name, 0) + 1
        if self.runs[name] > 1:
            pane_name = f"{pane_name}~{self.runs[name]}"
        on_output = {_EVERY_LINE: lambda m: on_line(m.line)} if on_line else None
        env = {"LTE_LOG_DIR": os.path.dirname(log)} if log else None
        if log:
            self.logs[name] = log
        self.panes[name] = self.session.new_pane(pane_name, command, mode=PANE_MODE_WRAP,
                                                 on_exit=lambda s: on_exit(name, s),
                                                 on_output=on_output, tee=log, env=env)

    def wait_for(self, name, regex, timeout):
        """The first re.Match in the output; TimeoutError, or PaneExited."""
        return self.session.wait_for_output(self.panes[name], regex, timeout).match

    def output(self, name):
        return "\n".join(self.lines(name))

    def lines(self, name):
        """What the latest start printed: from its log (lte-run --tee), else
        the pane, which also shows the shell's prompt and the command."""
        if name not in self.logs:
            return _pane_cmd(self.panes[name], "capture-pane", "-p", "-J", "-S", "-").stdout
        try:
            with open(self.logs[name], errors="replace") as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def stop(self, name, sigint_timeout):
        self.session.stop_pane(self.panes[name], sigint_timeout=sigint_timeout)

    def stop_all(self, names, sigint_timeout):
        _parallel(self.stop, [n for n in names if self.session.is_running(self.panes[n])],
                  sigint_timeout)

    def status(self, name):
        return self.session.pane_status(self.panes[name])


_EVERY_LINE = re.compile("")


def _parallel(fn, names, *args):
    threads = [threading.Thread(target=fn, args=(name,) + args) for name in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


class _Subprocess:
    def __init__(self, name, command, on_exit, tty, lock, on_line=None, log=None):
        self.name = name
        self.command = command
        self.lines = []
        self.plain = []  # the lines as plain text, for matching
        self.cond = threading.Condition()
        self.done_reading = False
        self.stopped = None
        # stdin is a pty, as in a terminal. Some programs act on that: 'ros2
        # launch' with a non-tty stdin re-sends SIGINT to children that
        # already got the group's, and Python children die of the second.
        tty_in, stdin = os.openpty()
        # Without BASH_ENV, as for panes (see _tmux.SKIP_ENV).
        env = {k: v for k, v in os.environ.items() if k != "BASH_ENV"}
        self.proc = subprocess.Popen(
            ["bash", "-c", command], stdin=stdin, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace",
            start_new_session=True,  # a process group of its own, for SIGINT
            env=dict(env, PYTHONUNBUFFERED="1",
                     **({"LTE_LOG_DIR": os.path.dirname(log)} if log else {})))
        os.close(stdin)
        log_file = open(log, "a", buffering=1) if log else None  # line-buffered: tail -f

        def read():
            for line in self.proc.stdout:
                plain = plain_line(line.rstrip("\n").encode()).decode()
                if log_file:
                    log_file.write(plain + "\n")
                with self.cond:
                    self.lines.append(line)
                    self.plain.append(plain)
                    self.cond.notify_all()
                if tty:
                    with lock:
                        tty.write(f"[{name}] {line}")
                if on_line:
                    on_line(plain)
            if log_file:
                log_file.close()
            with self.cond:
                self.done_reading = True
                self.cond.notify_all()

        def wait():
            self.proc.wait()
            os.close(tty_in)
            on_exit(name, self.status())

        threading.Thread(target=read, daemon=True).start()
        self.waiter = threading.Thread(target=wait, daemon=True)
        self.waiter.start()

    def status(self):
        rc = self.proc.poll()
        exit_status = (ExitStatus() if rc is None else
                       ExitStatus(signal=-rc) if rc < 0 else ExitStatus(code=rc))
        return PaneStatus(name=self.name, pane_id="", mode="subprocess", command=self.command,
                          target_pids=(self.proc.pid,), running=rc is None,
                          exit_status=exit_status, stopped=self.stopped)

    def wait_for(self, regex, timeout):
        """The first re.Match in the output; TimeoutError, or PaneExited."""
        seen = 0
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for line in self.plain[seen:]:
                    m = regex.search(line)
                    if m:
                        return m
                seen = len(self.plain)
                if self.done_reading:
                    self.proc.wait()
                    raise PaneExited(self.status())
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"{self.name}: no line matching {regex.pattern!r}")
                self.cond.wait(left)

    def stop(self, sigint_timeout):
        """SIGINT the group (a Ctrl-C), escalating like stop_pane()."""
        for sig, timeout in ((signal.SIGINT, sigint_timeout), (signal.SIGTERM, 5),
                             (signal.SIGKILL, 2)):
            if self.proc.poll() is not None:
                break
            self.stopped = sig.name
            try:
                os.killpg(self.proc.pid, sig)
                self.proc.wait(timeout)
            except ProcessLookupError:
                break
            except subprocess.TimeoutExpired:
                pass
        self.waiter.join(1)  # on_exit has fired


class _SubprocessBackend:
    """Plain subprocesses, their output in their logs; also on 'tty' for
    names matching an 'echo' pattern. A restart replaces the old one."""

    def __init__(self, tty, echo):
        self.tty = tty
        self.echo = echo
        self.lock = threading.Lock()
        self.procs = {}

    def start(self, name, command, setup, on_exit, on_line=None, log=None):
        if setup:
            command = f"{setup} && {command}"
        tty = self.tty if any(fnmatch.fnmatchcase(name, p) for p in self.echo) else None
        self.procs[name] = _Subprocess(name, command, on_exit, tty, self.lock, on_line, log)

    def wait_for(self, name, regex, timeout):
        return self.procs[name].wait_for(regex, timeout)

    def output(self, name):
        return "".join(self.procs[name].lines)

    def lines(self, name):
        return list(self.procs[name].plain)

    def stop(self, name, sigint_timeout):
        self.procs[name].stop(sigint_timeout)

    def stop_all(self, names, sigint_timeout):
        _parallel(self.stop, names, sigint_timeout)

    def status(self, name):
        return self.procs[name].status()


# ---------- processes ----------

class _Run:
    """One start of a process: whether its exit is expected, and when it came."""

    def __init__(self, exits=None, stop_codes=(0,)):
        self.exits = exits  # codes it may finish with on its own; None: it must not
        self.stop_codes = stop_codes  # codes it may exit with when stopped
        self.stopping = False
        self.died = False
        self.finished = False  # exited on its own, with a code in 'exits'
        self.exited = threading.Event()


def _codes(value):
    """An exit code, or several, as a tuple; None stays None."""
    if value is None:
        return None
    return (value,) if isinstance(value, int) else tuple(value)


def _exit_code(status):
    """The exit code, a signal as the shell's 128+N (SIGINT: 130)."""
    e = status.exit_status
    return e.code if e.code is not None else None if e.signal is None else 128 + e.signal


class Process:
    """One process of a group, as start() returns it: the group's methods
    for its name, and start() to start it again. It follows the name: still
    valid after a stop() and a start()."""

    def __init__(self, group, name):
        self.group = group
        self.name = name

    def __repr__(self):
        return f"<Process {self.name}>"

    def start(self):
        """Start it again: the same command and start() options."""
        command, options = self.group._commands[self.name]
        return self.group.start(self.name, command, **options)

    def stop(self, sigint_timeout=None, check=True) -> PaneStatus:
        return self.group.stop(self.name, sigint_timeout, check)

    def wait_for(self, pattern, timeout=10.0) -> "re.Match":
        return self.group.wait_for(self.name, pattern, timeout)

    def wait_for_in_order(self, patterns, timeout=10.0) -> list:
        return self.group.wait_for_in_order(self.name, patterns, timeout)

    def wait_for_exit(self, timeout=10.0) -> PaneStatus:
        return self.group.wait_for_exit(self.name, timeout)

    def status(self) -> PaneStatus:
        return self.group.status(self.name)

    def log(self):
        return self.group.log(self.name)

    def output(self) -> str:
        return self.group.output(self.name)


class Processes:
    """A group of processes under test and their health, in any mode.

    Make one in setup_class, close() it in teardown_class:

        cls.procs = Processes(cls, fail_on={"*": r"Traceback"})

    or in a test, for that test alone; closed at the end of the 'with', or
    else after the test:

        with Processes() as procs:

    'owner': the test class; None in a test: that test. A failed test of
    the owner stops the group at once, after on_abort() if given; a class
    also skips its remaining tests. Names (window:pane) are unique within a
    class: across its groups, and its tmux windows are '<Class>/<window>'.
    'fail_on': {name or "*": a regex, a function line -> problem or None,
    or a list of them}; a match is a death.
    'stop_codes': {name or "*": codes} allowed when stopped (default 0; a
    signal N is 128+N, so 130 allows dying of SIGINT).
    'sigint_timeout': seconds SIGINT gets before SIGTERM.
    'log_dir': where the logs go; default a temp dir named after the owner.
    """

    def __init__(self, owner=None, *, fail_on=None, stop_codes=None, sigint_timeout=10.0,
                 on_abort=None, log_dir=None):
        if not _plugin:
            raise RuntimeError("Processes() needs the plugin: "
                               'pytest_plugins = ["libtmux_easy.testing"] in conftest.py')
        if owner is None:
            owner = _plugin.get("item")
            if owner is None:
                raise TypeError("Processes() outside a test: in setup_class, Processes(cls)")
        if isinstance(owner, pytest.Item):
            self.scope = owner.cls or owner  # whose names and windows it shares
            test = re.sub(r"[^\w-]", "_", owner.name)
            prefix = owner.cls.__name__ if owner.cls else test
            log_name = f"{prefix}.{test}" if owner.cls else test
        elif isinstance(owner, type):
            self.scope = owner
            prefix = log_name = owner.__name__
        else:
            raise TypeError(f"Processes(owner): a test class, e.g. Processes(cls) in "
                            f"setup_class; got {owner!r}")
        config = _plugin["config"]
        if log_dir is None:
            log_dir = _plugin["tmp_path_factory"].mktemp(log_name)
        if config.lte_mode == "subprocess":
            self.backend = _SubprocessBackend(config.lte_tty, config.lte_echo)
        else:
            if self.scope not in _tmux_backends:
                _tmux_backends[self.scope] = _TmuxBackend(_run_session(config), prefix)
            self.backend = _tmux_backends[self.scope]
        config.lte_log_dirs.append(str(log_dir))
        self.owner = owner
        self.prefix = prefix
        self.log_dir = str(log_dir)
        self.keep_open = config.lte_keep_open
        self.setup = _plugin["setup"]
        self.fail_on = {name: [p] if isinstance(p, str) or callable(p) else list(p)
                        for name, p in (fail_on or {}).items()}
        self.stop_codes = dict(stop_codes or {})
        self.sigint_timeout = sigint_timeout
        self.on_abort = on_abort
        self.names = []
        self.died = []  # (name, PaneStatus, _Run) of each exit nobody asked for
        self.failed_on = []  # (problem, during a stop) of each fail_on hit
        self.stopping = False
        self._aborted = None  # why a failed test stopped everything
        self._runs = {}  # name -> _Run of its latest start
        self._commands = {}  # name -> (command, start()'s options): for Process.start()
        self._raised_died = self._raised_failed_on = 0  # how many check() raised
        _open.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:  # else the test failed: the plugin stops it, after on_abort()
            self.close()

    def start(self, name, command, ready=None, timeout=30.0, fail_on=(), exits=None,
              stop_codes=None) -> Process:
        """Start 'command' as 'window:name' (a name without ':' goes in a
        window of its own). With 'ready' (a regex), wait up to 'timeout' s
        for a line matching it, as wait_for() does. 'fail_on': more fail_on
        patterns for it. 'exits': a code (or several) it finishes with on
        its own, so that exit is no death; wait_for_exit() waits for it.
        'stop_codes': the codes it may exit with when stopped (default: the
        group's). A stopped name can be started again. Returns its Process."""
        name = self._name(name)
        if name in self._runs and self.status(name).running:
            raise ValueError(f"{name} is already running")
        if any(name in p._runs for p in _open if p is not self and p.scope is self.scope):
            raise ValueError(f"{name} is taken: another open Processes of {self.prefix} has it")
        patterns = self.fail_on.get("*", []) + self.fail_on.get(name, [])
        patterns += [fail_on] if isinstance(fail_on, str) or callable(fail_on) else list(fail_on)
        if stop_codes is None:
            stop_codes = self.stop_codes.get(name, self.stop_codes.get("*", 0))
        run = self._runs[name] = _Run(_codes(exits), _codes(stop_codes))
        self._commands[name] = (command, dict(ready=ready, timeout=timeout, fail_on=fail_on,
                                               exits=exits, stop_codes=stop_codes))
        if name not in self.names:
            self.names.append(name)
        self.backend.start(name, command, self.setup,
                           lambda n, status: self._on_exit(n, status, run),
                           self._on_line(name, [p if callable(p) else re.compile(p) for p in patterns]),
                           self.log(name))
        if ready is not None:
            self.wait_for(name, ready, timeout)
        return Process(self, name)

    def stop(self, name, sigint_timeout=None, check=True):
        """Stop it (SIGINT, then SIGTERM, SIGKILL); its exit is no death.
        With 'check', fail unless SIGINT stopped it, with one of its
        stop_codes. Returns its PaneStatus."""
        name = self._name(name)
        run = self._runs[name]
        run.stopping = True
        self.backend.stop(name, self.sigint_timeout if sigint_timeout is None else sigint_timeout)
        run.exited.wait(5)
        status = self.backend.status(name)
        if check and not run.finished and not self._clean(status, run):
            raise AssertionError(f"{name} did not stop cleanly: {status}\n    " + self._tail(name))
        return status

    def close(self, check=True):
        """Stop everything still running (SIGINT, then SIGTERM, SIGKILL).
        With 'check', fail unless each stopped cleanly: SIGINT was enough,
        with one of its stop_codes, and no fail_on line while stopping.
        A second close() does nothing; nor does --keep-open."""
        if self not in _open:
            return
        _open.remove(self)
        if self.keep_open:
            return
        judged = [(name, run) for name, run in self._runs.items()
                  if not (run.died or run.stopping or run.finished)]
        self.stopping = True
        self.backend.stop_all(self.names, self.sigint_timeout)
        if not check:
            return
        unclean = [f"{name}: {status}" for name, run in judged
                   for status in [self.backend.status(name)]
                   if not run.finished and not self._clean(status, run)]
        unclean += [f"while stopping: {what}" for what, late in self.failed_on if late]
        if unclean:
            pytest.fail("did not shut down cleanly:\n" + "\n".join(unclean), pytrace=False)

    def _abort(self, why):
        """A test failed: stop everything now, without checks."""
        self._aborted = why
        self.close(check=False)

    def _name(self, name):
        return name if ":" in name else f"{name}:{name}"

    @staticmethod
    def _clean(status, run):
        return status.stopped in (None, "SIGINT") and _exit_code(status) in run.stop_codes

    def _on_exit(self, name, status, run):  # a watcher thread
        if run.exits is not None and _exit_code(status) in run.exits:
            run.finished = True
        elif not (self.stopping or run.stopping):
            run.died = True
            self.died.append((name, status, run))
        run.exited.set()

    def wait_for_exit(self, name, timeout=10.0) -> PaneStatus:
        """Wait for a process started with 'exits' to finish; returns its
        PaneStatus. Fails if it is still running after 'timeout' s, or
        exited with a code not in 'exits'."""
        name = self._name(name)
        run = self._runs[name]
        if run.exits is None:
            raise ValueError(f"{name} was not started with exits=: its exit would be a death")
        if not run.exited.wait(timeout):
            raise AssertionError(f"{name} still running after {timeout}s; last output:\n    "
                                 + self._tail(name))
        self.check()  # an exit code not in 'exits' is a death
        return self.backend.status(name)

    def _on_line(self, name, patterns):
        if not patterns:
            return None

        def on_line(line):  # a reader thread
            for check in patterns:
                if callable(check):
                    problem = check(line)
                    what = problem and f"{name}: {problem}"
                else:
                    what = check.search(line) and f"{name} printed {line!r} (fail_on {check.pattern!r})"
                if what:
                    what += f" (log: {self.log(name)})"
                    self.failed_on.append((what, self.stopping or self._runs[name].stopping))
        return on_line

    def wait_for(self, name, pattern, timeout=10.0) -> "re.Match":
        """Wait for the process to print a line matching 'pattern' (a regex,
        searched in plain text); what it printed already counts. Fails the
        test if it doesn't within 'timeout' s, or exits first. Name "*":
        any process (checking health while it waits)."""
        if name == "*":
            regex = re.compile(pattern)
            return self.wait_until(lambda: next((m for n in self.names for line in self.backend.lines(n)
                                                 for m in [regex.search(line)] if m), None),
                                   timeout, poll=0.1, what=f"a line matching {pattern!r}")
        name = self._name(name)
        regex = re.compile(pattern)
        try:
            return self.backend.wait_for(name, regex, timeout)
        except TimeoutError:
            why = f"printed no line matching {pattern!r} within {timeout}s"
        except PaneExited as exc:
            why = f"{exc.status.exit_status} before printing a line matching {pattern!r}"
        raise AssertionError(f"{name} {why}; last output:\n    " + self._tail(name))

    def wait_for_in_order(self, name, patterns, timeout=10.0) -> list:
        """Wait for lines matching each of 'patterns', in that order, each on
        a later line than the one before; returns the re.Matches. Fails
        after 'timeout' s (0: check now), naming the first pattern missing."""
        name = self._name(name)
        regexes = [re.compile(p) for p in patterns]
        found = []

        def matched():
            found.clear()
            lines = iter(self.backend.lines(name))
            for regex in regexes:
                m = next((m for line in lines for m in [regex.search(line)] if m), None)
                if m is None:
                    return False
                found.append(m)
            return True

        deadline = time.monotonic() + timeout
        while not matched():
            if time.monotonic() >= deadline:
                after = f" after {found[-1].group(0)!r}" if found else ""
                raise AssertionError(f"{name}: no line matching {patterns[len(found)]!r}{after} "
                                     f"within {timeout}s; last output:\n    " + self._tail(name))
            self.check()
            time.sleep(0.1)
        return list(found)

    def wait_until(self, predicate, timeout=30.0, poll=0.2, what=None):
        """Call predicate() until it returns something true, and return
        that; check() between calls, so a death fails now, not at the
        timeout. Fails if 'timeout' s pass first."""
        deadline = time.monotonic() + timeout
        while True:
            self.check()
            result = predicate()
            if result:
                return result
            if time.monotonic() >= deadline:
                raise AssertionError(f"timed out after {timeout}s waiting for "
                                     f"{what or getattr(predicate, '__name__', 'a condition')}")
            time.sleep(poll)

    def status(self, name) -> PaneStatus:
        return self.backend.status(self._name(name))

    def log(self, name):
        """The file the process's output goes to, as plain text."""
        return os.path.join(self.log_dir, re.sub(r"[^A-Za-z0-9_.-]", ".", self._name(name)) + ".log")

    def output(self, name) -> str:
        """Everything the process has printed so far (its latest start)."""
        return self.backend.output(self._name(name))

    def _tail(self, name, lines=8):
        return "\n    ".join(self.output(name).strip().splitlines()[-lines:])

    def check(self):
        """Raise AssertionError if anything died, or printed a fail_on line,
        since the last check(). Each problem fails one test: the one it
        happened in. Runs after every test; call it while waiting to fail
        fast."""
        died = self.died[self._raised_died:]
        failed_on = self.failed_on[self._raised_failed_on:]
        self._raised_died, self._raised_failed_on = len(self.died), len(self.failed_on)
        problems = [f"{name} {status.exit_status}" + ("" if run.exits is None else
                                                       f" (expected exit {', '.join(map(str, run.exits))})")
                    for name, status, run in died]
        problems += [what for what, late in failed_on if not late]
        for name, _, _ in died:
            problems.append(f"  last output of {name} ({self.log(name)}):\n    " + self._tail(name))
        if problems:
            raise AssertionError("launched process died early:\n" + "\n".join(problems))


_open = []  # every Processes not closed yet: what check_health() and the hooks check
_tmux_backends = {}  # test class (or test) -> the _TmuxBackend its groups share


def check_health():
    """Raise AssertionError if any open Processes saw a death. For helpers
    deep in test code that weren't handed one, e.g. a polling loop."""
    for procs in list(_open):
        procs.check()


@pytest.fixture(scope="session")
def lte_pane_setup():
    """A shell command run before each process. None: nothing."""
    return None


_plugin = {}  # what Processes() needs outside a fixture; see _lte_plugin


@pytest.fixture(scope="session", autouse=True)
def _lte_plugin(request, tmp_path_factory, lte_pane_setup):
    """Before any setup_class: it can't take fixtures."""
    _plugin.update(config=request.config, tmp_path_factory=tmp_path_factory,
                   setup=lte_pane_setup)
    yield
    _plugin.clear()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Fail the test itself, not just teardown, if a process died while it
    ran, or one of its own groups doesn't close cleanly. (A failed test's
    are stopped by pytest_runtest_makereport.) Processes() in the test is
    owned by it."""
    _plugin["item"] = item
    try:
        outcome = yield
    finally:
        _plugin.pop("item", None)
    if outcome.excinfo is None:
        try:
            check_health()
            for procs in [p for p in _open if p.owner is item]:
                procs.close()
        except (AssertionError, pytest.fail.Exception) as exc:
            outcome.force_exception(exc)


_aborted = {}  # test class -> why its processes were stopped


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """A failed test (or setup) stops its class's and its own processes at
    once, each group's on_abort() first. Not with --keep-open."""
    report = (yield).get_result()
    if not report.failed or call.when == "teardown" or item.config.lte_keep_open:
        return
    why = f"{item.name} failed"
    for procs in [p for p in _open if p.owner in (item, item.cls)]:
        if procs.on_abort is not None:
            try:
                procs.on_abort()
            except Exception as exc:  # noqa: BLE001 -- still stop them below
                item.warn(pytest.PytestWarning(f"on_abort() raised {exc!r}"))
        procs._abort(why)
        if procs.owner is not item:
            _aborted[procs.owner] = why


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Skip what is left of a class whose processes were aborted."""
    if item.cls in _aborted:
        pytest.skip(f"processes stopped: {_aborted[item.cls]}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """After a class's last test: stop a group its teardown_class didn't
    close, e.g. because setup_class failed (pytest then skips the teardown)."""
    yield
    for procs in [p for p in _open if p.owner is item]:  # e.g. the test never ran
        procs.close(check=False)
    if nextitem is None or nextitem.cls is not item.cls:
        for procs in [p for p in _open if p.owner is item.cls]:
            procs.close(check=False)


def parametrize(argnames, argvalues):
    """Class decorator: run the whole class -- setup_class, its tests,
    teardown_class -- once per value, like launch_testing.parametrize.
    Each value is a class attribute, e.g. cls.rate in setup_class:

        @parametrize("rate", [5, 10])
        class TestRover: ...           # TestRover[rate=5], TestRover[rate=10]

    'argnames' may name several ("a, b", values as tuples); stacked
    decorators multiply."""
    names = [n.strip() for n in argnames.split(",") if n.strip()]

    def decorate(cls):
        sets = [tuple(v) if len(names) > 1 else (v,) for v in argvalues]
        before = cls.__dict__.get("_lte_params", [{}])
        cls._lte_params = [dict(b, **dict(zip(names, values))) for b in before for values in sets]
        return cls
    return decorate


@pytest.hookimpl(tryfirst=True)
def pytest_pycollect_makeitem(collector, name, obj):
    params = isinstance(obj, type) and obj.__dict__.get("_lte_params")
    if not params or not collector.istestclass(obj, name):
        return None
    items = []
    for values in params:
        label = ",".join(f"{k}={v}" for k, v in values.items())
        safe = re.sub(r"[^\w-]+", "_", f"{name}_{label}").strip("_")
        sub = type(safe, (obj,), dict(values, __module__=obj.__module__, __qualname__=safe))
        node = _ParametrizedClass.from_parent(collector, name=f"{name}[{label}]")
        node._lte_cls = sub
        items.append(node)
    return items


class _ParametrizedClass(pytest.Class):
    """A class made by parametrize(): not an attribute of its module."""

    def _getobj(self):
        return self._lte_cls
