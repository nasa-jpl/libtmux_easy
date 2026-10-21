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
teardown, or prints a FAIL_ON line ("*": every process), even if the test
never noticed. Teardown stops them (SIGINT, then SIGTERM, SIGKILL) and
errors unless each exited 0 on SIGINT; SIGINT_TIMEOUT (default 10 s) is how
long SIGINT gets.

A test can also bring its own up and down, with the same checks:

    def test_restart(launcher):  # function scope; stops what is left after
        launcher.start("clients:c", "./client", ready=r"connected")
        launcher.stop("clients:c")   # fails unless it exits 0 on SIGINT
        launcher.start("clients:c", "./client")   # a stopped name restarts

Modes (--lte-mode; default auto: dash under lte-session, else shell;
--tmux: dash under lte-session, else tmux):

  shell  plain subprocesses; output to the terminal, prefixed '[name]'
  tmux   one wrap-mode pane each, in a detached session for the run; only
         test output in the terminal. Kept on failure (--lte-keep)
  dash   under lte-session: the panes join its session and dashboard. Kept
         to inspect with 'lte-session --keep'

Each process's output, as plain text, also goes to a log file under
pytest's temp dir: '<module>N/<window>.<name>.log' for 'processes', the
test's tmp_path for 'launcher'. The summary prints where.

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
                    help="run 'processes' in tmux: --lte-mode=dash under lte-session, else tmux")
    group.addoption("--keep-open", action="store_true",
                    help="never stop processes; leave the tmux session up (implies --tmux)")
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
    tmux = getattr(config, "lte_session", None)
    if tmux is None:
        return
    keep = "always" if config.lte_keep_open else config.getoption("lte_keep")
    if config.lte_mode == "dash" or keep == "always" or (keep == "failure" and exitstatus):
        tmux.release(keep_windows=True)
        config.lte_kept = config.lte_mode == "tmux"
    else:
        tmux.kill_session()


def pytest_terminal_summary(terminalreporter, config):
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
        on_output = {regex: (lambda m, r=regex: on_line(r, m.line)) for regex in on_line.patterns} \
            if on_line else None
        self.panes[name] = self.session.new_pane(pane_name, command, mode=PANE_MODE_WRAP,
                                                 on_exit=lambda s: on_exit(name, s),
                                                 on_output=on_output, tee=log)

    def wait_for(self, name, regex, timeout):
        """The first re.Match in the output; TimeoutError, or PaneExited."""
        return self.session.wait_for_output(self.panes[name], regex, timeout).match

    def output(self, name):
        return "\n".join(_pane_cmd(self.panes[name], "capture-pane", "-p", "-J", "-S", "-").stdout)

    def stop(self, name, sigint_timeout):
        self.session.stop_pane(self.panes[name], sigint_timeout=sigint_timeout)

    def stop_all(self, sigint_timeout):
        _parallel(self.stop, [n for n, p in self.panes.items() if self.session.is_running(p)],
                  sigint_timeout)

    def status(self, name):
        return self.session.pane_status(self.panes[name])


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
            env=dict(os.environ, PYTHONUNBUFFERED="1"))
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
                for regex in (on_line.patterns if on_line else ()):
                    if regex.search(plain):
                        on_line(regex, plain)
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

    def stop(self, name, sigint_timeout):
        self.procs[name].stop(sigint_timeout)

    def stop_all(self, sigint_timeout):
        _parallel(self.stop, list(self.procs), sigint_timeout)

    def status(self, name):
        return self.procs[name].status()


# ---------- processes ----------

class _Run:
    """One start of a process: whether its exit is expected, and when it came."""

    def __init__(self):
        self.stopping = False
        self.died = False
        self.exited = threading.Event()


class Processes:
    """Processes under test and their health, whatever the mode.

    The 'processes' fixture fills one from the module's PROCESSES; the
    'launcher' fixture gives a test an empty one to start() and stop()."""

    def __init__(self, backend, commands=None, setup=None, checks=(), fail_on=None,
                 log_dir=None, ready_timeout=30.0, sigint_timeout=10.0):
        self.backend = backend
        self.setup = setup
        self.names = []
        self.checks = list(checks)
        self.died = []  # (name, PaneStatus) of each exit nobody asked for
        self.failed_on = []  # (name, pattern, line, during a stop) of FAIL_ON matches
        self.stopping = False
        self.log_dir = log_dir
        self.ready_timeout = ready_timeout
        self.sigint_timeout = sigint_timeout
        self.fail_on = {name: [p] if isinstance(p, str) else list(p)
                        for name, p in (fail_on or {}).items()}
        self._runs = {}  # name -> _Run of its latest start
        self._reported = set()  # output-check problems check() raised
        for name, command in (commands or {}).items():
            self.start(name, command)

    def start(self, name, command, ready=None, timeout=None, fail_on=()) -> None:
        """Start 'command' as 'window:name' (a name without ':' goes in a
        window of its own). With 'ready' (a regex), wait for a line matching
        it, as wait_for() does. 'fail_on': more FAIL_ON patterns for it.
        A stopped name can be started again."""
        if name in self._runs and self.status(name).running:
            raise ValueError(f"{name} is already running")
        if ":" not in name:
            name = f"{name}:{name}"
        patterns = self.fail_on.get("*", []) + self.fail_on.get(name, [])
        patterns += [fail_on] if isinstance(fail_on, str) else list(fail_on)
        run = self._runs[name] = _Run()
        if name not in self.names:
            self.names.append(name)
        self.backend.start(name, command, self.setup,
                           lambda n, status: self._on_exit(n, status, run),
                           self._on_line(name, [re.compile(p) for p in patterns]), self.log(name))
        if ready is not None:
            self.wait_for(name, ready, self.ready_timeout if timeout is None else timeout)

    def stop(self, name, sigint_timeout=None, check=True):
        """Stop it (SIGINT, then SIGTERM, SIGKILL); its exit is no death.
        With 'check', fail unless it exited 0 on SIGINT. Returns its PaneStatus."""
        name = self._name(name)
        self._runs[name].stopping = True
        self.backend.stop(name, self.sigint_timeout if sigint_timeout is None else sigint_timeout)
        self._runs[name].exited.wait(5)
        status = self.backend.status(name)
        if check and not self._clean(status):
            raise AssertionError(f"{name} did not stop cleanly: {status}\n    " + self._tail(name))
        return status

    def _name(self, name):
        return name if ":" in name else f"{name}:{name}"

    @staticmethod
    def _clean(status):
        return status.stopped in (None, "SIGINT") and status.exit_status.ok

    def _on_exit(self, name, status, run):  # a watcher thread
        if not (self.stopping or run.stopping):
            run.died = True
            self.died.append((name, status))
        run.exited.set()

    def _on_line(self, name, patterns):
        if not patterns:
            return None

        def on_line(regex, line):  # a reader thread
            late = self.stopping or self._runs[name].stopping
            self.failed_on.append((name, regex.pattern, line, late))
        on_line.patterns = patterns
        return on_line

    def wait_for(self, name, pattern, timeout=10.0) -> "re.Match":
        """Wait for the process to print a line matching 'pattern' (a regex,
        searched in plain text); what it printed already counts. Fails the
        test if it doesn't within 'timeout' s, or exits first."""
        name = self._name(name)
        regex = re.compile(pattern)
        try:
            return self.backend.wait_for(name, regex, timeout)
        except TimeoutError:
            why = f"printed no line matching {pattern!r} within {timeout}s"
        except PaneExited as exc:
            why = f"{exc.status.exit_status} before printing a line matching {pattern!r}"
        raise AssertionError(f"{name} {why}; last output:\n    " + self._tail(name))

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

    def problems(self) -> list:
        found = [f"{name} {status.exit_status}" for name, status in self.died]
        found += [f"{name} printed {line!r} (FAIL_ON {pattern!r})"
                  for name, pattern, line, late in self.failed_on if not late]
        found += self._check_output()
        for name, _ in self.died:
            found.append(f"  last output of {name} ({self.log(name)}):\n    " + self._tail(name))
        return found

    def check(self):
        """Raise AssertionError if anything died. Runs after every test that
        uses 'processes' or 'launcher'; call it while waiting to fail fast."""
        problems = self.problems()
        if problems:
            self._reported.update(problems)
            raise AssertionError("launched process died early:\n" + "\n".join(problems))

    def shutdown(self, sigint_timeout=None) -> list:
        """Stop everything; return what did not shut down cleanly: needed
        more than SIGINT, exited non-zero, or failed an output check. A
        death, or a stop(), was reported already."""
        judged = [name for name, run in self._runs.items() if not (run.died or run.stopping)]
        self.stopping = True
        self.backend.stop_all(self.sigint_timeout if sigint_timeout is None else sigint_timeout)
        unclean = [f"{name}: {status}" for name in judged
                   for status in [self.backend.status(name)] if not self._clean(status)]
        unclean += [f"{name} printed {line!r} while stopping (FAIL_ON {pattern!r})"
                    for name, pattern, line, late in self.failed_on if late]
        return unclean + [p for p in self._check_output() if p not in self._reported]

    def finish(self, keep_open=False):
        """Fixture teardown: shut down, failing on anything unclean."""
        if keep_open:
            return
        unclean = self.shutdown()
        if unclean:
            pytest.fail("did not shut down cleanly:\n" + "\n".join(unclean), pytrace=False)


@pytest.fixture(scope="session")
def lte_pane_setup():
    """A shell command run before each process. None: nothing."""
    return None


@pytest.fixture(scope="session")
def lte_output_checks():
    """Callables (name, output) -> list of problems."""
    return []


def _module_name(request):
    return request.module.__name__.rpartition(".")[2]


def _make_processes(request, prefix, log_dir, setup, checks, commands=None):
    config = request.config
    module = request.module
    if config.lte_mode == "shell":
        backend = _ShellBackend(config.lte_tty)
    else:
        backend = _TmuxBackend(_run_session(config), prefix)
    config.lte_log_dirs.append(str(log_dir))
    return Processes(backend, commands, setup, checks, getattr(module, "FAIL_ON", None),
                     str(log_dir), getattr(module, "READY_TIMEOUT", 30.0),
                     getattr(module, "SIGINT_TIMEOUT", 10.0))


@pytest.fixture(scope="module")
def processes(request, tmp_path_factory, lte_pane_setup, lte_output_checks):
    """The module's PROCESSES, started and READY; stopped after its last test."""
    commands = getattr(request.module, "PROCESSES", None)
    if not commands:
        raise pytest.UsageError(f"{request.module.__name__}: no PROCESSES to launch")
    module = _module_name(request)
    procs = _make_processes(request, module, tmp_path_factory.mktemp(module),
                            lte_pane_setup, lte_output_checks, commands)
    keep_open = request.config.lte_keep_open
    try:
        for name, pattern in getattr(request.module, "READY", {}).items():
            procs.wait_for(name, pattern, procs.ready_timeout)
    except AssertionError as exc:
        if not keep_open:
            procs.shutdown()
        pytest.fail(f"not ready: {exc}", pytrace=False)
    yield procs
    procs.finish(keep_open)


@pytest.fixture
def launcher(request, tmp_path, lte_pane_setup, lte_output_checks):
    """An empty Processes for the test to start() and stop() its own; what
    is left running is stopped after the test. Logs go to tmp_path."""
    prefix = re.sub(r"[^\w-]", "_", f"{_module_name(request)}.{request.node.name}")
    procs = _make_processes(request, prefix, tmp_path, lte_pane_setup, lte_output_checks)
    yield procs
    procs.finish(request.config.lte_keep_open)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Fail the test itself, not just teardown, if a process died while it ran."""
    outcome = yield
    if outcome.excinfo is not None:
        return
    for procs in item.funcargs.values():
        if isinstance(procs, Processes):
            try:
                procs.check()
            except AssertionError as exc:
                outcome.force_exception(exc)
                return


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
