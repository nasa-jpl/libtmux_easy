"""pytest plugin: launch the processes a test needs, watch them, stop them.

Enable it in conftest.py:

    pytest_plugins = ["libtmux_easy.testing"]

A test module says what to launch, as 'window:name' -> command:

    PROCESSES = {"servers:web": "python3 -m http.server 8000"}

    READY = {"servers:web": r"Serving HTTP"}       # optional: wait for these
    FAIL_ON = {"*": [r"Traceback", r"ERROR"]}      # optional: a match fails the test

    def test_serves(processes):
        ...                      # processes.output("servers:web"), .wait_for(), .check()

The 'processes' fixture (module scope) starts them, and waits for each
READY line (READY_TIMEOUT, default 30 s). A test fails if one exits before
teardown, or prints a FAIL_ON line ("*": every process; a regex, or a
function line -> problem or None), even if the test never noticed. Each
problem fails one test: the one running. check_health() checks now. Teardown stops them (SIGINT, then SIGTERM, SIGKILL) and
errors unless each exited 0 on SIGINT; SIGINT_TIMEOUT (default 10 s) is how
long SIGINT gets.

Or a test class brings up its own, from plain setup/teardown code:

    class TestSystem:
        @classmethod
        def setup_class(cls):
            cls.procs = new_processes(cls)   # FAIL_ON etc. from the class
            cls.procs.start("sim:server", "./server", ready=r"listening")
            cls.procs.wait_until(server_answers)   # any gate
        @classmethod
        def teardown_class(cls):
            cls.procs.close()

A test can also bring its own up and down, with the same checks:

    def test_restart(launcher):  # function scope; stops what is left after
        launcher.start("clients:c", "./client", ready=r"connected")
        launcher.stop("clients:c")   # fails unless it exits 0 on SIGINT
        launcher.start("clients:c", "./client")   # a stopped name restarts

More, for any of these:

  start(..., exits=0)          a process that finishes on its own; that exit
                               is no death. wait_for_exit(name) waits for it
  start(..., stop_codes=...)   exit codes allowed when stopped (default 0;
                               130: died of SIGINT). EXITS / STOP_CODES =
                               {name or "*": codes} on the module or class
  wait_for("*", regex)         a line from any process
  wait_for_in_order(name, [regex, ...])   lines in that order
  arg("rate", 10)              --arg rate=20 on the command line (typed
                               like the default; usable in PROCESSES)
  @parametrize("rate", [5, 10])   a class, setup to teardown, per value
                               (cls.rate)

A failed test stops its class's, module's and own processes at once
(SIGINT, escalating; no shutdown checks), and skips the rest of that
class or module. A class (or module) can define abort() to bring them down
its own way first. Not with --keep-open.

Modes (--lte-mode; default auto: dash under lte-session, else shell;
--tmux: dash, rerunning pytest under lte-session in a terminal and
attaching; --tmux --no-attach, or no terminal: tmux):

  shell  plain subprocesses; output to the terminal, prefixed '[name]'
  tmux   one wrap-mode pane each, in a detached session for the run; only
         test output in the terminal. Kept on failure (--lte-keep)
  dash   under lte-session: the panes join its session and dashboard. Kept
         to inspect with 'lte-session --keep'

Each process's output, as plain text, also goes to a log file under
pytest's temp dir: '<module>N/<window>.<name>.log' for 'processes', the
test's tmp_path for 'launcher'. The summary prints where. Each process
gets LTE_LOG_DIR, its log's directory.

--keep-open (tmux modes; implies --tmux): processes are never stopped, and
the session or windows stay up, to look at after the run.

Override these fixtures in conftest.py to adapt it:

  lte_pane_setup     a shell command run before each process, e.g. to
                     source an environment (panes start a fresh login shell)
  lte_output_checks  callables (name, output) -> [problem, ...], for deaths
                     only the output shows, e.g. a child of a launcher

Also 'tmux_session' (function scope, tmux only): a TmuxSession for the
test to fill itself; under lte-session its windows are kept, renamed
'<test>/<window>', unless --lte-close-windows.

Two runs at once don't clash: tmux sessions are 'lte_test_<pid>[-N]', never
killing one that exists, and windows '<prefix>#N/...' if '<prefix>/...'
is taken (another run in lte-session's session, or kept windows).
"""

import contextlib
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

MODES = ("shell", "tmux", "dash")


def pytest_addoption(parser):
    group = parser.getgroup("libtmux_easy")
    group.addoption("--lte-mode", choices=("auto",) + MODES, default="auto",
                    help="where 'processes' run (default: auto)")
    group.addoption("--tmux", action="store_true",
                    help="run 'processes' in tmux and attach: pytest reruns under lte-session, "
                         "in the dashboard's home pane")
    group.addoption("--no-attach", action="store_true",
                    help="with --tmux: a detached tmux session instead (tmux attach to watch)")
    group.addoption("--keep-open", action="store_true",
                    help="never stop processes; leave the tmux session up (implies --tmux)")
    group.addoption("--lte-keep", choices=("failure", "always", "never"), default="failure",
                    help="tmux mode: when to leave the session up after the run")
    group.addoption("--arg", action="append", default=[], metavar="NAME=VALUE",
                    help="a value for the tests to read with arg(NAME), like launch_test's "
                         "NAME:=VALUE (repeatable)")
    group.addoption("--lte-close-windows", action="store_true",
                    help="close each test's tmux windows after it, even under lte-session")


def under_lte_session() -> bool:
    """True inside a session with a dashboard, e.g. lte-session's."""
    pane = os.environ.get("TMUX_PANE")
    return bool(pane) and bool(option(pane, dash.WINDOW_OPTION))


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    """--tmux in a terminal: rerun this pytest under lte-session, as the
    dashboard's home pane, and attach. Its exit status is pytest's."""
    wants_tmux = config.getoption("tmux") or config.getoption("keep_open")
    if (not wants_tmux or config.getoption("no_attach") or config.getoption("lte_mode") != "auto"
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
    mode = config.getoption("lte_mode")
    keep_open = config.getoption("keep_open")
    if mode == "auto":
        tmux = config.getoption("tmux") or keep_open
        mode = "dash" if under_lte_session() else "tmux" if tmux else "shell"
    elif mode == "dash" and not under_lte_session():
        raise pytest.UsageError("--lte-mode=dash needs lte-session: lte-session -- pytest ...")
    if mode == "shell" and keep_open:
        raise pytest.UsageError("--keep-open needs tmux: subprocesses can't outlive pytest")
    if mode == "tmux" and not shutil.which("tmux"):
        raise pytest.UsageError("--lte-mode=tmux: tmux is not installed")
    config.lte_mode = mode
    config.lte_keep_open = keep_open
    config.lte_session = None  # the run's TmuxSession, made on first use
    config.lte_session_name = (_free_session_name(f"lte_test_{os.getpid()}")
                               if mode == "tmux" else None)
    config.lte_kept = False
    config.lte_log_dirs = []
    # The real terminal, for shell mode's output: pytest captures fd 1 during
    # tests, but not here.
    config.lte_tty = os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1)


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
    if config.lte_mode == "tmux":
        return f"lte mode: tmux (watch: tmux attach -t {config.lte_session_name})"
    return f"lte mode: {config.lte_mode}"


def _run_session(config):
    # Processes get pytest's environment, as in shell mode: the tmux server's
    # may predate an activated venv or a sourced workspace.
    if config.lte_session is None:
        config.lte_session = (TmuxSession.current(env=os.environ) if config.lte_mode == "dash"
                              else TmuxSession.create(config.lte_session_name, kill_existing=False,
                                                   env=os.environ))
    return config.lte_session


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    for procs in list(_open):  # never closed: don't leave them running
        _open.remove(procs)
        if not procs.keep_open:
            procs.shutdown()
    tmux = getattr(config, "lte_session", None)
    if tmux is None:
        return
    keep = "always" if config.lte_keep_open else config.getoption("lte_keep")
    if config.lte_mode == "dash" or keep == "always" or (keep == "failure" and exitstatus):
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
    import time, e.g. in PROCESSES."""
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
    # Each a directory of pytest's basetemp: name that.
    for base in sorted({os.path.dirname(d) for d in getattr(config, "lte_log_dirs", ())}):
        terminalreporter.write_line(f"process logs: {base}")
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
    '<prefix>/<window>' so fixtures sharing the session don't clash. A
    restart gets a new pane, '<name>~N', next to the old one's output."""

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
        if window not in self.windows:
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

    def stop_all(self, sigint_timeout):
        _parallel(self.stop, [n for n, p in self.panes.items() if self.session.is_running(p)],
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
        self.proc = subprocess.Popen(
            ["bash", "-c", command], stdin=stdin, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace",
            start_new_session=True,  # a process group of its own, for SIGINT
            env=dict(os.environ, PYTHONUNBUFFERED="1",
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


class _ShellBackend:
    """Plain subprocesses, their output in the terminal. A restart replaces
    the old one."""

    def __init__(self, tty):
        self.tty = tty
        self.lock = threading.Lock()
        self.procs = {}

    def start(self, name, command, setup, on_exit, on_line=None, log=None):
        if setup:
            command = f"{setup} && {command}"
        self.procs[name] = _Subprocess(name, command, on_exit, self.tty, self.lock, on_line, log)

    def wait_for(self, name, regex, timeout):
        return self.procs[name].wait_for(regex, timeout)

    def output(self, name):
        return "".join(self.procs[name].lines)

    def lines(self, name):
        return list(self.procs[name].plain)

    def stop(self, name, sigint_timeout):
        self.procs[name].stop(sigint_timeout)

    def stop_all(self, sigint_timeout):
        _parallel(self.stop, list(self.procs), sigint_timeout)

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


class Processes:
    """Processes under test and their health, whatever the mode.

    The 'processes' fixture fills one from the module's PROCESSES; the
    'launcher' fixture gives a test an empty one to start() and stop()."""

    def __init__(self, backend, commands=None, setup=None, checks=(), fail_on=None,
                 log_dir=None, ready_timeout=30.0, sigint_timeout=10.0, keep_open=False,
                 owner=None, exits=None, stop_codes=None):
        self.backend = backend
        self.keep_open = keep_open
        self.owner = owner  # the class or module whose setup made it, for new_processes()
        self.setup = setup
        self.names = []
        self.checks = list(checks)
        self.died = []  # (name, PaneStatus, _Run) of each exit nobody asked for
        self.failed_on = []  # (problem, during a stop) of each FAIL_ON hit
        self.stopping = False
        self.log_dir = log_dir
        self.ready_timeout = ready_timeout
        self.sigint_timeout = sigint_timeout
        self.fail_on = {name: [p] if isinstance(p, str) or callable(p) else list(p)
                        for name, p in (fail_on or {}).items()}
        self.exits = dict(exits or {})  # name -> codes it may finish with (EXITS)
        self.stop_codes = dict(stop_codes or {})  # name or "*" -> codes when stopped (STOP_CODES)
        self.aborted = None  # why abort() stopped everything
        self.fixture = None  # the fixture that made it, if one did
        self._runs = {}  # name -> _Run of its latest start
        self._reported = set()  # output-check problems check() raised
        self._raised_died = self._raised_failed_on = 0  # how many check() raised
        _open.append(self)
        for name, command in (commands or {}).items():
            self.start(name, command)

    def start(self, name, command, ready=None, timeout=None, fail_on=(), exits=None,
              stop_codes=None) -> None:
        """Start 'command' as 'window:name' (a name without ':' goes in a
        window of its own). With 'ready' (a regex), wait for a line matching
        it, as wait_for() does. 'fail_on': more FAIL_ON patterns for it.
        'exits': a code (or several) it finishes with on its own, so that
        exit is no death; wait_for_exit() waits for it. 'stop_codes': the
        codes it may exit with when stopped (default 0; a signal N is
        128+N, so 130 allows dying of SIGINT). A stopped name can be
        started again."""
        if name in self._runs and self.status(name).running:
            raise ValueError(f"{name} is already running")
        if ":" not in name:
            name = f"{name}:{name}"
        patterns = self.fail_on.get("*", []) + self.fail_on.get(name, [])
        patterns += [fail_on] if isinstance(fail_on, str) or callable(fail_on) else list(fail_on)
        if exits is None:
            exits = self.exits.get(name)
        if stop_codes is None:
            stop_codes = self.stop_codes.get(name, self.stop_codes.get("*", 0))
        run = self._runs[name] = _Run(_codes(exits), _codes(stop_codes))
        if name not in self.names:
            self.names.append(name)
        self.backend.start(name, command, self.setup,
                           lambda n, status: self._on_exit(n, status, run),
                           self._on_line(name, [p if callable(p) else re.compile(p) for p in patterns]),
                           self.log(name))
        if ready is not None:
            self.wait_for(name, ready, self.ready_timeout if timeout is None else timeout)

    def stop(self, name, sigint_timeout=None, check=True):
        """Stop it (SIGINT, then SIGTERM, SIGKILL); its exit is no death.
        With 'check', fail unless SIGINT stopped it, with one of its
        stop_codes (default 0). Returns its PaneStatus."""
        name = self._name(name)
        run = self._runs[name]
        run.stopping = True
        self.backend.stop(name, self.sigint_timeout if sigint_timeout is None else sigint_timeout)
        run.exited.wait(5)
        status = self.backend.status(name)
        if check and not run.finished and not self._clean(status, run):
            raise AssertionError(f"{name} did not stop cleanly: {status}\n    " + self._tail(name))
        return status

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
                    what = check.search(line) and f"{name} printed {line!r} (FAIL_ON {check.pattern!r})"
                if what:
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

    def status(self, name) -> PaneStatus:
        return self.backend.status(self._name(name))

    def log(self, name):
        """The file the process's output goes to, as plain text; None: none."""
        if self.log_dir is None:
            return None
        return os.path.join(self.log_dir, re.sub(r"[^A-Za-z0-9_.-]", ".", self._name(name)) + ".log")

    def output(self, name) -> str:
        """Everything the process has printed so far (its latest start)."""
        return self.backend.output(self._name(name))

    def _tail(self, name, lines=8):
        return "\n    ".join(self.output(name).strip().splitlines()[-lines:])

    def _check_output(self):
        return [problem for name in self.names for check in self.checks
                for problem in check(name, self.output(name))]

    def problems(self, new_only=False) -> list:
        """Deaths, FAIL_ON hits and output-check problems; 'new_only': those
        check() hasn't raised yet."""
        died = self.died[self._raised_died:] if new_only else self.died
        failed_on = self.failed_on[self._raised_failed_on:] if new_only else self.failed_on
        found = [f"{name} {status.exit_status}" + ("" if run.exits is None else
                                                    f" (expected exit {', '.join(map(str, run.exits))})")
                 for name, status, run in died]
        found += [what for what, late in failed_on if not late]
        found += [p for p in self._check_output() if not (new_only and p in self._reported)]
        for name, _, _ in died:
            found.append(f"  last output of {name} ({self.log(name)}):\n    " + self._tail(name))
        return found

    def check(self):
        """Raise AssertionError if anything died since the last check() that
        did. Each problem fails one test: the one it happened in. Runs
        after every test; call it while waiting to fail fast."""
        died, failed_on = len(self.died), len(self.failed_on)
        problems = self.problems(new_only=True)
        self._raised_died, self._raised_failed_on = died, failed_on
        if problems:
            self._reported.update(problems)
            raise AssertionError("launched process died early:\n" + "\n".join(problems))

    def shutdown(self, sigint_timeout=None) -> list:
        """Stop everything; return what did not shut down cleanly: needed
        more than SIGINT, exited with a code not in its stop_codes, or failed
        an output check. A death, a stop(), or an expected exit was reported
        already."""
        judged = [(name, run) for name, run in self._runs.items()
                  if not (run.died or run.stopping or run.finished)]
        self.stopping = True
        self.backend.stop_all(self.sigint_timeout if sigint_timeout is None else sigint_timeout)
        unclean = [f"{name}: {status}" for name, run in judged
                   for status in [self.backend.status(name)]
                   if not run.finished and not self._clean(status, run)]
        unclean += [f"while stopping: {what}" for what, late in self.failed_on if late]
        return unclean + [p for p in self._check_output() if p not in self._reported]

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

    def abort(self, why="aborted"):
        """Stop everything now (SIGINT, then SIGTERM, SIGKILL), without the
        shutdown checks; later close() calls do nothing. The plugin calls it
        when a test fails, unless the owner defines abort() (see below)."""
        if self in _open:
            _open.remove(self)
        self.aborted = why
        if not self.keep_open:
            self.stopping = True
            self.backend.stop_all(self.sigint_timeout)

    def close(self):
        """Stop what is still running (not with --keep-open) and fail if
        anything did not stop cleanly. Fixtures call it after their tests;
        call it from teardown_class / teardown_module for new_processes()."""
        if self not in _open:
            return
        _open.remove(self)
        if self.keep_open:
            return
        unclean = self.shutdown()
        if unclean:
            pytest.fail("did not shut down cleanly:\n" + "\n".join(unclean), pytrace=False)


_open = []  # every Processes not closed yet: what check_health() and the hooks check


def check_health():
    """Raise AssertionError if any running Processes saw a death. For
    helpers deep in test code that weren't handed one, e.g. a polling loop."""
    for procs in list(_open):
        procs.check()


@pytest.fixture(scope="session")
def lte_pane_setup():
    """A shell command run before each process. None: nothing."""
    return None


@pytest.fixture(scope="session")
def lte_output_checks():
    """Callables (name, output) -> list of problems."""
    return []


_plugin = {}  # what new_processes() needs outside a fixture; see _lte_plugin


@pytest.fixture(scope="session", autouse=True)
def _lte_plugin(request, tmp_path_factory, lte_pane_setup, lte_output_checks):
    """Before any setup_class / setup_module: they can't take fixtures."""
    _plugin.update(config=request.config, tmp_path_factory=tmp_path_factory,
                   setup=lte_pane_setup, checks=lte_output_checks)
    yield
    _plugin.clear()


def _module_name(module):
    return module.__name__.rpartition(".")[2]


def _make_processes(sources, prefix, log_dir, commands=None, owner=None):
    """A Processes for the run's mode; FAIL_ON, READY_TIMEOUT,
    SIGINT_TIMEOUT, EXITS and STOP_CODES from the first of 'sources'
    (class, module) that has it."""
    config = _plugin["config"]
    attr = lambda name, default: next((getattr(o, name) for o in sources if hasattr(o, name)),
                                      default)
    if config.lte_mode == "shell":
        backend = _ShellBackend(config.lte_tty)
    else:
        backend = _TmuxBackend(_run_session(config), prefix)
    config.lte_log_dirs.append(str(log_dir))
    return Processes(backend, commands, _plugin["setup"], _plugin["checks"],
                     attr("FAIL_ON", None), str(log_dir), attr("READY_TIMEOUT", 30.0),
                     attr("SIGINT_TIMEOUT", 10.0), config.lte_keep_open, owner,
                     attr("EXITS", None), attr("STOP_CODES", None))


def new_processes(owner) -> Processes:
    """An empty Processes for setup_class / setup_module to start() into;
    close() it in teardown_class / teardown_module. 'owner' is the class
    (or module): its FAIL_ON, READY_TIMEOUT, SIGINT_TIMEOUT apply, then its
    module's. Logs go to a tmp dir named after it. Every test checks it,
    as for the fixtures; if setup fails, it is stopped when the class ends."""
    if not _plugin:
        raise RuntimeError("new_processes() needs the plugin: "
                           'pytest_plugins = ["libtmux_easy.testing"] in conftest.py')
    if isinstance(owner, type):
        module = sys.modules[owner.__module__]
        sources, prefix = (owner, module), f"{_module_name(module)}.{owner.__name__}"
    else:
        sources, prefix = (owner,), _module_name(owner)
    return _make_processes(sources, prefix, _plugin["tmp_path_factory"].mktemp(prefix),
                           owner=owner)


@pytest.fixture(scope="module")
def processes(request, tmp_path_factory):
    """The module's PROCESSES, started and READY; stopped after its last test."""
    commands = getattr(request.module, "PROCESSES", None)
    if not commands:
        raise pytest.UsageError(f"{request.module.__name__}: no PROCESSES to launch")
    module = _module_name(request.module)
    procs = _make_processes((request.module,), module, tmp_path_factory.mktemp(module), commands,
                            owner=request.module)
    procs.fixture = "processes"
    try:
        for name, pattern in getattr(request.module, "READY", {}).items():
            procs.wait_for(name, pattern, procs.ready_timeout)
    except AssertionError as exc:
        _open.remove(procs)
        if not procs.keep_open:
            procs.shutdown()
        pytest.fail(f"not ready: {exc}", pytrace=False)
    yield procs
    procs.close()


@pytest.fixture
def launcher(request, tmp_path):
    """An empty Processes for the test to start() and stop() its own; what
    is left running is stopped after the test. Logs go to tmp_path."""
    prefix = re.sub(r"[^\w-]", "_", f"{_module_name(request.module)}.{request.node.name}")
    procs = _make_processes((request.cls, request.module) if request.cls else (request.module,),
                            prefix, tmp_path, owner=request.node)
    yield procs
    procs.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Fail the test itself, not just teardown, if a process died while it ran."""
    outcome = yield
    if outcome.excinfo is None:
        try:
            check_health()
        except AssertionError as exc:
            outcome.force_exception(exc)


_aborted = {}  # owner (class or module) -> (why, the fixture that made its Processes)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """A failed test (or setup) stops its class's, module's and own
    processes at once: the owner's abort() if it defines one, then
    Processes.abort(). Not with --keep-open."""
    report = (yield).get_result()
    if not report.failed or call.when == "teardown" or item.config.lte_keep_open:
        return
    groups = [p for p in _open if p.owner in (item, item.cls, item.module)]
    why = f"{item.name} failed"
    for owner in dict.fromkeys(p.owner for p in groups):
        custom = None if owner is item else getattr(owner, "abort", None)
        if callable(custom):
            try:
                custom()
            except Exception as exc:  # noqa: BLE001 -- still stop them below
                item.warn(pytest.PytestWarning(f"{owner.__name__}.abort() raised {exc!r}"))
    for procs in groups:
        procs.abort(why)
        if procs.owner is not item:
            _aborted[procs.owner] = (why, procs.fixture)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Skip what is left of a class or module whose processes were aborted."""
    for owner in (item.cls, item.module):
        if owner in _aborted:
            why, fixture = _aborted[owner]
            if fixture is None or fixture in item.fixturenames:
                pytest.skip(f"processes stopped: {why}")


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


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """After a class's (or module's) last test: stop a new_processes() its
    teardown didn't close, e.g. because its setup failed (pytest then skips
    the teardown)."""
    yield
    for procs in [p for p in _open if p.owner in (item.cls, item.module)]:
        if nextitem is None or procs.owner not in (nextitem.cls, nextitem.module):
            _open.remove(procs)
            if not procs.keep_open:
                procs.shutdown()


@pytest.fixture
def tmux_session(request):
    if os.environ.get("TMUX_PANE"):
        session = TmuxSession.current(env=os.environ)
    else:
        name = re.sub(r"[^A-Za-z0-9_-]", "_", f"lte_test_{os.getpid()}_{request.node.name}")
        session = TmuxSession.create(name, kill_existing=False, env=os.environ)
    try:
        yield session
    finally:
        keep_open = request.config.lte_keep_open
        if not keep_open:
            session.stop_all()
        keep = keep_open or (session._adopted and session._dash_window is not None
                             and not request.config.getoption("lte_close_windows"))
        if keep:
            session.release(keep_windows=True, label=request.node.name)
        else:
            session.kill_session()
