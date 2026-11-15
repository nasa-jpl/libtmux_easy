"""pytest plugin: launch the processes a test needs, watch them, stop them.

Enable it in conftest.py:

    pytest_plugins = ["libtmux_easy.testing"]

A test class brings its processes up in setup_class and down in
teardown_class:

    from libtmux_easy.testing import Processes, check_health

    class TestSystem:
        @classmethod
        def setup_class(cls):
            cls.procs = Processes(cls, output_checks={"*": r"Traceback"})
            cls.procs.start("sim:server", "./server", ready=r"listening")
            cls.procs.wait_until(server_answers)   # any gate

        @classmethod
        def teardown_class(cls):
            cls.procs.close()      # fails unless each exits 0 on SIGINT
                                   # (optional: see Processes, "Closing")

        def test_x(self):
            with Processes() as procs:   # this test's own, stopped after it
                procs.start("tools:probe", "./probe")

A process that dies, or prints an output check's line, fails the test running
when it happened. A failed test stops the class's processes at once and
skips the rest of the class. A session script's processes: build() runs
its build(session) function, adopt() runs the script and adopts its panes.

Where processes run: subprocesses (default; output to log files,
--echo PATTERN: also to the terminal), --tmux (the dashboard,
attached), --tmux --no-attach (a detached tmux session). --keep-open: never
stop anything. --arg NAME=VALUE: read with arg().

Reference: docs/pytest-plugin.md.
"""

import contextlib
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Mapping, Optional, Sequence, Union

import pytest

from . import lte_dash
from .tmuxcmd import option
from . import processes
from .processes import Process, ProcessFailure, _open, _TmuxBackend, _unique_prefix  # noqa: F401
from .processes import ExitCodes, OutputChecks
from .status import PaneStatus
from .tmux_lib import TmuxSession


def pytest_addoption(parser):
    """The plugin's command-line options: --tmux, --no-attach, --keep-open,
    --echo, --arg."""
    group = parser.getgroup("libtmux_easy")
    group.addoption(
        "--tmux",
        action="store_true",
        help="run processes in tmux and attach: pytest reruns under lte-session, "
        "in the dashboard's home pane",
    )
    group.addoption(
        "--no-attach",
        action="store_true",
        help="as --tmux, in a detached tmux session instead (tmux attach to watch)",
    )
    group.addoption(
        "--keep-open",
        action="store_true",
        help="never stop processes; leave the tmux session up (implies --tmux)",
    )
    group.addoption(
        "--arg",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="a value for the tests to read with arg(NAME), like launch_test's "
        "NAME:=VALUE (repeatable)",
    )
    group.addoption(
        "--echo",
        action="append",
        default=[],
        metavar="PATTERN",
        help="subprocesses: also print the output of those whose 'window:name' "
        "matches, prefixed [name]; '*' for all (repeatable)",
    )


def under_lte_session() -> bool:
    """Whether this runs inside a session with a dashboard, e.g. lte-session's.

    @return: True if it does
    """
    pane = os.environ.get("TMUX_PANE")
    return bool(pane) and bool(option(pane, lte_dash.WINDOW_OPTION))


def _wants_tmux(config):
    """Any option that needs tmux: --tmux, --no-attach or --keep-open."""
    return any(config.getoption(o) for o in ("tmux", "no_attach", "keep_open"))


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    """--tmux in a terminal: rerun this pytest under lte-session, as the
    dashboard's home pane, and attach. Its exit status is pytest's."""
    if (
        not _wants_tmux(config)
        or config.getoption("no_attach")
        or under_lte_session()
        or not shutil.which("tmux")
    ):
        return None
    # pytest captures fds 0-2 by now: look at, and exec with, the real ones.
    capman = config.pluginmanager.getplugin("capturemanager")
    with capman.global_and_fixture_disabled() if capman else contextlib.nullcontext():
        if not (os.isatty(1) and _in_foreground()):
            return None
    if capman:
        capman.suspend(in_=True)
    cmd = [
        sys.executable,
        "-m",
        "libtmux_easy.lte_session",
        "--name",
        _free_session_name(f"lte_test_{os.getpid()}"),
    ]
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
    """Read --arg values; pick the mode (dash under lte-session, tmux, else
    subprocess); set up the run's state on 'config'."""
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
    config.lte_session_name = (
        _free_session_name(f"lte_test_{os.getpid()}") if mode == "tmux" else None
    )
    config.lte_kept = False
    config.lte_kept_servers = []  # adopt()'s tmux servers, kept to look at
    config.lte_log_dirs = []
    config.lte_echo = config.getoption("echo")
    # The real terminal, for --echo: pytest captures fd 1 during tests,
    # but not here.
    config.lte_tty = (
        os.fdopen(os.dup(sys.stdout.fileno()), "w", buffering=1) if config.lte_echo else None
    )


def _free_session_name(base):
    """'base', or 'base-N' if a session has it: one kept by --keep-open, from
    a run whose pid this one reuses."""
    name, n = base, 1
    while (
        subprocess.run(["tmux", "has-session", "-t", f"={name}"], capture_output=True).returncode
        == 0
    ):
        n += 1
        name = f"{base}-{n}"
    return name


def pytest_report_header(config):
    """The mode and where the process logs go, in pytest's header."""
    logs = f"process logs: {config._tmp_path_factory.getbasetemp()}"
    if config.lte_mode == "tmux":
        return [f"lte mode: tmux (watch: tmux attach -t {config.lte_session_name})", logs]
    return [f"lte mode: {config.lte_mode}", logs]


def _run_session(config):
    """The run's TmuxSession, made on first use: lte-session's (dash mode),
    else a new detached one. Processes get pytest's environment, as
    subprocesses do: the tmux server's may predate an activated venv or a
    sourced workspace. Panes don't wait for their shells (wait_start=False):
    rc files run at once."""
    if config.lte_session is None:
        if config.lte_mode != "dash" and config.lte_session_name is None:  # build() without --tmux
            config.lte_session_name = _free_session_name(f"lte_test_{os.getpid()}")
        config.lte_session = (
            TmuxSession.current(env=os.environ, wait_start=False)
            if config.lte_mode == "dash"
            else TmuxSession.create(
                config.lte_session_name, kill_existing=False, env=os.environ, wait_start=False
            )
        )
    return config.lte_session


def pytest_sessionfinish(session, exitstatus):
    """Close every group still open (unchecked). Kill the run's tmux session,
    or keep it: in dash mode, with --keep-open, or after a failure."""
    config = session.config
    for procs in list(_open):  # never closed: don't leave them running
        procs.close(check=False)
    tmux = getattr(config, "lte_session", None)
    if tmux is None:
        return
    # A detached session is kept on failure, to attach and look.
    if config.lte_mode == "dash" or config.lte_keep_open or exitstatus:
        tmux.release(keep_windows=True)
        config.lte_kept = config.lte_mode != "dash"
    else:
        tmux.kill()


_args = {}  # --arg NAME=VALUE, set before collection
_args_read = set()


def arg(name: str, default=None):
    """The value of --arg NAME=VALUE. Usable at import time.

    @param name: NAME
    @param default: the value without the option; its type converts VALUE
        (bools take 1/0, true/false, yes/no). None: the option is required
    @return: the value, as 'default''s type; a str without one
    @raise pytest.UsageError: no option and no 'default', or a VALUE that
        doesn't convert
    """
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
        raise pytest.UsageError(
            f"--arg {name}={value}: expected {type(default).__name__}"
        ) from None


def pytest_terminal_summary(terminalreporter, config):
    """At the end: --arg values no test read, where the logs are, and the tmux
    sessions kept to attach to."""
    unread = sorted(set(_args) - _args_read)
    if unread:
        terminalreporter.write_line(f"warning: no test read --arg {', '.join(unread)}", yellow=True)
    for d in getattr(config, "lte_log_dirs", ()):
        terminalreporter.write_line(f"process logs: {d}")
    for socket in getattr(config, "lte_kept_servers", ()):
        terminalreporter.write_sep(
            "-",
            f"script's session kept: tmux -S {socket} attach  (then tmux -S {socket} kill-server)",
        )
    if getattr(config, "lte_kept", False):
        state = "still running" if config.lte_keep_open else "kept"
        terminalreporter.write_sep(
            "-",
            f"processes {state}: tmux attach -t {config.lte_session_name}  (then tmux kill-session)",
        )


# ---------- Processes, owned by a test ----------


class Processes(processes.ProcessGroup):
    """A group of processes under test and their health, in any mode. Use
    this one in tests.

    It is libtmux_easy.processes.ProcessGroup (the engine, for programs
    that aren't tests) tied to pytest: a test owns it, the mode (subprocess,
    tmux, dash) comes from the command line, and a failure is a pytest
    failure. Same methods: start(), wait_for(), stop(), close(), ...

    Make one in setup_class:

        cls.procs = Processes(cls, output_checks={"*": r"Traceback"})

    or in a test, for that test alone (closed at the end of the 'with', or
    else after the test):

        with Processes() as procs:

    Closing: close() in teardown_class stops everything and fails unless
    each process stopped cleanly (SIGINT was enough, with an allowed code).
    Forget it, and the plugin still stops the group after the class's last
    test, but with close(check=False): nothing is checked. A death while a
    test runs fails that test either way.

    @param owner: the test class (cls, in setup_class). None, in a test: that
        test. A failed test of the owner stops the group at once (on_abort()
        first); a class also skips its remaining tests. Names ('window:pane')
        are unique within a class, across its groups; its tmux windows are
        '<Class>/<window>'.
    @param output_checks: output that fails a process while it runs, by
        process name ("*": every process). Each value is an OutputCheck or a
        list of them: a regex (string or compiled), searched in each line; or
        a function that gets each line and returns a problem message, or None
        when the line is fine. A hit counts as a death: the running test
        fails. E.g. {"*": r"Traceback", "sim:server": [r"ERROR", my_check]}
    @param allowed_exit_codes: the exit codes a process may end with when it
        is stopped, by process name ("*": every process): an int or a set of
        them. Default 0. Death by signal N is 128+N, so 130 allows dying of
        SIGINT. E.g. {"*": {0, 130}}
    @param sigint_timeout: seconds a stopped process gets after SIGINT before
        SIGTERM
    @param on_abort: called with no arguments when a test of the owner fails,
        before everything is stopped: bring things down your own way
    @param log_dir: where the processes' logs go; None: a new temp dir named
        after the owner
    @param on_exit: on_exit(name, PaneStatus, died), on every exit, from a
        watcher thread. 'died': nobody asked for it.
    @param sigterm_timeout: seconds it then gets after SIGTERM before SIGKILL
    @param env: variables for every process of the group (None unsets);
        start(env=) wins. The group's only: never put in os.environ, so
        other classes and tests don't see it.
    @raise RuntimeError: the plugin isn't enabled
    @raise TypeError: no owner outside a test, or an owner that isn't a test
        class
    """

    def __init__(
        self,
        owner: Union[pytest.Item, type, None] = None,
        *,
        output_checks: Optional[Mapping[str, OutputChecks]] = None,
        allowed_exit_codes: Optional[Mapping[str, ExitCodes]] = None,
        sigint_timeout: float = 5.0,
        on_abort: Optional[Callable[[], None]] = None,
        log_dir: Union[str, os.PathLike, None] = None,
        on_exit: Optional[Callable[[str, PaneStatus, bool], None]] = None,
        sigterm_timeout: float = 5.0,
        env: Optional[Mapping[str, Optional[str]]] = None,
    ):
        if not _plugin:
            raise RuntimeError(
                "Processes() needs the plugin: "
                'pytest_plugins = ["libtmux_easy.testing"] in conftest.py'
            )
        if owner is None:
            owner = _plugin.get("item")
            if owner is None:
                raise TypeError("Processes() outside a test: in setup_class, Processes(cls)")
        if isinstance(owner, pytest.Item):
            scope = owner.cls or owner  # whose names and windows it shares
            test = re.sub(r"[^\w-]", "_", owner.name)
            prefix = owner.cls.__name__ if owner.cls else test
            log_name = f"{prefix}.{test}" if owner.cls else test
        elif isinstance(owner, type):
            scope = owner
            prefix = log_name = owner.__name__
        else:
            raise TypeError(
                f"Processes(owner): a test class, e.g. Processes(cls) in "
                f"setup_class; got {owner!r}"
            )
        config = _plugin["config"]
        if log_dir is None:
            log_dir = _plugin["tmp_path_factory"].mktemp(f"{log_name}_")  # TestFoo_0, ...
        tmux = None if config.lte_mode == "subprocess" else _class_tmux_backend(scope, prefix)
        super().__init__(
            prefix,
            tmux=tmux,
            prefix=prefix,
            log_dir=str(log_dir),
            echo=config.lte_echo,
            tty=config.lte_tty,
            setup=_plugin["setup"],
            output_checks=output_checks,
            allowed_exit_codes=allowed_exit_codes,
            sigint_timeout=sigint_timeout,
            sigterm_timeout=sigterm_timeout,
            on_abort=on_abort,
            on_exit=on_exit,
            keep_open=config.lte_keep_open,
            scope=scope,
            env=env,
        )
        config.lte_log_dirs.append(str(log_dir))
        self.owner = owner

    def _fail(self, message):
        """Fail the test: pytest.fail, without a traceback into this module."""
        pytest.fail(message, pytrace=False)

    def _tmux_for_build(self):
        """build() without --tmux: the owner's tmux backend in the run's
        session."""
        return _class_tmux_backend(self.scope, self.prefix)

    def _kept_server(self, socket):
        """Note adopt()'s tmux server, kept after a failure, for the terminal
        summary."""
        _plugin["config"].lte_kept_servers.append(socket)


_tmux_backends = {}  # test class (or test) -> the _TmuxBackend its groups share


def _class_tmux_backend(scope, prefix):
    """The _TmuxBackend every group of a test class (or test) shares, made on
    first use."""
    if scope not in _tmux_backends:
        _tmux_backends[scope] = _TmuxBackend(_run_session(_plugin["config"]), prefix)
    return _tmux_backends[scope]


def check_health():
    """Fail if any open Processes saw a death. For helpers deep in test code
    that weren't handed one, e.g. a polling loop.

    @raise ProcessFailure: an AssertionError, naming each problem
    """
    for procs in list(_open):
        procs.check()


@pytest.fixture(scope="session")
def lte_pane_setup():
    """A fixture: a shell command run before each process. Override it in
    conftest.py.

    @return: the command; None: nothing
    """
    return None


_plugin = {}  # what Processes() needs outside a fixture; see _lte_plugin


@pytest.fixture(scope="session", autouse=True)
def _lte_plugin(request, tmp_path_factory, lte_pane_setup):
    """Before any setup_class: it can't take fixtures."""
    _plugin.update(config=request.config, tmp_path_factory=tmp_path_factory, setup=lte_pane_setup)
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


def parametrize(argnames: str, argvalues: Sequence[Any]) -> Callable[[type], type]:
    """Class decorator: run the whole class -- setup_class, its tests,
    teardown_class -- once per value, like launch_testing.parametrize.
    Each value is a class attribute, e.g. cls.rate in setup_class:

        @parametrize("rate", [5, 10])
        class TestRover: ...           # TestRover[rate=5], TestRover[rate=10]

    Stacked decorators multiply.

    @param argnames: the attribute's name; several, comma-separated ("a, b"),
        take values as tuples
    @param argvalues: one value (or tuple) per run of the class
    @return: the decorator
    """
    names = [n.strip() for n in argnames.split(",") if n.strip()]

    def decorate(cls):
        sets = [tuple(v) if len(names) > 1 else (v,) for v in argvalues]
        before = cls.__dict__.get("_lte_params", [{}])
        cls._lte_params = [dict(b, **dict(zip(names, values))) for b in before for values in sets]
        return cls

    return decorate


@pytest.hookimpl(tryfirst=True)
def pytest_pycollect_makeitem(collector, name, obj):
    """Collect a parametrize()d class as one class per value set, named
    'Class[a=1,b=2]'."""
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
        """The generated subclass, which has the values as class attributes."""
        return self._lte_cls
