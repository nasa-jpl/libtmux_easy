import collections
import json
import logging
import os
import re
import shlex
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Mapping, Optional, Union

import libtmux
import psutil
from libtmux.common import exc, tmux_cmd
from libtmux.pane import Pane
from libtmux.session import Session
from libtmux.window import Window
from packaging import version

from . import close as _close, dash, starter as _starter
from .dash import keys as _keys
from ._tmux import env_args, option, pane_env
from .dash.homelog import _SkipIfTeed, _Tee
from .output import (POLL as _OUTPUT_POLL, QUIET as _OUTPUT_QUIET, Capture, OutputExpect,
                     OutputMatch, OutputReader, OutputWatch, PaneExited, PIPE_FILE, PIPE_FROM,
                     PIPE_ID, plain_lines)
from .proc import pane_child_processes
from .status import ExitStatus, PaneStatus
from .watcher import _PaneWatcher, _proc_alive, _WrapRun
from .wrap import (DIR_OPTION, PIPE_TEE_OPTION, PROTOCOL_VERSION as WRAP_PROTOCOL_VERSION,
                   SOCKET_NAME, guarded_pipe, guarded_unpipe)

libtmux_version = version.parse(libtmux.__version__)

logger = logging.getLogger(__name__)

try:
    from libtmux.exc import DeprecatedError
except ImportError:
    # DeprecatedError doesn't exist in older versions. Create a dummy exception that will never be
    # raised by older versions
    class DeprecatedError(Exception):
        pass


PANE_MODE_SHELL = "shell"
PANE_MODE_EXEC = "exec"
PANE_MODE_WRAP = "wrap"
_PANE_MODES = (PANE_MODE_SHELL, PANE_MODE_EXEC, PANE_MODE_WRAP)


# How long a run typed with send_keys('lte-run ...') may take to report that
# it started; until then wait_for_output() treats it as running.
_RUN_START = 5.0

# How long a new pane's shell may take to start (~/.profile, ~/.bashrc) before
# new_pane() types into it anyway (e.g. a PS1 that draws nothing).
_SHELL_START = 5.0


def _check_mode(mode: str) -> str:
    if mode not in _PANE_MODES:
        raise ValueError(f"mode must be one of {_PANE_MODES}, got {mode!r}")
    return mode


# A library-launched command containing these is run as 'bash -c <cmd>', so
# the whole thing is supervised -- not just the part before the operator.
# Over-wrapping (an operator that was only inside quotes) is harmless.
_SHELL_OPERATORS = ("&&", "||", ";", "|", "&")

# sun_path is 108 bytes on Linux, including the terminating NUL.
_MAX_SOCKET_PATH = 107


def _wrap_prefix() -> str:
    """How the library invokes lte-run in a pane: the bare name if it is
    installed, else an absolute path that works from a plain checkout."""
    if shutil.which("lte-run"):
        return "lte-run"
    script = Path(__file__).resolve().with_name("wrap.py")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


def _make_wrap_dir() -> str:
    """A private (0700) temp dir for the session's control socket. Falls back
    to /tmp when $TMPDIR is long enough to push the socket path past the
    AF_UNIX limit."""
    path = tempfile.mkdtemp(prefix="lte_")
    if len(os.path.join(path, SOCKET_NAME).encode()) <= _MAX_SOCKET_PATH:
        return path
    os.rmdir(path)
    return tempfile.mkdtemp(prefix="lte_", dir="/tmp")


# Marker used to scrape '$?' back out of a shell pane. Deliberately unlikely to
# appear in real output.
_EXIT_MARKER = "__LTE_EXIT__"
_EXIT_RE = re.compile(rf"^{re.escape(_EXIT_MARKER)}:(-?\d+):$", re.M)

# Output capture (TmuxSession.capture()). One hook per session pipes a new
# pane from its first byte: to @lte_pipe_next (set by the library just before
# it makes a pane), else to the @lte_pipe template of its window or session.
# Index 77 leaves the user's own hooks (index 0) alone.
_PIPE_HOOKS = ("after-split-window[77]", "after-new-window[77]")
_PIPE_HOOK = (
    'if -F "#{@lte_pipe_next}" { pipe-pane -O "#{E:@lte_pipe_next_cmd}" ; '
    f'set -pF {PIPE_FILE} "#{{@lte_pipe_next}}" ; set -pF {PIPE_FROM} "#{{@lte_pipe_next_from}}" ; '
    'set -u @lte_pipe_next ; set -u @lte_pipe_next_cmd ; set -u @lte_pipe_next_from } '
    '{ if -F "#{@lte_pipe}" { pipe-pane -O "#{E:@lte_pipe_cmd}" ; '
    f'set -pF {PIPE_FILE} "#{{E:@lte_pipe}}" ; set -p {PIPE_FROM} 0 }} }}')
_PIPE_NAME = "#{s/[^A-Za-z0-9_.-]/_/:window_name}.#{pane_id}.log"  # a pane made by hand


def _safe_name(name) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))


def _file_size(path) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def _tmux_literal(text) -> str:
    """'text' as itself in a pipe-pane command, which tmux expands as a
    format and with strftime: 'main.%4.log' would become 'main.'."""
    return text.replace("#", "##").replace("%", "%%")


def _cat_to(path) -> str:
    """pipe-pane's command for 'path'."""
    return _tmux_literal(f"exec cat >> {shlex.quote(path)}")


# Global default server object
_server = libtmux.Server(
    socket_name=None,
    socket_path=None,
    config_file=None,
    colors=None,
)


def set_server(**kwargs):
    """Set the server object that will be used by the rest of the this library. This allow users to
    specify custom tmux server options (e.g. socket name)"""
    global _server
    _server = libtmux.Server(**kwargs)


def tmux_command(cmd: str) -> str:
    """Helper function to run a tmux command and return the output. Raises an exception if the
    command returns an error."""
    proc = tmux_cmd(*shlex.split(cmd))
    if proc.stderr:
        raise exc.LibTmuxException(
            "Command: %s returned error: %s" % (proc.cmd, proc.stderr)
        )
    return proc.stdout


def session_exists(session_name: str) -> bool:
    """Returns True if tmux session already exists."""
    assert isinstance(session_name, str)
    exists = _server.has_session(session_name)
    return exists


def _pane_cmd(pane: Pane, cmd: str, *args):
    """Run a tmux command against 'pane', always with an explicit '-t'.

    Not Pane.cmd(): libtmux (0.30 at least) adds '-t <pane>' only when no
    argument contains the substring '-t', so text like 'node --topic x' or a
    title like 'my-topic' silently went to the session's active pane."""
    return pane.server.cmd(cmd, "-t", pane.id, *args)


def _type(pane: Pane, text: str, enter: bool = True):
    """Pane.send_keys(), but always into 'pane' (see _pane_cmd)."""
    _pane_cmd(pane, "send-keys", text)
    if enter:
        _pane_cmd(pane, "send-keys", "Enter")


def _set_pane_title(pane: Pane, pane_name: str):
    """Set the title of a tmux pane. Args are passed as a list: titles carry a
    status suffix ('sim [running]'), which a shell-split string would break."""
    proc = _pane_cmd(pane, "select-pane", "-T", pane_name)
    if proc.stderr:
        raise exc.LibTmuxException(
            "Command: %s returned error: %s" % (proc.cmd, proc.stderr)
        )


def _create_session(
    session_name,
    window_name="unnamed",
    pane_name="unnamed",
    kill_existing=True,
    **kwargs,
):
    """Create a new tmux session with the given name. If a session with the same name already
    exists, it can be killed and replaced with a new session. Returns the created session, window,
    and pane objects."""

    global _server
    # TODO - just use kill_session option in new_session()

    if session_exists(session_name):
        if not kill_existing:
            raise ValueError(
                f"Session {session_name} already exists and kill_existing is False."
            )
        session = [
            session for session in _server.sessions if session.name == session_name
        ][0]
        logger.info(
            f"Session {session_name} exists. Killing and creating new session."
        )
        if hasattr(session, "kill"):
            session.kill()
        else:
            session.kill_session()  # libtmux < 0.30
        session = _server.new_session(
            session_name=session_name,
        )
    else:
        logger.info(f"Creating new session named {session_name}")
        session = _server.new_session(session_name=session_name, **kwargs)

    window = session.windows[0]
    pane = window.panes[0]

    if window_name:
        window.rename_window(window_name)

    if pane_name:
        _set_pane_title(pane, pane_name)

    return session, window, pane


# =================================================================================
# -------------------------------  TmuxSession Class  -----------------------------
# =================================================================================


class TmuxSession(Session):
    """Helper class that streamlines the creation of a tmux session. It is "stateful" in that it
    will act on the most recent context. Sort of similar to matplotlib. (e.g. new_pane will create a
    new pane in the most recently created window)

    New Windows and Panes
    ---------------------
    For a new session, tmux will by default create a new window and pane (which we will title
    'unnamed'), and the first call to new_window() or new_pane() will reuse that initial
    window/pane.  Then subsequent calls will create new windows/panes as normal.

    Similarly, when new_window() creates a new window, tmux will create an new pane for that
    window. The next call to new_pane() will reuse that initial pane, and then subsequent calls will
    create new panes as normal.

    Naming
    ---------------------
    When created, windows and names will automatically be given unique ids. The first window will be
    @0, the second will be @1, etc. Panes are named %0, %1, etc.

    In this class, windows and panes are also always assigned a user-specified name.
        - Window names must be unique.
        - Pane names must be unique within a window.

    Becuase pane names can be repeated across windows, the identifying name of a pane is actually
    'window_name:pane_name'. This is the name that should be supplied to functions that accept a pane
    name.

    In general, functions that accept a target_pane will accept a Pane object, the pane id, or the
    fully qualified pane name (i.e. 'window_name:pane_name').
    """

    def __init__(self, **kwargs):
        raise NotImplementedError(
            "TmuxSession cannot be directly instantiated. Use TmuxSession.create() instead."
        )

    @classmethod
    def create(
        cls,
        session_name: str,
        kill_existing: bool = True,
        title_status: bool = True,
        status_file: Optional[str] = None,
        env: Optional[Mapping[str, str]] = None,
        capture: Union[None, bool, str] = None,
        mode: str = PANE_MODE_SHELL,
        **kwargs,
    ) -> "TmuxSession":
        """
        The primary entry point to create a managed TmuxSession.

        'title_status' keeps each pane's border title updated with its
        running/exit state (see new_pane()'s on_exit, and _refresh_pane_title) --
        a zero-infrastructure visual monitor. Set False for pristine titles.

        'status_file', if given, is a path that gets the output of status()
        written to it as JSON on every pane state transition, so an external
        process can monitor pane state without any libtmux knowledge.

        'env' (e.g. os.environ, or a dict of your own) goes into every pane;
        see copy_environment(). Without it, panes get the tmux server's
        environment, which may predate e.g. an activated venv.

        'capture' (True, or a directory) saves every pane's output to a
        file: capture().

        'mode' is new_pane()'s default mode: set_default_mode().
        """
        _check_mode(mode)
        session, window, pane = _create_session(
            session_name, kill_existing=kill_existing, **kwargs
        )
        session.__class__ = cls
        session.session_name = session_name
        session._adopted = False
        session.__post_init__(window, pane, title_status=title_status, status_file=status_file)
        session.default_mode = mode
        _starter.apply(run=session._tmux_run)
        session.bind_close_key()
        if env:
            session.copy_environment(env)
        if capture:
            session.capture(None, None if capture is True else capture)
        return session

    @classmethod
    def current(cls, title_status: bool = True, status_file: Optional[str] = None,
                env: Optional[Mapping[str, str]] = None,
                mode: str = PANE_MODE_SHELL) -> "TmuxSession":
        """Adopt the tmux session this process is running in ($TMUX_PANE),
        e.g. a test run by lte-session, or a script run in your own session.

        Unlike create(), the session isn't this object's:
          - there is no unnamed window to reuse; call new_window() first
          - kill_session() / kill() close only the windows this object made
          - an open dashboard (lte-session's, say) is reused, not replaced

        'status_file' defaults to one in the control dir when the session has
        a dashboard, so its sidebar lists this object's panes.

        'mode' is new_pane()'s default mode: set_default_mode()."""
        _check_mode(mode)
        pane_id = os.environ.get("TMUX_PANE")
        tmux_env = os.environ.get("TMUX", "")
        if not pane_id or not tmux_env:
            raise RuntimeError("TmuxSession.current(): not running inside tmux")
        server = libtmux.Server(socket_path=tmux_env.split(",")[0])
        session_id = server.cmd("display-message", "-p", "-t", pane_id, "#{session_id}").stdout
        matches = [s for s in server.sessions if session_id and s.id == session_id[0].strip()]
        if not matches:
            raise RuntimeError(f"TmuxSession.current(): no session for pane {pane_id}")
        session = matches[0]
        session.__class__ = cls
        session.session_name = session.name
        window = session.windows[0]
        session.__post_init__(window, window.panes[0], title_status=title_status)
        session.default_mode = mode
        _starter.apply(run=session._tmux_run)

        session._adopted = True
        session.unnamed_window = False
        session.unnamed_pane = {}
        session.current_window = session.current_pane = None
        dash_window = session._session_option(dash.WINDOW_OPTION)
        if dash_window and session.get_window(dash_window) is not None:
            session._dash_window = dash_window
            session._dash_sidebar = session._session_option(dash.SIDEBAR_OPTION) or None
            session._dash_owned = False
            if status_file is None:
                # lte-session's run dir outlives us, so the sidebar can still
                # read how our panes ended after kill_session().
                run_dir = session._session_option(dash.RUN_DIR_OPTION)
                if run_dir and os.path.isdir(run_dir):
                    status_file = tempfile.mkstemp(prefix="status_", suffix=".json",
                                                   dir=run_dir)[1]
                else:
                    status_file = os.path.join(session._wrap_dir, "status.json")
        if status_file is not None:
            session.status_file = status_file
            session._publish_status_file()
        if env:
            session.copy_environment(env)
        return session

    def _session_option(self, name) -> str:
        return option(self.id, name, run=self._tmux_run)

    def _set_session_option(self, name, value=None):
        """Set a session option, or unset it when 'value' is None."""
        if value is None:
            self.server.cmd("set-option", "-uq", "-t", self.id, name)
        else:
            self.server.cmd("set-option", "-q", "-t", self.id, name, value)

    def _publish_status_file(self):
        """Point the session's @lte_status at our status file, for a sidebar
        that follows it (lte-dash without --status)."""
        self._set_session_option(dash.STATUS_OPTION, os.path.abspath(self.status_file))
        self._write_status_file()

    def __post_init__(self, window, pane, title_status: bool = True, status_file: Optional[str] = None):
        # Set internal state
        self.current_window = window
        self.current_pane = pane

        # Set default options. These allow for pane titles to be displayed
        self.set_option("renumber-windows", "on")
        try:
            self.set_option("pane-border-status", "top", _global=True)
        except TypeError:  # renamed global_ in newer libtmux
            self.set_option("pane-border-status", "top", global_=True)

        self.unnamed_window = True
        self._initial_window = window.id  # the one new_window() reuses
        self.unnamed_pane = {self.current_window.id: True}

        self.pane_names = {}  # Mapping of pane_name -> Pane object
        self.commands = {}
        self.command_before = {"all": None}

        self.pane_modes = {}      # pane.id -> PANE_MODE_*
        self.default_mode = PANE_MODE_SHELL  # new_pane(mode=None), see set_default_mode()
        self._session_env = {}    # see create(env=)
        self._window_env = {}     # window.id -> env, see new_window(env=)
        self._pane_env = {}       # pane.id -> the env it was started with
        self._pane_tee = {}       # pane.id -> lte-run --tee file, see new_pane(tee=)
        self._exit_status = {}    # pane.id -> ExitStatus (cached; see one-shot note)
        self._stopped_by = {}     # pane.id -> last signal stop_pane() sent this run

        # Target pids, exit events, status surfaces (docs/exit-status.md).
        self.title_status = title_status
        self.status_file = status_file
        self.pane_targets = {}        # pane.id -> tuple of psutil.Process (the pids
                                       # the pane's command itself spawned)
        self._exit_callbacks = {}     # pane.id -> list of on_exit(PaneStatus) callbacks
        self._watched_panes = set()   # pane.id -- already armed, avoid double-registering
        self._watcher = None          # lazily-started _PaneWatcher
        self._status_lock = threading.Lock()

        # Wrap mode. Set up unconditionally, so 'lte-run' typed by hand works
        # in any pane of the session.
        self._wrap_runs = {}      # pane.id -> _WrapRun (latest run only)
        self._wrap_started = {}   # pane.id -> threading.Event, set on 'started'
        self._wrap_pending = set()  # pane.id typed with enter=False, not yet run
        self._run_typed = {}  # pane.id -> when send_keys() typed 'lte-run ...', until it reports
        self._wrap_dir = _make_wrap_dir()
        self._wrap_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._wrap_listener.bind(os.path.join(self._wrap_dir, SOCKET_NAME))
        self._wrap_listener.listen(64)
        self.set_option(DIR_OPTION, self._wrap_dir)
        if status_file is not None:
            self._set_session_option(dash.STATUS_OPTION, os.path.abspath(status_file))
        self._ensure_watcher().add_listener(self._wrap_listener)

        self._killed = False      # see kill_session()
        self._own_windows = []    # ids of windows new_window() made
        self._dash_owned = True   # False for a dashboard adopted by current()
        self._dash_window = None  # window id, see dashboard()
        self._dash_sidebar = None
        self._home_tee = None     # (file, (stdout, stderr), handler), see tee_home()
        self._on_close = []       # (callable, when), see on_close()
        self._close_keys = []     # keys borrowed by bind_close_key()
        self._reader = None       # OutputReader, started by the first output watch
        self._typed = {}          # pane.id -> recent texts typed into it (echo)
        self._pending_output = None  # new_pane(on_output=): watches for the claimed pane
        self._pending_capture = None  # new_pane(capture=)
        self._scope_captures = {}  # None (session) or window id -> Capture
        self._capture_hooked = False
        self._watch_dir = None    # files of panes watched but not captured
        self.keep_output = False  # keep those files after the session

    def new_window(
        self, name: str, layout: Union[None, str] = "tiled",
        env: Optional[Mapping[str, str]] = None,
        capture: Union[None, bool, str] = None, **window_kwargs
    ) -> Window:
        """Create a new window with the given name. Due to the nature of tmux, this will by also
        create a default, unnamed pane. The next call to new_pane will reuse and rename this pane
        instead of actually creating a new pane.

        'env' goes into every pane new_pane() makes in this window, on top of
        the session's (copy_environment()). tmux has no window environment,
        so panes made by hand in it don't get it.

        'capture' (True, or a directory) saves the output of every pane in
        it to a file: capture()."""

        if self.unnamed_window:
            # By id: a dashboard() opened before this is window 0 now.
            window = self.get_window(self._initial_window)
            window.rename_window(name)
            self.unnamed_window = False
            # Update current_window to the renamed window
            self.current_window = window
            self.current_pane = window.panes[0]
        else:
            if name in [w.name for w in self.windows]:
                raise ValueError(f"Window name {name} already exists.")

            window = super().new_window(name, **window_kwargs)
            self._own_windows.append(window.id)

            self.current_window = window
            self.current_pane = window.panes[0]
            self.unnamed_pane[self.current_window.id] = True

        if env:
            self._window_env[window.id] = pane_env(env)

        if layout:
            # 'layout' is an attribute already used by libtmux, hence layout_str.
            # This must be set on the reused first window too: split_window()
            # only re-tiles when it is present, and without that every split
            # halves the previous pane (30 -> 14 -> 7 -> 3 -> 2 rows), so the
            # sixth pane fails with 'no space for new pane'.
            window.layout_str = layout

        if capture:
            self.capture(window, None if capture is True else capture)
        return window


    def send_command_before(self, command: str, window: Union[str, Window] = None):
        """Helper function to send a command before sending the actual command to a new pane. This
        is useful for setting up the context for the command (e.g. changing directories, setting env
        vars, etc.)
        
        If 'window' is provided, the command will be sent to all the new panes for that window.
        Otherwise, the command will be sent to all new panes for the entire session.
        
        NOTE - a window command_before will override a session command_before.
        """
        if window:
            window = self.resolve_target_window(window)
            self.command_before[window.id] = command
        else:
            self.command_before["all"] = command

    def copy_environment(self, env: Optional[Mapping[str, str]] = None):
        """Give every pane started from now on 'env' (os.environ by default),
        added to and overriding what was copied before. In a session of our
        own (create()) it is the session's tmux environment, so panes made by
        hand get it too; in an adopted one (current()), only ours do. For one
        window or one pane, see new_window(env=) and new_pane(env=).

        An alternative to send_command_before("source ...") for an
        environment that is already set up (an activated venv, a sourced ROS
        workspace). _tmux.SKIP_ENV (TMUX, TERM, LTE_DIR, ...) is never copied."""
        env = pane_env(os.environ if env is None else env)
        self._session_env.update(env)
        if not self._adopted:
            for name, value in env.items():
                self.server.cmd("set-environment", "-t", self.id, name, value)

    def _claim_pane(self, name, env=None, **split_kwargs):
        """Return (pane, reused, env) for the pane 'name' should occupy -- either
        the window's initial unnamed pane (first call after new_window) or a
        freshly split one. 'reused' matters to callers that must respawn the
        pane, since a reused pane is already running its login shell. 'env' is
        the pane's: session, window and 'env' merged. A split pane starts with
        it; a reused one gets it only when respawned (respawn-pane -e)."""
        env = {**self._session_env, **self._window_env.get(self.current_window.id, {}),
               **pane_env(env)}
        capture = self._new_pane_file(name)
        if self.unnamed_pane[self.current_window.id]:
            pane = self.current_window.panes[0]
            self.set_pane_title(pane, name)
            self.unnamed_pane[self.current_window.id] = False
            self._pane_env[pane.id] = env
            self._claimed(pane, True, capture)
            return pane, True, env

        full_name = self._full_pane_name(self.current_window, name)
        if full_name in self.pane_names:
            raise ValueError(
                f"Pane name {name} already exists in window {self.current_window.name}."
            )
        if env:
            split_kwargs["environment"] = env
        if capture:
            self._arm_pipe(capture)
        try:
            pane = self.split_window(self.current_window, name, **split_kwargs)
        finally:
            if capture:
                self._disarm_pipe()
        self._pane_env[pane.id] = env
        self._claimed(pane, False, capture)
        return pane, False, env

    def new_pane(
        self,
        name: str,
        command: str,
        enter: bool = True,
        mode: Optional[str] = None,
        on_exit=None,
        target_pid_timeout: float = 1.0,
        env: Optional[Mapping[str, str]] = None,
        tee: Optional[str] = None,
        on_output: Optional[Mapping[str, object]] = None,
        capture: Union[None, bool, str] = None,
        **pane_kwargs,
    ) -> Pane:
        """Create a new pane named 'name' and run 'command' in it.

        mode=None (default) uses the session's default: set_default_mode(),
        shell unless changed.

        mode=PANE_MODE_SHELL
            The pane runs a shell and 'command' is typed into it. The shell
            survives the command, so the pane stays interactive -- you can stop,
            edit and rerun by hand. exit_status() reads the shell's '$?'.

        mode=PANE_MODE_EXEC
            'command' becomes the pane's own process, which lets tmux record its
            exit status directly (see exit_status()). The pane has no shell, so it
            cannot be typed into; with remain-on-exit it stays visible after the
            command ends, showing its final output, and respawn_pane() can rerun it.

        mode=PANE_MODE_WRAP
            Shell mode, but the command is launched as 'lte-run <command>'. The
            wrapper reports the command's real pid and exact exit status over a
            socket, so there is no 128+N guessing (ExitStatus.inferred is never
            set), nothing a user types in the pane can clobber the result, and
            reruns by hand are tracked automatically. Costs one ~13 MB helper
            process per pane.

        'enter' applies to shell and wrap mode only (exec mode has no shell to
        submit to).

        'env' (e.g. os.environ, or a dict of your own) is this pane's
        environment, on top of the window's (new_window(env=)) and the
        session's (copy_environment()); later levels win. It is added to the
        tmux server's environment, not a replacement for it.

        'tee' (wrap mode only) is a file that also gets the command's output,
        as plain text: 'lte-run --tee'. The pane still shows it. Complete by
        the time on_exit fires; a rerun overwrites it.

        'on_exit', if given, is a callback fired with a PaneStatus the moment the
        command exits for any reason (normal exit, crash, or a signal -- including
        a Ctrl-C typed by a user who attached to the session). Equivalent to
        calling watch_pane(pane, on_exit) right after creation; see watch_pane()
        for callback-thread caveats.

        'on_output' ({pattern: callback(OutputMatch)}) watches the pane's
        output from its first byte: watch_output() for each item.

        'capture' (True, or a file) saves the pane's output from its first
        byte: capture(). None follows a window or session capture; False
        opts out of one.

        'target_pid_timeout' bounds how long (shell mode only) to wait for the
        command's immediate child process(es) to appear -- the pids that
        is_running()/exit_status()/on_exit are actually judged on. See
        _capture_target_pids().

        Caveat: exec mode relies on split_window(shell=...), which needs
        libtmux >= 0.21. _claim_pane()'s reuse path uses respawn-pane instead,
        so it works on any version.
        """
        mode = _check_mode(self.default_mode if mode is None else mode)
        if tee is not None and mode != PANE_MODE_WRAP:
            raise ValueError("'tee' needs mode=PANE_MODE_WRAP (it is lte-run's --tee)")
        if self.unnamed_window:
            raise ValueError("Cannot create a new pane before creating a new window.")
        if self.current_window is None:
            raise ValueError("Call new_window() first: an adopted session (current()) "
                             "has no window of its own to put panes in.")

        before = (
            self.command_before.get(self.current_window.id)
            or self.command_before["all"]
        )

        if tee is not None:
            pane_kwargs["_tee"] = os.path.abspath(tee)
        # Both used by _claim_pane(): the pane is piped as it is made (an
        # exec pane's command starts with it), then watched from byte 0.
        self._pending_output = on_output
        self._pending_capture = capture
        try:
            if mode == PANE_MODE_EXEC:
                pane = self._new_exec_pane(name, command, before, env, **pane_kwargs)
            else:
                pane = self._new_shell_pane(name, command, before, enter, mode, env,
                                            **pane_kwargs)
        finally:
            self._pending_output = self._pending_capture = None

        self.pane_modes[pane.id] = mode
        self.commands[self.get_pane_name(pane)] = [command]
        self.current_pane = pane

        if mode == PANE_MODE_WRAP and not enter:
            # Nothing runs until someone presses Enter; 'started' tracks it then.
            # Until then the pane must not be probed for '$?': the probe's own
            # Enter would submit the half-typed line.
            self._wrap_pending.add(pane.id)
            self.pane_targets[pane.id] = ()
        else:
            self.pane_targets[pane.id] = self._capture_target_pids(pane, mode, target_pid_timeout)
        self._refresh_pane_title(pane)
        if self.title_status or self.status_file is not None or on_exit is not None:
            self.watch_pane(pane, on_exit)
        self._write_status_file()
        return pane

    def _capture_target_pids(self, pane, mode, timeout):
        """Record the process(es) the pane's command actually started.

        This is what liveness and exit events are judged on, rather than 'does
        the pane have any children' -- the latter is masked by anything else
        running in the pane (e.g. a user who attached and ran a command).

        Returns a possibly-empty tuple; empty means the command exited before
        it could be seen, or never started."""
        if mode == PANE_MODE_EXEC:
            try:
                return (psutil.Process(int(pane.pane_pid)),)
            except (psutil.NoSuchProcess, ValueError, TypeError):
                return ()

        if mode == PANE_MODE_WRAP:
            started = self._wrap_started.get(pane.id)
            # Floor covers interpreter startup + the tmux lookup (~25 ms
            # measured) on a heavily loaded machine.
            if started is not None and started.wait(max(timeout, 5.0)):
                return self.pane_targets.get(pane.id, ())
            logger.error(
                f"lte-run never reported from pane {self.get_pane_name(pane) or pane.id}; "
                f"tracking it as a plain shell pane. Is '{_wrap_prefix()}' runnable there?"
            )
            return self._capture_target_pids(pane, PANE_MODE_SHELL, timeout)

        deadline = time.time() + timeout
        while time.time() < deadline:
            procs = pane_child_processes(pane)
            if procs:
                return tuple(procs)
            time.sleep(0.05)
        logger.debug(
            f"No target pid observed for pane {self.get_pane_name(pane) or pane.id} "
            f"within {timeout}s; treating its command as already exited."
        )
        return ()

    def _new_shell_pane(self, name, command, before, enter, mode, env, _tee=None,
                        **pane_kwargs):
        """Shell and wrap mode: type 'command' into the pane's shell."""
        pane, reused, env = self._claim_pane(name, env, **pane_kwargs)
        if _tee is not None:
            self._pane_tee[pane.id] = _tee
        if reused and env:
            # Its shell predates 'env': start a fresh one with it.
            _pane_cmd(pane, "respawn-pane", "-k", *env_args(env))
            pane.refresh()
        self._wait_shell_ready(pane)
        if before:
            self._type(pane, before, enter=True)
            # Let setup finish, so _capture_target_pids() snapshots the target
            # command and not the setup command. Uses the any-children test
            # (via is_running()'s fallback), since no target is recorded yet.
            self.wait_for_pane_idle(pane, timeout=30.0)
        if mode == PANE_MODE_WRAP:
            self._send_wrapped(pane, command, enter)
        else:
            self._type(pane, command, enter)
        return pane

    def _wait_shell_ready(self, pane, timeout=_SHELL_START):
        """Until the pane's shell has started: drawn its prompt (the cursor
        has moved) and no child left (what ~/.bashrc runs). Typed earlier, the
        command echoes as output, and _capture_target_pids() records the rc
        file's helpers instead of the command. Twice in a row, since an rc file
        that prints moves the cursor before it is done."""
        deadline = time.time() + timeout
        ready = 0
        while time.time() < deadline:
            moved = self._pane_fmt(pane, "#{cursor_x},#{cursor_y}") not in ("", "0,0")
            ready = ready + 1 if moved and not pane_child_processes(pane) else 0
            if ready == 2:
                return
            time.sleep(0.05)
        logger.debug(f"Pane {pane.id}: shell not ready within {timeout}s; typing anyway.")

    def _new_exec_pane(self, name, command, before, env, **pane_kwargs):
        # A command_before still works here, but it has to run in the same process
        # that then becomes 'command' -- hence 'exec'. Without it the pane process
        # would be the setup shell, and tmux would report ITS status, not the
        # command's. This is the idiom for e.g. sourcing an environment
        # before launching.
        shell_command = command
        if before:
            inner = f"{before} && exec {command}"
            shell_command = f"bash -c {shlex.quote(inner)}"

        # 'shell' is how this mode launches the command; a caller-supplied one
        # would be silently overwritten, so reject it rather than confuse.
        if "shell" in pane_kwargs:
            raise ValueError("'shell' is not accepted in exec mode; pass 'command' instead.")

        pane, reused, env = self._claim_pane(name, env, shell=shell_command, **pane_kwargs)

        # Must happen before the command can exit, or a fast-failing command takes
        # its own exit status (and possibly the whole session) down with the pane.
        self._set_remain_on_exit(pane)

        if reused:
            # The reused initial pane is still running its login shell -- the
            # 'shell' kwarg above never applied to it. Replace its process.
            _pane_cmd(pane, "respawn-pane", "-k", *env_args(env), shell_command)
            # respawn-pane kills the old process and starts a new one under
            # the same pane, but this Pane object's 'pane_pid' field is a
            # cached snapshot from whenever it was first fetched -- it still
            # holds the OLD (now-dead) pid unless refreshed. Without this,
            # _capture_target_pids() would watch a pid that's already gone.
            pane.refresh()

        return pane

    def _set_remain_on_exit(self, pane):
        """Keep a pane and its recorded exit status alive after its process ends.
        Per-pane scope needs tmux >= 3.0; fall back to the window option."""
        result = _pane_cmd(pane, "set-option", "-p", "remain-on-exit", "on")
        if getattr(result, "stderr", None):
            logger.debug("per-pane remain-on-exit unavailable; falling back to window scope")
            self.current_window.set_window_option("remain-on-exit", "on")

    # TODO - specify layout when creating the window. If 'None', then don't select layout.
    def set_default_mode(self, mode: str) -> str:
        """Make 'mode' (PANE_MODE_SHELL, _EXEC or _WRAP) the mode of every
        later new_pane() that doesn't pass one. Panes already made keep
        theirs. Returns the previous default."""
        previous, self.default_mode = self.default_mode, _check_mode(mode)
        return previous

    def split_window(self, window: Window, pane_name=None, **pane_kwargs) -> Pane:
        """Add a new pane to the given window"""
        # Splitting with layouts that aren't 'tiled' can run into issues. Switch to tiled, split,
        # and then back to the target layout.
        if hasattr(window, "layout_str"):
            window.select_layout("tiled")

        if "target" in pane_kwargs:
            pane_kwargs["target"] = self.resolve_target_pane(pane_kwargs["target"]).id
        else:
            pane_kwargs["target"] = window.panes[-1].id

        try:
            pane = window.split_window(**pane_kwargs)
        except DeprecatedError:  # Window.split_window() was deprecated in 0.33.0
            pane = window.split(**pane_kwargs)
        self.set_pane_title(pane, pane_name)

        if hasattr(window, "layout_str"):
            window.select_layout(window.layout_str)
        return pane

    def send_keys(
        self, command: str, enter: bool = True, target_pane: Union[Pane, str] = None
    ):
        """Send the provided command string to the last created pane, or to the target_pane.

        'target_pane' should either be a pane name, pane id, or a Pane object. If None, the command
        will be sent to the most recently created pane. Raises ValueError if the resolved pane isn't
        a pane tracked/named by this session (i.e. not created via new_pane()).
        """
        if self.unnamed_window or self.unnamed_pane[self.current_window.id]:
            raise ValueError("Must create a window and pane before sending commands.")

        if target_pane:
            pane = self.resolve_target_pane(target_pane)
        else:
            pane = self.current_pane

        pane_name = self.get_pane_name(pane)
        if pane_name is None:
            raise ValueError(f"Pane {pane} is not a named pane tracked by this session.")

        logger.debug(f"Sending: {command}")
        self._type(pane, command, enter)
        if enter and command.lstrip().startswith(("lte-run ", _wrap_prefix() + " ")):
            self._run_typed[pane.id] = time.monotonic()  # see _command_running()
        self.commands.setdefault(pane_name, []).append(command)

    def resolve_target_pane(self, identifier: Union[Pane, str]) -> Pane:
        """Helper function to retrieve a Pane object from either a pane name, pane id, or a Pane
        object."""
        if isinstance(identifier, str):
            pane = self.get_pane(identifier)
            if pane is None:
                raise ValueError(f"Pane with identifier {identifier} not found.")
        else:
            self._check_pane_membership(identifier)
            pane = identifier
        return pane

    def resolve_target_window(self, identifier: Union[Window, str]) -> Window:
        """Helper function to retrieve a Window object from either a window name, window id, or a
        window object."""
        if isinstance(identifier, str):
            window = self.get_window(identifier)
            if window is None:
                raise ValueError(f"Window with identifier {identifier} not found.")
        else:
            self._check_window_membership(identifier)
            window = identifier
        return window

    def set_pane_title(self, pane: Pane, pane_name: str):
        """Set the title of a tmux pane. This is done by sending a tmux command to select the pane
        and set its title."""
        _set_pane_title(pane, pane_name)
        self.pane_names[self._full_pane_name(pane, pane_name)] = pane

    def print(self):
        """Print layout of the session, including windows and panes."""
        rep = f"Session: {self.session_name}\n"
        tab = "    "
        for window in self.windows:
            rep += f"{tab}Window: {window.name}\n"
            for pane in window.panes:
                pane_name = self.get_pane_name(pane)
                if pane_name is None:
                    continue  # not created via new_pane(), e.g. the dashboard's
                rep += f"{tab*2}Pane: {pane_name.split(':')[1]}\n"
                for cmd in self.commands.get(pane_name, []):
                    rep += f"{tab*3}{cmd}\n"
        print(rep)

    def select_window(self, target_window: Union[Window, str]) -> Window:
        """Select the given window. 'target_window' can be either the name of the window, the id of
        the window, or the Window object itself. Returns the Window object. Note that selecting a
        window will also select the most recently created pane in that window."""
        window = self.resolve_target_window(target_window)
        self.current_window = window
        self.current_pane = window.panes[-1]
        window = super().select_window(window.id)
        window.select_pane(self.current_pane.id)
        return window
    
    def select_pane(self, target_pane: Union[Pane, str]) -> Pane:
        """Select the given pane. 'target_pane' can be either the name of the pane, the id of
        the pane, or the Pane object itself. Returns the Pane object"""
        pane = self.resolve_target_pane(target_pane)
        self.current_pane = pane
        self.current_window = pane.window
        self.select_window(pane.window)
        pane = pane.window.select_pane(pane.id)
        return pane

    def select_first(self):
        """Select the first pane in the first window, after the dashboard's."""
        window = next(w for w in self.windows if w.id != self._dash_window)
        self.current_window = window
        self.current_pane = window.panes[0]
        return super().select_window(window.id).select_pane(window.panes[0].id)

    def attach(self) -> Session:
        """Attach to the tmux session"""
        try:
            return self.attach_session()
        except DeprecatedError:  # attach_session() deprecated in 0.30.0
            return super().attach()

    def _pane_mode(self, pane):
        """Panes not created through new_pane() (e.g. a window's initial pane) are
        treated as shell panes."""
        return self.pane_modes.get(pane.id, PANE_MODE_SHELL)

    def _pane_fmt(self, pane, fmt):
        """Query a tmux format string for a pane. Asks tmux directly rather than
        reading a libtmux attribute, so this works across libtmux versions and
        never returns stale cached data."""
        out = _pane_cmd(pane, "display-message", "-p", fmt).stdout
        return out[0].strip() if out else ""

    def is_running(self, target_pane: Union[Pane, str]) -> bool:
        """True while the pane's target command is still running.

        Judged on the pids the command actually spawned (see new_pane()), so
        anything else running in the pane -- e.g. a command a user typed after
        attaching -- neither masks the target's death nor is mistaken for the
        target.

        Falls back to 'does the pane have any children' only when no target
        pids were recorded, which is the case for panes not created via
        new_pane()."""
        try:
            pane = self.resolve_target_pane(target_pane)
        except ValueError:
            return False  # pane is gone (e.g. the user typed 'exit')

        if self._pane_mode(pane) == PANE_MODE_WRAP:
            run = self._wrap_runs.get(pane.id)
            if run is not None and not run.lost:
                return run.status is None
            # lost, or never connected: fall through to target-pid tracking

        targets = self.pane_targets.get(pane.id)
        if targets:
            alive = any(_proc_alive(p) for p in targets)
            if alive or self._pane_mode(pane) != PANE_MODE_EXEC:
                return alive
            # Exec mode: the OS-level pid can be reaped a beat before tmux's
            # own bookkeeping (pane_dead_status/pane_dead_signal, read by
            # exit_status()) catches up. Fall through to tmux's own flag so
            # callers never see "not running" before that status is readable.
            return not self._pane_is_dead(pane)
        if pane.id in self.pane_targets:
            return False  # recorded, but empty -- the command already exited
        if self._pane_mode(pane) == PANE_MODE_EXEC:
            return not self._pane_is_dead(pane)
        return bool(pane_child_processes(pane))

    def _pane_is_dead(self, pane) -> bool:
        """Whether tmux has recorded the pane's process as finished.

        Tests for "1" rather than against "0" on purpose: _pane_fmt() returns ""
        both for a format that expanded to nothing and for a query that failed
        (a busy or restarting server), and reading that as "dead" would make
        exit_status() go looking for a status tmux has not recorded yet and
        report 'unknown'. Treating an unreadable flag as "still running" is the
        conservative answer -- callers are already bounded by their timeouts."""
        return self._pane_fmt(pane, "#{pane_dead}") == "1"

    def exit_status(self, target_pane: Union[Pane, str]) -> ExitStatus:
        """How the pane's command finished, or an unknown ExitStatus if it is still
        running (or the pane is gone, e.g. the user typed 'exit').

        The first successful result is cached, which is required for shell panes:
        reading '$?' costs a shell command, and that command then becomes the
        shell's last one, overwriting '$?'. Call this (or stop_pane(), which calls
        it) promptly after the command goes idle.

        The whole check-then-probe-then-cache sequence is serialized under
        _status_lock: the background watcher and a caller can both land here
        for the same pane around the same moment (the whole point of the
        watcher is to probe immediately on exit), and a shell-mode probe sends
        a real command into the pane -- two concurrent probes would race to
        read '$?' and the loser would see the winner's own probe command's
        status (0) instead of the target's."""
        try:
            pane = self.resolve_target_pane(target_pane)
        except ValueError:
            return ExitStatus()

        with self._status_lock:
            cached = self._exit_status.get(pane.id)
            if cached is not None:
                return cached
            if self.is_running(pane):
                return ExitStatus()

            mode = self._pane_mode(pane)
            if mode == PANE_MODE_EXEC:
                status = self._read_exec_status(pane)
            elif mode == PANE_MODE_WRAP:
                run = self._wrap_runs.get(pane.id)
                # A lost run's status is unknowable, and a pending one has not
                # run yet. No run at all means the wrapper never connected, so
                # this is effectively a shell pane.
                if run is not None or pane.id in self._wrap_pending:
                    status = ExitStatus()
                else:
                    status = self._probe_shell_status(pane)
            else:
                status = self._probe_shell_status(pane)

            if status.known:
                self._exit_status[pane.id] = status
            return status

    def _read_exec_status(self, pane, timeout=2.0, poll_interval=0.05):
        """tmux records a dead pane's outcome itself: exactly one of
        pane_dead_status (normal exit) / pane_dead_signal (killed) is set.

        Polls briefly because tmux's own detection can lag a beat behind the
        raw OS-level exit that is_running() acts on -- see its exec-mode note."""
        deadline = time.time() + timeout
        while True:
            sig = self._pane_fmt(pane, "#{pane_dead_signal}")
            if sig:
                return ExitStatus(signal=int(sig))
            code = self._pane_fmt(pane, "#{pane_dead_status}")
            if code:
                return ExitStatus(code=int(code))
            if time.time() >= deadline:
                break
            time.sleep(poll_interval)

        if self._pane_is_dead(pane):
            # Known tmux issue (seen on 3.3a, reproduced with raw tmux): the
            # pane's pty closes, so pane_dead=1, but tmux never reaps the
            # process. It stays a zombie, pane_dead_status/_signal are never
            # set, and since only the parent can read a zombie's status, it is
            # lost for good. Rare and timing-dependent; seen on both signal
            # deaths and normal exits. Root cause unknown.
            logger.warning(
                f"Pane {self.get_pane_name(pane) or pane.id} is dead but tmux never "
                f"recorded its exit status (known intermittent tmux issue in exec mode)."
            )
        return ExitStatus()

    def _probe_shell_status(self, pane, timeout=2.0, poll_interval=0.1):
        """Ask a shell pane's still-live shell for '$?' and scrape it back out of
        the pane's text.

        '-J' joins wrapped lines, without which a narrow pane could split the
        marker line and defeat the match. The regex is anchored to the start of a
        line so the echoed command line itself (which sits after a prompt) is not
        mistaken for the result."""
        self._type(pane, f"echo {_EXIT_MARKER}:$?:", enter=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(poll_interval)
            text = "\n".join(_pane_cmd(pane, "capture-pane", "-p", "-J").stdout)
            hits = _EXIT_RE.findall(text)
            if hits:
                raw = int(hits[-1])
                # Shells report a signal death as 128+N. This cannot be told apart
                # from a genuine 'exit 130', hence inferred=True.
                if raw > 128:
                    return ExitStatus(signal=raw - 128, inferred=True)
                return ExitStatus(code=raw)
        logger.warning(
            f"Could not read '$?' from pane {self.get_pane_name(pane) or pane.id} "
            f"within {timeout}s."
        )
        return ExitStatus()

    def wait_for_pane_idle(
        self,
        target_pane: Union[Pane, str],
        timeout: float = 10.0,
        poll_interval: float = 0.2,
    ) -> bool:
        """Poll until the pane's command is no longer running, or until 'timeout'
        seconds have elapsed. Returns True if it became idle, False on timeout.
        Only polls -- never sends a signal."""
        pane = self.resolve_target_pane(target_pane)
        end_time = time.time() + timeout
        while self.is_running(pane):
            if time.time() >= end_time:
                return False
            time.sleep(poll_interval)
        return True

    def stop_pane(
        self,
        target_pane: Union[Pane, str],
        sigint_timeout: float = 10.0,
        sigterm_timeout: float = 5.0,
        sigkill_timeout: float = 2.0,
        poll_interval: float = 0.2,
    ) -> bool:
        """Gracefully stop whatever is running in a pane, escalating
        SIGINT -> SIGTERM -> SIGKILL. Returns True if the pane ended up idle.

        A caller escalating past SIGINT should treat that as a sign that whatever
        was running did not shut down cleanly (e.g. a recorder likely failed to
        finalize its metadata). Call exit_status() afterwards for the outcome; it is
        captured here while it is still readable."""
        pane = self.resolve_target_pane(target_pane)
        pane_name = self.get_pane_name(pane) or pane.id

        if not self.is_running(pane):
            self.exit_status(pane)
            return True

        # SIGINT goes to the pane's foreground process group, like a Ctrl-C
        # typed there (a wrap pane's lte-run delivers it to the same group),
        # so the process can shut down gracefully. SIGTERM and SIGKILL go to
        # the processes directly; SIGKILL allows no cleanup at all.
        steps = (("SIGINT", sigint_timeout), ("SIGTERM", sigterm_timeout),
                 ("SIGKILL", sigkill_timeout))
        for i, (sig, timeout) in enumerate(steps):
            if i:
                logger.warning(f"Pane {pane_name} still running after {steps[i - 1][0]} "
                               f"({steps[i - 1][1]}s). Escalating to {sig}.")
            self._stopped_by[pane.id] = sig
            if sig == "SIGINT":
                if not self._wrap_signal(pane, signal.SIGINT):
                    _pane_cmd(pane, "send-keys", "C-c")
            else:
                self._signal_pane_processes(pane, getattr(signal, sig))
            if self.wait_for_pane_idle(pane, timeout=timeout, poll_interval=poll_interval):
                self.exit_status(pane)
                return True
        logger.warning(f"Pane {pane_name} still running after SIGKILL ({sigkill_timeout}s).")
        return False

    def _signal_pane_processes(self, pane, sig):
        """Send SIGTERM or SIGKILL to everything the pane runs.

        Shell pane: the shell's descendants; the shell must survive, or the
        pane dies with it. Exec pane: the pane process too, as it is the
        command. Wrap pane: lte-run signals its target group; failing that,
        the recorded targets, never the wrapper (it holds the status)."""
        if self._pane_mode(pane) == PANE_MODE_WRAP:
            if self._wrap_signal(pane, sig):
                return
            targets = [p for p in self.pane_targets.get(pane.id, ()) if _proc_alive(p)]
        else:
            targets = list(pane_child_processes(pane))
            if self._pane_mode(pane) == PANE_MODE_EXEC:
                try:
                    targets.append(psutil.Process(int(pane.pane_pid)))
                except (psutil.NoSuchProcess, ValueError, TypeError):
                    pass
        for proc in targets:
            try:
                proc.send_signal(sig)
            except psutil.NoSuchProcess:
                pass

    def respawn_pane(self, target_pane: Union[Pane, str], command: Optional[str] = None) -> Pane:
        """Rerun a pane's command in place, replacing whatever is running.

        Defaults to the pane's original command. Works in every mode: an exec
        pane's process is replaced via respawn-pane, a shell or wrap pane's
        command is stopped (if still running) and typed in again.

        This is the only correct way to rerun a tracked pane. Sending the command
        again with send_keys() does restart it as far as tmux is concerned, but
        leaves the library describing the previous run -- the exit status is
        cached (see exit_status()) and the recorded target pid is the old, dead
        process."""
        pane = self.resolve_target_pane(target_pane)
        mode = self._pane_mode(pane)

        if command is None:
            command = self.commands[self.get_pane_name(pane)][0]
        self._exit_status.pop(pane.id, None)
        if self._reader is not None:
            self._reader.new_run(pane.id)

        if mode == PANE_MODE_EXEC:
            self._stopped_by.pop(pane.id, None)
            _pane_cmd(pane, "respawn-pane", "-k", *env_args(self._pane_env.get(pane.id)),
                      command)
            pane.refresh()  # pane_pid is a cached field; respawn-pane gave it a new one
        else:
            if self.is_running(pane):
                self.stop_pane(pane)
                # stop_pane() captures how the OLD run ended; that is not what
                # the caller is about to ask about.
                self._exit_status.pop(pane.id, None)
            self._stopped_by.pop(pane.id, None)  # that was the old run
            if mode == PANE_MODE_WRAP:
                self._send_wrapped(pane, command)
            else:
                self._type(pane, command, enter=True)

        self.commands[self.get_pane_name(pane)] = [command]

        # Target-pid tracking is per-run: the old pid is gone the moment the
        # command is replaced, so is_running()/the watcher must pick up the new
        # one, same as a fresh new_pane().
        self.pane_targets[pane.id] = self._capture_target_pids(pane, mode, 1.0)
        self._watched_panes.discard(pane.id)
        self._refresh_pane_title(pane)
        if self.title_status or self.status_file is not None:
            self.watch_pane(pane)
        self._write_status_file()
        return pane

    # =============== Exit events ===============

    def _ensure_watcher(self) -> _PaneWatcher:
        if self._watcher is None:
            self._watcher = _PaneWatcher(
                self._on_target_exit, self._on_wrap_message, self._on_wrap_closed)
            self._watcher.start()
        return self._watcher

    def watch_pane(self, target_pane: Union[Pane, str], on_exit=None):
        """Begin watching a pane's target command, firing 'on_exit(PaneStatus)'
        when it exits for any reason (normal exit, crash, or a signal --
        including a Ctrl-C typed by a user who attached to the session).

        Callbacks run on the watcher thread and should return promptly. Safe to
        call more than once (including with on_exit=None, which just arms the
        internal title/status-file tracking) to register several callbacks."""
        pane = self.resolve_target_pane(target_pane)
        pane_id = pane.id

        if self._pane_mode(pane) == PANE_MODE_WRAP:
            run = self._wrap_runs.get(pane_id)
            if run is None and pane_id in self._wrap_pending:
                # Typed with enter=False: 'started' will create the run.
                with self._status_lock:
                    if on_exit is not None:
                        self._exit_callbacks.setdefault(pane_id, []).append(on_exit)
                self._watched_panes.add(pane_id)
                return
            if run is not None and not run.lost:
                # Wrap panes are driven by the 'exited' message, not pidfds.
                # Check-and-register under the lock its handler takes, so the
                # callback can neither be missed nor fire twice.
                with self._status_lock:
                    finished = run.status is not None
                    if not finished and on_exit is not None:
                        self._exit_callbacks.setdefault(pane_id, []).append(on_exit)
                self._watched_panes.add(pane_id)
                if finished and on_exit is not None:
                    on_exit(self._build_pane_status(pane_id, pane))  # just this one
                return

        if on_exit is not None:
            with self._status_lock:
                self._exit_callbacks.setdefault(pane_id, []).append(on_exit)

        if pane_id in self._watched_panes:
            return
        self._watched_panes.add(pane_id)

        targets = self.pane_targets.get(pane_id)
        if targets:
            self._ensure_watcher().watch(pane_id, targets)
            return

        if not self.is_running(pane):
            # Either recorded-but-empty target pids (command exited before
            # capture) or a pane not created via new_pane() that's simply
            # idle -- resolve now rather than waiting on an event source that
            # doesn't exist.
            self._on_target_exit(pane_id)
            return

        logger.warning(
            f"watch_pane(): no target pid recorded for pane "
            f"{self.get_pane_name(pane) or pane_id}; it was not created via "
            f"new_pane(), or its command exited before capture. on_exit will not fire."
        )

    def unwatch_pane(self, target_pane: Union[Pane, str]):
        """Stop firing on_exit callbacks for a pane. Does not affect
        is_running()/exit_status() -- only the push-callback mechanism."""
        pane = self.resolve_target_pane(target_pane)
        pane_id = pane.id
        with self._status_lock:
            self._exit_callbacks.pop(pane_id, None)
        self._watched_panes.discard(pane_id)
        if self._watcher is not None:
            self._watcher.unwatch(pane_id)

    def wait_for_exit(self, target_pane: Union[Pane, str], timeout: Optional[float] = None) -> ExitStatus:
        """Block until the pane's target command exits, returning its
        ExitStatus. Returns an unknown ExitStatus on timeout."""
        pane = self.resolve_target_pane(target_pane)
        done = threading.Event()
        self.watch_pane(pane, lambda status: done.set())
        if not done.wait(timeout):
            return ExitStatus()
        return self.exit_status(pane)

    def stop_watching(self):
        """Stop the background watcher thread, if one was started. Called
        automatically by kill_session()/kill(); safe to call even if no
        watcher was ever started."""
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def _on_target_exit(self, pane_id):
        """Fired (from the watcher thread, or synchronously from
        watch_pane() for an already-finished pane) the moment a pane's
        target pids have all exited. Captures the exit status immediately --
        this is what lets a shell-mode pane's '$?' be read before anything
        else runs in that pane -- then updates the title/status file and
        invokes callbacks, each isolated so one bad callback can't kill the
        watcher thread."""
        pane = self.get_pane(pane_id)
        if pane is not None:
            self.exit_status(pane)  # captures + caches, if not already cached
            self._refresh_pane_title(pane)

        with self._status_lock:
            callbacks = list(self._exit_callbacks.get(pane_id, []))

        status = self._build_pane_status(pane_id, pane)
        self._write_status_file()

        for cb in callbacks:
            try:
                cb(status)
            except Exception:
                logger.exception(f"on_exit callback for pane {pane_id} raised")

    # =============== Wrap mode ===============

    def _on_wrap_message(self, conn, msg):
        """Watcher thread. 'started' for a tracked pane begins a new run --
        whether the library launched it or a user reran it by hand."""
        if "cmd" in msg:
            self._on_dash_command(msg.get("cmd"), msg.get("pane"))
            return
        event = msg.get("event")
        if event == "started":
            pane_id = msg.get("pane")
            if pane_id not in self.pane_modes:
                logger.debug(f"lte-run started in untracked pane {pane_id}; ignoring")
                return
            if msg.get("version") != WRAP_PROTOCOL_VERSION:
                logger.warning(
                    f"lte-run protocol {msg.get('version')} != {WRAP_PROTOCOL_VERSION}")
            conn.pane_id = pane_id
            try:
                targets = (psutil.Process(int(msg["target_pid"])),)
            except (psutil.NoSuchProcess, KeyError, ValueError, TypeError):
                targets = ()
            # A pidfd watch left over from shell mode or a lost run would fire
            # on_exit a second time. From here on, 'exited' drives this pane.
            if self._watcher is not None:
                self._watcher.unwatch(pane_id)
            with self._status_lock:
                self._wrap_pending.discard(pane_id)
                self._run_typed.pop(pane_id, None)
                self._exit_status.pop(pane_id, None)  # a new run
                self._stopped_by.pop(pane_id, None)
                self._wrap_runs[pane_id] = _WrapRun(
                    conn, msg.get("wrapper_pid"), msg.get("target_pid"), msg.get("argv", []))
                self.pane_modes[pane_id] = PANE_MODE_WRAP  # also upgrades a shell pane
                self.pane_targets[pane_id] = targets
            self._record_run_command(pane_id, msg.get("argv"))
            if self._reader is not None:
                self._reader.new_run(pane_id)  # its --tee may have moved the pane's file
            self._wrap_started.setdefault(pane_id, threading.Event()).set()
            pane = self.get_pane(pane_id)
            if pane is not None:
                self._refresh_pane_title(pane)
            self._write_status_file()

        elif event == "exited":
            run = self._wrap_runs.get(conn.pane_id)
            if run is None or run.conn is not conn:
                return  # untracked pane, or superseded by a newer run
            status = ExitStatus(code=msg.get("code"), signal=msg.get("signal"))
            with self._status_lock:
                run.status = status
                self._exit_status[conn.pane_id] = status
            self._on_target_exit(conn.pane_id)

    def _on_dash_command(self, cmd, pane_id):
        """Watcher thread. Commands from lte-dash, run on a thread of their
        own: stop_pane() blocks through its escalation timeouts, and the
        watcher has to keep serving the 'exited' messages it waits for."""
        pane_actions = {"stop": self.stop_pane, "respawn": self.respawn_pane}
        session_actions = {"stop_all": self.stop_all, "shutdown": self._shutdown,
                           "close": self._close_requested}
        if cmd in pane_actions and pane_id in self.pane_modes:
            action = lambda: pane_actions[cmd](pane_id)
        elif cmd in session_actions:
            action = session_actions[cmd]
        else:
            logger.debug(f"ignoring lte-dash command {cmd!r} for pane {pane_id!r}")
            return

        def run():
            try:
                action()
            except Exception:
                logger.exception(f"lte-dash {cmd} failed")

        threading.Thread(target=run, daemon=True, name=f"lte-dash-{cmd}").start()

    def _on_wrap_closed(self, conn):
        """Watcher thread. EOF without 'exited' means the wrapper died."""
        run = self._wrap_runs.get(conn.pane_id)
        if run is None or run.conn is not conn or run.status is not None:
            return  # normal: the wrapper exits right after 'exited'
        run.lost = True
        logger.warning(
            f"lte-run in pane {self._name_for_pane_id(conn.pane_id) or conn.pane_id} exited "
            f"without reporting (killed?); its command's exit status will be unknown."
        )
        # The target may outlive its wrapper. Fall back to watching its pid so
        # on_exit still fires when it ends.
        self._watched_panes.discard(conn.pane_id)
        try:
            self.watch_pane(conn.pane_id)
        except ValueError:
            pass  # the pane itself is gone

    def _record_run_command(self, pane_id, argv):
        """A run typed by hand (up-arrow and edit, or a new lte-run) becomes
        the pane's command: what the status shows and respawn_pane() reruns."""
        name = self._name_for_pane_id(pane_id)
        if not name or not argv:
            return
        # _send_wrapped() wraps shell operators in 'bash -c': unwrap it.
        if len(argv) == 3 and argv[:2] == ["bash", "-c"]:
            command = argv[2]
        else:
            command = shlex.join(argv)
        cmds = self.commands.setdefault(name, [command])
        cmds[0] = command

    def _send_wrapped(self, pane, command, enter=True):
        """Type 'command' into a shell pane under lte-run."""
        target = command
        if any(op in command for op in _SHELL_OPERATORS):
            target = f"bash -c {shlex.quote(command)}"
        # Armed before sending, so a fast 'started' can't be missed. The mode
        # must be set first too, or 'started' would be ignored as untracked.
        self._wrap_started[pane.id] = threading.Event()
        self.pane_modes[pane.id] = PANE_MODE_WRAP
        tee = self._pane_tee.get(pane.id)
        options = f"--tee {shlex.quote(tee)} -- " if tee else ""
        self._type(pane, f"{_wrap_prefix()} {options}{target}", enter)

    def _wrap_signal(self, pane, sig) -> bool:
        """Ask the pane's lte-run to signal its target. False if there is no
        live connection to ask (not a wrap pane, finished, or lost)."""
        run = self._wrap_runs.get(pane.id)
        if run is None or run.lost or run.status is not None:
            return False
        return run.conn.send(cmd="signal", signal=int(sig))

    def stop_all(self, **stop_kwargs) -> dict:
        """Stop every running pane created via new_pane(), in parallel, each
        with stop_pane()'s SIGINT -> SIGTERM -> SIGKILL escalation, so the
        whole thing takes as long as the slowest pane. 'stop_kwargs' go to
        stop_pane() (e.g. sigint_timeout). Returns {pane name: stopped}.

        Killing the session instead (kill_session(), or tmux's own
        kill-session / closing the last window) only sends SIGHUP."""
        panes = [p for p in (self.get_pane(i) for i in list(self.pane_targets))
                 if p is not None and self.is_running(p)]
        results = {}

        def stop(pane):
            name = self.get_pane_name(pane) or pane.id
            try:
                results[name] = self.stop_pane(pane, **stop_kwargs)
            except Exception:
                logger.exception(f"stop_all: stopping {name} failed")
                results[name] = False

        threads = [threading.Thread(target=stop, args=(p,), daemon=True) for p in panes]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for name, stopped in results.items():
            status = self.exit_status(name) if stopped else "still running"
            logger.info(f"stop_all: {name}: {status}")
        return results

    def _shutdown(self):
        """The 'shutdown' command (tlaunch's dashboards): close()."""
        self.close()

    # =============== Output: capture, search, watch ===============

    def _type(self, pane, text, enter=True):
        """Type 'text' into the pane, noted so its echo isn't taken as output."""
        self._typed.setdefault(pane.id, collections.deque(maxlen=32)).append(text)
        if self._reader is not None:
            self._reader.expect_echo(pane.id, text)
        _type(pane, text, enter)

    def _new_pane_file(self, name) -> Optional[str]:
        """The file new_pane()'s pane is captured to, if any."""
        capture, window = self._pending_capture, self.current_window
        if capture is False:
            return None
        if capture and capture is not True:
            return os.path.abspath(capture)
        log = f"{_safe_name(window.name)}.{_safe_name(name)}.log"
        if capture is True:
            return os.path.join(self._output_dir(keep=True), log)
        scope = self._scope_captures.get(window.id) or self._scope_captures.get(None)
        if scope is not None:
            return os.path.join(scope.directory, log)
        if self._pending_output:
            return os.path.join(self._output_dir(keep=False), log)
        return None

    def _claimed(self, pane, reused, capture):
        """_claim_pane() has the pane: pipe it, arm new_pane(on_output=)."""
        if capture is not None and (reused or self._pipe_info(pane)[2] != capture):
            # A reused pane predates the hook; a split one only if it failed.
            self._pipe(pane, capture)
        elif self._pending_capture is False and self._capture_hooked:
            self._unpipe(pane)  # new_pane(capture=False): the scope's hook piped it
        pending, self._pending_output = self._pending_output, None
        for pattern, callback in (pending or {}).items():
            self.watch_output(pane, pattern, callback, since="start")

    def _output_dir(self, keep) -> str:
        """Where capture files go by default: kept ones under the temp dir,
        a watch's own in a dir deleted with the session (unless keep_output)."""
        if keep:
            path = os.path.join(tempfile.gettempdir(), f"lte_output_{os.getuid()}",
                                _safe_name(self.session_name))
            os.makedirs(path, exist_ok=True)
            return path
        if self._watch_dir is None:
            self._watch_dir = tempfile.mkdtemp(prefix=f"lte_watch_{_safe_name(self.session_name)}_")
        return self._watch_dir

    def _log_name(self, pane, window_name=None) -> str:
        """'window.pane.log' for a named pane, else 'window.%id.log'."""
        name = self._name_for_pane_id(pane.id)
        if name:
            window, _, pane_name = name.partition(":")
            return f"{_safe_name(window)}.{_safe_name(pane_name)}.log"
        window = window_name or self._pane_fmt(pane, "#{window_name}")
        return f"{_safe_name(window)}.{pane.id}.log"

    def _ensure_capture_hooks(self):
        if not self._capture_hooked:
            for hook in _PIPE_HOOKS:
                self._tmux_run("set-hook", "-t", self.id, hook, _PIPE_HOOK)
            self._capture_hooked = True

    def _arm_pipe(self, path):
        """The next pane made in the session is piped to 'path', by the hook."""
        self._ensure_capture_hooks()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._tmux_run("set", "-t", self.id, "@lte_pipe_next", path, ";",
                       "set", "-t", self.id, "@lte_pipe_next_cmd", _cat_to(path), ";",
                       "set", "-t", self.id, "@lte_pipe_next_from", str(_file_size(path)))

    def _disarm_pipe(self):
        for opt in ("@lte_pipe_next", "@lte_pipe_next_cmd", "@lte_pipe_next_from"):
            self._tmux_run("set", "-q", "-u", "-t", self.id, opt)

    def _pipe_info(self, pane):
        """(piped, lte-run's --tee has it, the pane's file or '')."""
        return self._pane_pipe_info(pane.id)

    def _pipe(self, pane, path, quiet=False) -> bool:
        """Pipe the pane's output to 'path' (appended), replacing a capture
        pipe of ours. False when the pipe is lte-run's --tee, for this run,
        or another tool's (tmux has one per pane)."""
        return bool(self._pipe_many({pane.id: path}, quiet))

    def _pipe_many(self, paths, quiet=False) -> set:
        """_pipe() for {pane id: path}, in one tmux call. The pane ids piped."""
        if len(paths) == 1:
            pane_id = next(iter(paths))
            infos = {pane_id: self._pane_pipe_info(pane_id)}
        else:
            infos = self._all_pipe_info()
        args, piped, stale = [], set(), []
        for pane_id, path in paths.items():
            is_piped, tee, current = infos.get(pane_id, (False, False, ""))
            if is_piped and (tee or not current):
                if not current and not quiet:
                    logger.warning(f"pane {self._name_for_pane_id(pane_id) or pane_id} is already "
                                   "piped by something else; not capturing it")
                continue
            piped.add(pane_id)
            if is_piped and current == path:
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Checked again in tmux, with the pipe: lte-run may pipe it meanwhile.
            args += guarded_pipe(pane_id, _cat_to(path), {
                PIPE_FILE: path, PIPE_FROM: str(_file_size(path)),
                PIPE_ID: f"lib-{time.monotonic_ns()}"}) + [";"]
            if current and current != path and current.endswith(f".{pane_id}.log"):
                stale.append(current)
        if args:
            for line in self._tmux_run(*args[:-1]):
                pane_id, _, ok = line.partition(" ")
                if pane_id in piped and ok != "1":
                    piped.discard(pane_id)  # lte-run --tee got there first
                    logger.debug(f"pane {pane_id}: piped by lte-run meanwhile")
        for path in stale:
            # The hook's file for a pane only now named (new_window()'s first
            # pane): just its shell's prompt.
            try:
                os.remove(path)
            except OSError:
                pass
        if args and self._reader is not None:
            self._reader.resync()
        return piped

    def _pane_pipe_info(self, pane_id):
        out = self._tmux_run("display", "-p", "-t", pane_id,
                             f"#{{pane_pipe}}\t#{{{PIPE_TEE_OPTION}}}\t#{{{PIPE_FILE}}}")
        piped, tee, path = ((out[0] if out else "").split("\t") + ["", "", ""])[:3]
        return piped == "1", tee == "1", path

    def _all_pipe_info(self):
        """{pane id: (piped, lte-run's --tee has it, file)} for the session."""
        out = self._tmux_run("list-panes", "-s", "-t", self.id, "-F",
                             f"#{{pane_id}}\t#{{pane_pipe}}\t#{{{PIPE_TEE_OPTION}}}\t#{{{PIPE_FILE}}}")
        infos = {}
        for line in out:
            pane_id, piped, tee, path = (line.split("\t") + ["", "", "", ""])[:4]
            infos[pane_id] = (piped == "1", tee == "1", path)
        return infos

    def _unpipe(self, pane):
        """Stop a capture pipe and forget its file (it held a moment's output)."""
        piped, tee, current = self._pipe_info(pane)
        if piped and current and not tee:
            self._tmux_run(*guarded_unpipe(pane.id, f"set -p -u -t {pane.id} {PIPE_FILE}"))
            try:
                os.remove(current)
            except OSError:
                pass

    def _resolve_scope(self, target):
        """('session', None), ('window', Window) or ('pane', Pane)."""
        if target is None or target is self:
            return "session", None
        if isinstance(target, Pane):
            return "pane", self.resolve_target_pane(target)
        if isinstance(target, Window):
            return "window", self.resolve_target_window(target)
        if isinstance(target, str):
            if target.startswith("%") or ":" in target:
                return "pane", self.resolve_target_pane(target)
            return "window", self.resolve_target_window(target)
        raise TypeError(f"not a pane, window or session: {target!r}")

    def capture(self, target=None, path: Optional[str] = None, *, keep: bool = True) -> Capture:
        """Save the output of a pane, a window or the whole session (None)
        to files: tmux pipes it, raw, from now on. A window or session
        capture also covers panes made in it later, by the library or by
        hand, from their first byte.

        'path': the file for a pane, the directory for a window or session
        (files 'window.pane.log'); by default under
        $TMPDIR/lte_output_<uid>/<session>. 'keep=False' files are deleted
        with the session unless 'keep_output' is set. lte-run --tee takes a
        pane's pipe over for its run. Returns a Capture: path(), mark(),
        search(), text(), stop(). See docs/output-match.md."""
        kind, obj = self._resolve_scope(target)
        if kind == "pane":
            file = os.path.abspath(path) if path else \
                os.path.join(self._output_dir(keep), self._log_name(obj))
            cap = Capture(self, obj.id, file, keep)
            self._pipe(obj, file)
            return cap
        directory = os.path.abspath(path) if path else self._output_dir(keep)
        os.makedirs(directory, exist_ok=True)
        self._ensure_capture_hooks()
        scope = ["-t", self.id] if kind == "session" else ["-w", "-t", obj.id]
        esc = directory.replace("#", "##")
        self._tmux_run("set", *scope, "@lte_pipe", f"{esc}/{_PIPE_NAME}", ";",
                       "set", *scope, "@lte_pipe_cmd",
                       f"exec cat >> {_tmux_literal(shlex.quote(directory + '/'))}{_PIPE_NAME}")
        cap = Capture(self, None if kind == "session" else obj.id, directory, keep)
        self._scope_captures[cap.target] = cap
        windows = [obj] if kind == "window" else [
            w for w in self.windows if w.id not in self._scope_captures]
        self._pipe_many({pane.id: os.path.join(directory, self._log_name(pane, window.name))
                         for window in windows for pane in window.panes
                         if pane.id != self._dash_sidebar},  # the pane on view there is captured
                        quiet=True)
        return cap

    def _no_capture(self, window_id):
        """Keep a window of ours (the dashboard) out of session captures."""
        if not self._capture_hooked:
            return
        self._tmux_run("set", "-w", "-t", window_id, "@lte_pipe", "")
        for pane_id, (window, piped, path, _, _) in self._piped_panes().items():
            if window == window_id and piped:
                self._tmux_run(*guarded_unpipe(pane_id, f"set -p -u -t {pane_id} {PIPE_FILE}"))
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _stop_capture(self, cap):
        """Capture.stop(): unpipe its panes; new panes aren't piped."""
        files = self._output_files(cap.target)
        if not cap._is_pane:
            if self._scope_captures.get(cap.target) is cap:
                del self._scope_captures[cap.target]
            scope = ["-t", self.id] if cap.target is None else ["-w", "-t", cap.target]
            for opt in ("@lte_pipe", "@lte_pipe_cmd"):
                self._tmux_run("set", "-q", "-u", *scope, opt)
        for pane_id, path in files.items():
            mine = path == cap.path_ if cap._is_pane else os.path.dirname(path) == cap.path_
            tee = self._tmux_run("display", "-p", "-t", pane_id, f"#{{{PIPE_TEE_OPTION}}}")
            if mine and tee != ["1"]:  # lte-run's --tee closes its own
                self._tmux_run(*guarded_unpipe(pane_id))

    def _piped_panes(self) -> dict:
        """{pane id: (window id, piped now, file, where the capture began,
        pipe id)} for the session's panes that have a file."""
        out = self._tmux_run("list-panes", "-s", "-t", self.id, "-F",
                             f"#{{pane_id}}\t#{{window_id}}\t#{{pane_pipe}}\t"
                             f"#{{{PIPE_FILE}}}\t#{{{PIPE_FROM}}}\t#{{{PIPE_ID}}}")
        panes = {}
        for line in out:
            pane_id, window_id, piped, path, start, pipe = (line.split("\t") + [""] * 6)[:6]
            if path:
                panes[pane_id] = (window_id, piped == "1", path,
                                  int(start) if start.isdigit() else 0, pipe)
        return panes

    def _output_files(self, target=None) -> dict:
        """{pane id: file} in scope: a pane id, a window id, or None (all)."""
        return {p: path for p, (w, _, path, _, _) in self._piped_panes().items()
                if target is None or target in (p, w)}

    def _output_reader(self) -> OutputReader:
        if self._reader is None or not self._reader.alive:
            self._reader = OutputReader(
                self._piped_panes, self._name_for_pane_id,
                lambda pane_id: list(self._typed.get(pane_id, ())),
                ignore=re.compile(re.escape(_EXIT_MARKER)))
        return self._reader

    @staticmethod
    def _compile(pattern, regex, flags):
        if isinstance(pattern, re.Pattern):
            return pattern
        return re.compile(pattern if regex else re.escape(pattern), flags)

    def _scrollback(self, pane):
        """What the pane shows and keeps, as plain lines, without the
        commands typed into it."""
        typed = [t.strip() for t in self._typed.get(pane.id, ()) if t.strip()]
        for line in _pane_cmd(pane, "capture-pane", "-p", "-J", "-S", "-").stdout:
            line = line.rstrip()
            if line.strip() and _EXIT_MARKER not in line and \
                    not any(line.endswith(t) for t in typed):
                yield line

    def output_path(self, target_pane: Union[Pane, str]) -> Optional[str]:
        """The file the pane's output goes (or last went) to: a capture's,
        or lte-run --tee's. None if it was never captured."""
        pane = self.resolve_target_pane(target_pane)
        return self._pipe_info(pane)[2] or None

    def output_mark(self, target_pane: Union[Pane, str]) -> int:
        """Where the pane's file ends now: pass as search_output(since=)."""
        path = self.output_path(target_pane)
        return _file_size(path) if path else 0

    def _file_lines(self, pane_id, path, since):
        """(offset, line) of plain text from 'path' at 'since', without
        the commands typed into the pane and our probes."""
        try:
            with open(path, "rb") as f:
                f.seek(since or 0)
                data = f.read()
        except OSError:
            return
        typed = [t.strip() for t in self._typed.get(pane_id, ()) if t.strip()]
        for off, line in plain_lines(data):
            if line.strip() and _EXIT_MARKER not in line and \
                    not any(line.rstrip().endswith(t) for t in typed):
                yield (since or 0) + off, line

    def search_output(self, target, pattern, since=None, *, regex: bool = True,
                      flags: int = 0) -> list:
        """[OutputMatch] for the lines of a pane's, window's or the
        session's (None) captured output that match 'pattern' (re.search on
        plain text). 'since': an offset, or {pane id: offset}, from
        output_mark() / Capture.mark(). A pane never captured is searched in
        what tmux keeps (scrollback)."""
        kind, obj = self._resolve_scope(target)
        rx = self._compile(pattern, regex, flags)
        files = self._output_files(None if kind == "session" else obj.id)
        now = time.time()
        if kind == "pane" and obj.id not in files:
            name = self._name_for_pane_id(obj.id)
            return [OutputMatch(obj.id, name, rx.pattern, m, line, now)
                    for line in self._scrollback(obj) for m in [rx.search(line)] if m]
        found = []
        for pane_id, path in files.items():
            begin = since.get(pane_id, 0) if isinstance(since, dict) else since
            name = self._name_for_pane_id(pane_id)
            for off, line in self._file_lines(pane_id, path, begin):
                m = rx.search(line)
                if m:
                    found.append(OutputMatch(pane_id, name, rx.pattern, m, line, now, off))
        return found

    def output_text(self, target_pane: Union[Pane, str], since=None) -> str:
        """The pane's captured output as plain text (from 'since', an
        output_mark()); its scrollback if never captured."""
        pane = self.resolve_target_pane(target_pane)
        path = self.output_path(pane)
        if not path:
            return "\n".join(self._scrollback(pane))
        return "\n".join(line for _, line in self._file_lines(pane.id, path, since))

    def watch_output(self, target, pattern, callback=None, *,
                     once: bool = False, regex: bool = True, flags: int = 0,
                     since: str = "now", skip_echo: bool = True) -> OutputWatch:
        """Match each line a pane, a window or the session (None) prints
        against 'pattern' (re.search on plain text: colors and other escapes
        dropped), calling 'callback(OutputMatch)' on each match, on the
        reader's thread: keep it short. A window or session watch covers
        panes made later too.

        Panes not yet captured are captured (keep=False) from now on.
        'regex=False' matches a plain substring. 'once' stops after the
        first match. 'since="start"' also matches what was printed already:
        the capture so far, or the scrollback of a pane only captured now.
        Commands the library types are not output ('skip_echo'). Works in
        every pane mode. See docs/output-match.md."""
        if since not in ("now", "start"):
            raise ValueError(f"since must be 'now' or 'start', not {since!r}")
        kind, obj = self._resolve_scope(target)
        rx = self._compile(pattern, regex, flags)
        scroll = []
        if kind == "pane":
            target_id, name = obj.id, self._name_for_pane_id(obj.id)
            piped, _, current = self._pipe_info(obj)
            if piped and not current:
                raise RuntimeError(f"pane {name or obj.id} is piped by something else "
                                   "(tmux has one pipe per pane): can't watch it")
            if not piped:
                if since == "start":
                    scroll = list(self._scrollback(obj))
                self.capture(obj, keep=False)
        else:
            target_id = None if kind == "session" else obj.id
            name = None if kind == "session" else obj.name
            if target_id not in self._scope_captures and None not in self._scope_captures:
                self.capture(obj, keep=False)
        watch = OutputWatch(target_id, name, rx, callback, once, skip_echo)
        now = time.time()
        for line in scroll:
            if watch.done:
                break
            watch._offer(line, now, target_id, name)
        self._output_reader().add(watch, since="start" if since == "start" and not scroll else None)
        return watch

    def unwatch_output(self, target=None):
        """Cancel every output watch on a pane, a window or the session (None)."""
        kind, obj = self._resolve_scope(target)
        if self._reader is not None:
            self._reader.remove_target(None if kind == "session" else obj.id)

    def wait_for_output(self, target, pattern, timeout: Optional[float] = None, *,
                        since: str = "start", regex: bool = True,
                        flags: int = 0) -> OutputMatch:
        """Block until a pane (or a window, or the session) prints a line
        matching 'pattern' (see watch_output()); by default what it printed
        already counts too. Raises TimeoutError, or for a pane PaneExited if
        its command ends first."""
        kind, obj = self._resolve_scope(target)
        watch = self.watch_output(obj, pattern, once=True, regex=regex, flags=flags,
                                  since=since)
        try:
            return self._await(watch, obj if kind == "pane" else None, timeout)
        finally:
            watch.cancel()

    def expect(self, target, pattern, timeout: Optional[float] = 10, *,
               regex: bool = True, flags: int = 0) -> "OutputExpect":
        """A context manager: watch from entry, wait at exit. For output
        that answers an action, without missing a fast answer:

            with sess.expect("main:web", r"GET /health") as e:
                requests.get(url)
            e.match.line

        Raises like wait_for_output() at exit."""
        return OutputExpect(self, target, pattern, timeout, regex, flags)

    def _await(self, watch, pane, timeout):
        deadline = None if timeout is None else time.monotonic() + timeout
        ended = None  # when the command was first seen not running
        what = watch.name or watch.target or "session"
        while True:
            step = 0.2 if deadline is None else min(0.2, deadline - time.monotonic())
            if step <= 0:
                raise TimeoutError(f"{what}: no line matching "
                                   f"{watch.regex.pattern!r} within {timeout}s")
            match = watch.wait(step)
            if match is not None:
                return match
            if watch.done:
                raise RuntimeError(f"{what}: output watch closed (session gone?)")
            if pane is not None and not self._command_running(pane):
                # Output trails the exit event by a little: give it a read or two.
                ended = ended or time.monotonic()
                if time.monotonic() - ended > 3 * _OUTPUT_QUIET + 2 * _OUTPUT_POLL:
                    raise PaneExited(self.pane_status(pane))

    def _command_running(self, pane):
        if pane.id in self._wrap_pending:
            return True  # typed without Enter: not started yet
        if time.monotonic() - self._run_typed.get(pane.id, -_RUN_START) < _RUN_START:
            return True  # send_keys() typed 'lte-run ...': not reported yet
        try:
            return self.is_running(pane)
        except Exception:
            return False

    def _close_output(self):
        """Stop the reader; drop a watch's files unless keep_output."""
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        watch_dir, self._watch_dir = self._watch_dir, None
        try:
            # A session that stays (adopted, or kept): stop piping into
            # files about to go, and take our hooks and options back.
            for pane_id, (_, piped, path, _, _) in self._piped_panes().items():
                if piped and watch_dir and not self.keep_output and \
                        path.startswith(watch_dir + os.sep):
                    self._tmux_run(*guarded_unpipe(pane_id))
            if self._capture_hooked and self._adopted:
                for hook in _PIPE_HOOKS:
                    self._tmux_run("set-hook", "-u", "-t", self.id, hook)
                for opt in ("@lte_pipe", "@lte_pipe_cmd"):
                    self._tmux_run("set", "-q", "-u", "-t", self.id, opt)
        except Exception:
            logger.debug("output capture cleanup failed", exc_info=True)
        self._capture_hooked = False
        if watch_dir:
            if self.keep_output:
                logger.info(f"output of watched panes kept in {watch_dir}")
            else:
                shutil.rmtree(watch_dir, ignore_errors=True)


    # =============== Closing ===============

    def on_close(self, action, when: str = "after"):
        """Run 'action' when the session is closed gracefully: lte-close
        (bind_close_key(), the dashboard's Q Q) or close().

        A callable runs in this process, so only while this script runs. A
        string is a shell command, kept in a session option: lte-close runs
        it in its window even after this script has gone. 'when': "before"
        the panes are stopped, or "after" (default). See docs/session-close.md."""
        if when not in _close.WHEN:
            raise ValueError(f"when must be one of {_close.WHEN}, not {when!r}")
        if callable(action):
            self._on_close.append((action, when))
        else:
            _close.add_action(self.id, action, when, run=self._tmux_run)

    def _run_close_callbacks(self, when):
        for fn, fn_when in list(self._on_close):
            if fn_when != when:
                continue
            try:
                fn(self)
            except Exception:
                logger.exception(f"on_close callback {fn!r} raised")

    def close(self, **stop_kwargs):
        """Close gracefully, here in the script: the 'before' actions, stop_all()
        ('stop_kwargs' go to it), the 'after' actions, then kill_session().
        Shell actions run with this process's stdout."""
        shell = _close.actions(self.id, run=self._tmux_run)
        for when in _close.WHEN:
            self._run_close_callbacks(when)
            _close.run_actions(shell, when, out=lambda text: logger.info(f"close: {text}"))
            if when == "before":
                self.stop_all(**stop_kwargs)
        self.kill_session()

    def _close_requested(self):
        """lte-close asks us to stop our panes (it runs the shell actions and
        kills the session itself). Progress goes through the status file."""
        if not self.status_file:
            self.status_file = os.path.join(self._wrap_dir, "status.json")
            self._publish_status_file()
        self._set_session_option(_close.STATE_OPTION, "stopping")
        try:
            self._run_close_callbacks("before")
            self.stop_all()
            self._run_close_callbacks("after")
        finally:
            self._write_status_file()
            self._give_back_close_keys()  # the session is about to go
            self._set_session_option(_close.STATE_OPTION, "done")

    def bind_close_key(self, key: Optional[str] = None) -> Optional[str]:
        """Bind prefix 'key' to lte-close in this session, so it closes
        gracefully. Elsewhere the key keeps its binding (e.g. a kill-session
        from ~/.tmux.conf); it is given back when this script lets go of the
        session (kill_session(), release()).

        'key' None (create() does this): ~/.tmux.conf's @lte_key_close, else
        Q, and only if unbound. Returns the key bound, or None."""
        run = self._tmux_run
        if_free = key is None
        if key is None:
            key = _keys.chosen(_close.KEY_ROLE, _close.KEY, run=run)
        if not key or not _keys.borrow(key, _close.KEY_OPTION, _close.key_command(),
                                       message="lte-close: not a libtmux_easy session",
                                       run=run, if_free=if_free):
            return None
        self._set_session_option(_close.KEY_OPTION, "1")
        self._close_keys.append(key)
        return key

    def kill_session(self, *args, **kwargs):
        """Stop the watcher, then kill the session. For an adopted session
        (current()), close only our windows: see release(). Idempotent.

        Only sends SIGHUP to what runs in the panes; stop_all() first for
        a clean shutdown."""
        return self.kill(*args, **kwargs)

    def kill(self, *args, **kwargs):
        """kill_session(), under libtmux's newer name."""
        if self._killed:
            return None
        self._killed = True
        if self._adopted:
            return self._release()
        self.stop_watching()
        self._close_wrap()
        if hasattr(Session, "kill"):
            return super().kill(*args, **kwargs)
        return super().kill_session(*args, **kwargs)  # libtmux < 0.30

    def release(self, keep_windows: bool = False, label: Optional[str] = None):
        """Let go of the session: stop tracking and close what is ours.

        Adopted (current()): close the windows we made, or with
        'keep_windows' leave them for inspection, renamed '<label>/<name>'
        if 'label' is given so a later new_window() can reuse the name.
        Created (create()): with 'keep_windows', leave the whole session up,
        e.g. for 'tmux attach' after a failed test run; else kill it.

        Stop the panes first (stop_all()) if they should not keep running."""
        if self._killed:
            return None
        if not self._adopted and not keep_windows:
            return self.kill_session()
        self._killed = True
        if self._adopted:
            return self._release(keep_windows, label)
        self._write_status_file()
        self.stop_watching()
        self._close_wrap()
        return None

    def _release(self, keep_windows=False, label=None):
        """kill_session() for an adopted session: close the windows we made,
        and our session options if nobody has replaced them, but leave the
        session itself -- and its other windows -- alone."""
        # Final state, for a sidebar that outlives us: stop_all()'s last
        # 'exited' message may still be in flight when the watcher stops.
        self._write_status_file()
        self.stop_watching()
        status = self.status_file and os.path.abspath(self.status_file)
        keep_status = status and not status.startswith(self._wrap_dir + os.sep)
        for name, ours in ((DIR_OPTION, self._wrap_dir),
                           (dash.STATUS_OPTION, None if keep_status else status)):
            if ours and self._session_option(name) == ours:
                self._set_session_option(name, None)
        if keep_windows:
            renamed = {}
            for window_id in self._own_windows:
                window = self.get_window(window_id)
                if window is not None and label:
                    renamed[window.name] = f"{label}/{window.name}"
                    self.server.cmd("rename-window", "-t", window_id, renamed[window.name])
            if renamed:
                # Our names are 'window:pane'; the status file (and so the
                # sidebar's grouping) should follow the new window names.
                def rename(full):
                    window, _, pane = full.partition(":")
                    return f"{renamed[window]}:{pane}" if window in renamed else full
                self.pane_names = {rename(k): v for k, v in self.pane_names.items()}
                self.commands = {rename(k): v for k, v in self.commands.items()}
                self._write_status_file()
            self._close_wrap()
            return None
        if self._dash_window and self.get_window(self._dash_window) is not None:
            # A pane of ours on show has the home pane parked in its slot,
            # which kill-window would take with it.
            dash.Viewport(self._dash_window, run=self._tmux_run,
                          sidebar=self._dash_sidebar).restore()
        for window_id in self._own_windows:
            self.server.cmd("kill-window", "-t", window_id)
        self._close_wrap()
        return None

    def _give_back_close_keys(self):
        while self._close_keys:
            try:
                _keys.give_back(self._close_keys.pop(), run=self._tmux_run)
            except Exception:
                logger.exception("giving back the close key failed")

    def _close_wrap(self):
        """Close the wrap-mode listener and remove its dir. Idempotent."""
        self._close_output()
        self._untee_home()
        self._give_back_close_keys()
        if self._wrap_listener is not None:
            self._wrap_listener.close()
            self._wrap_listener = None
        if self._wrap_dir is not None:
            shutil.rmtree(self._wrap_dir, ignore_errors=True)
            self._wrap_dir = None

    # =============== Dashboard ===============

    def _tmux_run(self, *args):
        return self.server.cmd(*args).stdout

    def dashboard(
        self,
        name: str = "dash",
        width: Union[int, str] = dash.AUTO,
        select: bool = True,
        tee: bool = True,
        mouse: bool = True,
        key: Optional[str] = None,
        initial: Union[Pane, str, None] = None,
    ) -> Window:
        """Open a dashboard window: a status sidebar (lte-dash) on the left, and
        on the right a viewport holding the selected pane -- the real pane,
        swapped in from its window, so it is fully interactive.

        'width' is the sidebar's width: columns, a percentage like "25%", or
        "auto" (default): sized once, at start, to fit the longest pane name
        and status listed then. 'select' switches attached clients to it. 'tee' calls tee_home(), so the viewport's home entry
        shows this script's output. 'mouse' turns on tmux mouse support for
        the session (click the sidebar, drag the border to resize). 'initial'
        (a Pane or pane name) is the pane the sidebar selects and shows first,
        instead of home. 'key' is the prefix key back to the dashboard (""
        for none); None: ~/.tmux.conf's @lte_key_dash, else g, if unbound.

        Customize it with dashboard_pane(), and the returned Window.

        Needs Textual, here or in an installed lte-dash; raises RuntimeError
        with install instructions otherwise. Uses the status file, so one is
        created in the session's control dir if none was configured. Calling
        it again reuses the open dashboard."""
        run = self._tmux_run
        if self._dash_window is None or self.get_window(self._dash_window) is None:
            launcher = dash.launcher()  # fail before touching the session
            initial_pane = self.resolve_target_pane(initial) if initial is not None else None

            if self.status_file is None:
                self.status_file = os.path.join(self._wrap_dir, "status.json")
                self._publish_status_file()
            for pane_id in list(self.pane_targets):
                pane = self.get_pane(pane_id)
                if pane is not None:
                    self.watch_pane(pane)  # status-file updates; no-op if armed
            self._write_status_file()
            if tee:
                self.tee_home()

            window_id, sidebar = dash.open_window(
                self.id, self.status_file, self.home_log,
                os.path.join(self._wrap_dir, SOCKET_NAME),
                name=name, width=width, mouse=mouse, run=run, sidebar_cmd=launcher,
                key=key, initial=initial_pane.id if initial_pane is not None else None)
            self._dash_window, self._dash_sidebar = window_id, sidebar
            self._no_capture(window_id)

        if select:
            run("select-window", "-t", self._dash_window)
            run("select-pane", "-t", self._dash_sidebar)
        return self.get_window(self._dash_window)

    def close_dashboard(self):
        """Put the viewed pane back in its window and close the dashboard.
        No-op if none is open."""
        if self._dash_window is None:
            return
        if not self._dash_owned:  # someone else's, e.g. lte-session's
            self._dash_window = self._dash_sidebar = None
            return
        if self.get_window(self._dash_window) is not None:
            # Stop the sidebar first: a swap of its own (a pending show())
            # racing restore() can put the viewed pane back in the viewport,
            # and kill-window would take it with it.
            if self._dash_sidebar:
                self._tmux_run("kill-pane", "-t", self._dash_sidebar)
            dash.Viewport(self._dash_window, run=self._tmux_run).restore()
            self._tmux_run("kill-window", "-t", self._dash_window)
        for name in (dash.WINDOW_OPTION, dash.SIDEBAR_OPTION):
            self._set_session_option(name, None)
        self._dash_window = self._dash_sidebar = None

    def dashboard_pane(
        self,
        command: Optional[str] = None,
        title: Optional[str] = None,
        side: str = "below",
        size: Optional[Union[int, str]] = None,
    ) -> Pane:
        """Add a persistent pane to the open dashboard, next to the viewport
        ('below' or 'right'). Panes swap through the viewport around it; it
        stays put.

        It is a shell with 'command' typed in, so Ctrl-C leaves it usable.
        'size' is rows/columns, or a percentage like "30%". Not a new_pane():
        it has no name, mode or status, and is not listed in the sidebar."""
        if side not in ("below", "right"):
            raise ValueError(f"side must be 'below' or 'right', got {side!r}")
        if self._dash_window is None or self.get_window(self._dash_window) is None:
            raise RuntimeError("no dashboard open; call dashboard() first")
        viewport = dash.Viewport(self._dash_window, run=self._tmux_run,
                                 sidebar=self._dash_sidebar)
        viewport.repair()
        args = ["split-window", "-d", "-v" if side == "below" else "-h", "-t", viewport.view]
        if size is not None:
            args += ["-l", str(size)]
        pane_id = self._tmux_run(*args, "-P", "-F", "#{pane_id}")[0].strip()
        pane = self.get_pane(pane_id)
        if title:
            _set_pane_title(pane, title)
        if command:
            self._type(pane, command, enter=True)
        return pane

    @property
    def home_log(self) -> Optional[str]:
        """The file the dashboard's home pane follows. See tee_home()."""
        return os.path.join(self._wrap_dir, dash.HOME_LOG) if self._wrap_dir else None

    def tee_home(self, stdout: bool = True, logs: bool = True):
        """Copy this script's output to home_log, for the dashboard's home pane.

        'stdout' tees sys.stdout and sys.stderr (Python-level writes only; a
        subprocess writing to the inherited fds is not copied). 'logs' adds a
        root logging handler, which sees records at the levels the root
        logger already allows. Undone by kill_session(). Idempotent."""
        if self._home_tee is not None:
            return
        log = open(self.home_log, "a", buffering=1)
        saved = (sys.stdout, sys.stderr)
        if stdout:
            sys.stdout, sys.stderr = _Tee(sys.stdout, log), _Tee(sys.stderr, log)
        handler = None
        if logs:
            handler = logging.StreamHandler(log)
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
            handler.addFilter(_SkipIfTeed())
            logging.getLogger().addHandler(handler)
        self._home_tee = (log, saved, handler)

    def _untee_home(self):
        if self._home_tee is None:
            return
        log, (stdout, stderr), handler = self._home_tee
        self._home_tee = None
        if isinstance(sys.stdout, _Tee):
            sys.stdout = stdout
        if isinstance(sys.stderr, _Tee):
            sys.stderr = stderr
        if handler is not None:
            logging.getLogger().removeHandler(handler)
        log.close()

    # =============== Status surface ===============

    def pane_status(self, target_pane: Union[Pane, str]) -> PaneStatus:
        """A PaneStatus snapshot for a single pane."""
        pane = self.resolve_target_pane(target_pane)
        return self._build_pane_status(pane.id, pane)

    def status(self) -> list:
        """A PaneStatus snapshot for every pane created via new_pane()."""
        return [
            self._build_pane_status(pane_id, self.get_pane(pane_id))
            for pane_id in list(self.pane_targets)
        ]

    def print_status(self):
        """Human-readable status table for every pane created via new_pane()."""
        rows = self.status()
        if not rows:
            print("(no panes)")
            return
        width = max(len(r.name) for r in rows)
        for r in rows:
            state = str(r)
            pids = ",".join(str(p) for p in r.target_pids) or "-"
            print(f"{r.name:<{width}}  [{state}]  pid={pids}  {r.command}")

    def _build_pane_status(self, pane_id, pane: Optional[Pane]) -> PaneStatus:
        """Build a PaneStatus for 'pane_id'. 'pane' may be None if the pane has
        vanished (e.g. the user typed 'exit'), in which case the last-known
        exit status is reported and running is False."""
        name = self._name_for_pane_id(pane_id)
        mode = self.pane_modes.get(pane_id, PANE_MODE_SHELL)
        cmds = self.commands.get(name) if name else None
        command = cmds[0] if cmds else ""
        targets = self.pane_targets.get(pane_id, ())
        target_pids = tuple(p.pid for p in targets)

        if pane is not None:
            running = self.is_running(pane)
            exit_status = self.exit_status(pane)
        else:
            running = False
            with self._status_lock:
                exit_status = self._exit_status.get(pane_id, ExitStatus())

        return PaneStatus(
            name=name or pane_id,
            pane_id=pane_id,
            mode=mode,
            command=command,
            target_pids=target_pids,
            running=running,
            exit_status=exit_status,
            stopped=None if running else self._stopped_by.get(pane_id),
        )

    def _name_for_pane_id(self, pane_id) -> Optional[str]:
        for name, p in self.pane_names.items():
            if p.id == pane_id:
                return name
        return None

    def _refresh_pane_title(self, pane: Pane):
        """Update the visible pane border title to reflect running/exit state.
        Does not touch pane_names, so lookups by name keep working -- only the
        displayed title gets the '[...]' suffix. No-op if title_status=False."""
        if not self.title_status:
            return
        name = self.get_pane_name(pane)
        base = name.split(":")[-1] if name else pane.id
        status = self._build_pane_status(pane.id, pane)
        if status.stopped:  # the full str() is too long for a border
            suffix = "stopped" if status.stopped == "SIGINT" else f"stopped, {status.stopped}"
        else:
            suffix = str(status)
        try:
            _set_pane_title(pane, f"{base} [{suffix}]")
        except Exception:
            logger.debug(f"Could not refresh title for pane {pane.id}", exc_info=True)

    def _write_status_file(self):
        """Write status() as JSON to self.status_file, if one was configured.
        Lets an external process (no tmux/libtmux knowledge required) monitor
        pane state by polling one small file."""
        if not self.status_file:
            return
        try:
            payload = [asdict(s) for s in self.status()]
            # Replaced atomically, so a reader never sees a half-written file.
            tmp = f"{self.status_file}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self.status_file)
        except OSError:
            logger.exception(f"Could not write status file {self.status_file}")

    # =============== Getters ===============
    def get_pane_name(self, pane: Pane) -> Optional[str]:
        """Return the fully qualified name of the given pane (i.e. 'window_name:pane_name'), or None
        if the pane is unnamed."""
        self._check_pane_membership(pane)
        return self._name_for_pane_id(pane.id)

    def get_window_name(self, window: Window) -> Optional[str]:
        """Return the name of the given window, or None if the window is unnamed."""
        self._check_window_membership(window)
        return window.name

    def get_pane(self, identifier: str) -> Optional[Pane]:
        """Return the Pane object associated with the given 'identifier' which can either be the
        fully qualified pane name or the pane id. Return None if no pane is found."""
        if identifier[0] == "%":
            try:
                ids = {p.id: p for p in self.panes}
            except exc.LibTmuxException:  # the session (or the server) is gone
                return None
            return ids.get(identifier, None)
        else:
            return self.pane_names.get(identifier, None)

    def get_window(self, identifier: str) -> Optional[Window]:
        """Return the Window object associated with the given 'identifier' which can either be the
        window name or the window id. Return None if no window is found."""
        if identifier[0] == "@":
            ids = {w.id: w for w in self.windows}
            return ids.get(identifier, None)
        else:
            names = {w.name: w for w in self.windows}
            return names.get(identifier, None)

    # =============== Private ===============
    def _full_pane_name(self, context: Union[Window, Pane], name):
        """'window_name:pane_name', for a pane in (or next to) 'context'."""
        if isinstance(context, Pane):
            context = context.window
        elif not isinstance(context, Window):
            raise ValueError(f"Context must be a Window or Pane object, not {type(context)}.")
        return f"{context.name}:{name}"

    def _check_pane_membership(self, pane: Pane):
        """Check if the given pane is part of this session. Raises an exception if not."""
        if pane not in self.panes:
            raise ValueError(f"Pane {pane} not found in this session.")

    def _check_window_membership(self, window: Window):
        """Check if the given window is part of this session. Raises an exception if not."""
        if window not in self.windows:
            raise ValueError(f"Window {window} not found in this session.")

