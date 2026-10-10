import libtmux
from libtmux.server import Server
from libtmux.window import Window
from libtmux.session import Session
from libtmux.pane import Pane
from libtmux.common import tmux_cmd, exc
import re
import shlex
import shutil
import signal
import socket
import sys
import tempfile
import psutil
import time
import os
import json
import selectors
import threading
from pathlib import Path

from .proc import pane_child_processes, get_session_pids
from .wrap import DIR_OPTION, PROTOCOL_VERSION as WRAP_PROTOCOL_VERSION, SOCKET_NAME

from pprint import pprint as pp
import logging
from dataclasses import dataclass, asdict

from typing import Optional, Union
from packaging import version

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


@dataclass(frozen=True)
class ExitStatus:
    """How a pane's command finished.

    When known, exactly one of these is set:
      code    -- the process exited normally with this status
      signal  -- the process was killed by this signal number

    'inferred' is True when 'signal' was derived from a shell's 128+N
    convention (shell-mode panes) rather than reported directly by tmux or
    lte-run. In that case a genuine 'exit 130' cannot be distinguished from
    'killed by SIGINT'. Exec- and wrap-mode panes never have this ambiguity.
    """

    code: Optional[int] = None
    signal: Optional[int] = None
    inferred: bool = False

    @property
    def known(self) -> bool:
        """False when the status could not be determined (still running, or the
        pane/shell could not be probed)."""
        return self.code is not None or self.signal is not None

    @property
    def ok(self) -> bool:
        """True only for a normal exit with status 0."""
        return self.code == 0

    def __str__(self) -> str:
        if self.signal is not None:
            base = f"killed by signal {self.signal}"
            return f"{base} (inferred)" if self.inferred else base
        if self.code is not None:
            return f"exited {self.code}"
        return "unknown"


@dataclass(frozen=True)
class PaneStatus:
    """A snapshot of one pane created via new_pane(). See TmuxSession.status()
    and .pane_status()."""

    name: str                  # 'window:pane', or the raw pane id if unnamed
    pane_id: str                # '%3'
    mode: str                   # PANE_MODE_SHELL | PANE_MODE_EXEC | PANE_MODE_WRAP
    command: str
    target_pids: tuple          # ints, for display -- see new_pane()
    running: bool
    exit_status: ExitStatus


def _proc_alive(proc: psutil.Process) -> bool:
    """True if 'proc' is still alive and not a zombie.

    The zombie check matters: a reaped-but-not-yet-collected process still
    reports is_running() == True from psutil."""
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


"""
DESCRIPTION: TODO
"""

"""
TODO
1. Add more options for how to split and arrange panes (e.g. horizontal vs vertical, custom layouts, etc.)
2. Add logic for removing windows/panes
3. Add options for sending commands (e.g. send to multiple, wait and retrieve output, check on error code, check on status, etc)
4. Add support for using either the fully qualified pane name or just the pane name?
"""

"""TODO
1. Test across different versions
    - 0.10.1
    - 0.21.0
2. Update the construction of TmuxSession
    - Doesn't work in older 0.10.1 version
3. Do we want to support reusing an old session? What would the use case for that be? This lib is
all about generating a session. Maybe for updating a session? But that would also probably involve
deleting window/panes and generating new ones. And maybe moving them around. Which I don't support
yet.
    - How about we mark it as future functionality
    - If we did reuse an old session, we would have to manually repopulate the state of TmuxSession
    - ACTUALLY - Suppose our script is called within a tmux session. In that case we would want to
    use the existing session instead of trying to nest. We could still create our windows/panes, but
    just within the existing session.
    
    This raises some questions:
    - How do we init our TmuxSession? Because if we are merging with an existing session that has
    not used this lib, some assumptions might not hold. Like we enforce windows having unique names.
    But libtmux doesn't. Only sessions have unique names. Windows and panes can have duplicate
    names/titles, but they have unique ids. 
    - Do we just hope for the best? I like having the ability to specify a window or pane by name
    instead of having to create a bunch of dummy variables to get the windows and panes to pass. 
    - We could keep track of the windows/panes WE'VE created. Then if we pass in a name for lookup,
    it is assumed that we are looking for a window/pane we've created. Unless explicitly specified
    that it is outside our current "session"
    - Or maybe we could somehow namespace our names? 
    
    
4. Code in ability to retrieve the pane title from libtmux

    window.list_panes() # To force a refresh
    # For libtmux >= 0.21.0
    pane_title = pane.item.get("pane_title")
    # For older versions
    pane_title = pane.get("pane_title")
    
"""

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


def _set_pane_title(pane: Pane, pane_name: str):
    """Set the title of a tmux pane. This is done by sending a tmux command to select the pane and
    set its title."""
    tmux_command(f"select-pane -T {pane_name} -t {pane.id}")


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
        try:
            session.kill_session()
        except DeprecatedError:  # kill_session() deprecated in >0.30.0
            session.kill()
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

    return session, window, window.panes[0]


class _Watch:
    """Bookkeeping for one pane's outstanding target pids, used only by
    _PaneWatcher."""

    __slots__ = ("pane_id", "fds", "poll_procs")

    def __init__(self, pane_id):
        self.pane_id = pane_id
        self.fds = {}          # pidfd -> psutil.Process
        self.poll_procs = []   # procs with no pidfd available (fallback)


_LISTENER = object()  # selector tag for the wrap-mode listening socket


class _WrapConn:
    """One lte-run connection, owned by _PaneWatcher."""

    __slots__ = ("sock", "buf", "pane_id", "send_lock")

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.pane_id = None  # set on the 'started' message
        self.send_lock = threading.Lock()

    def send(self, **msg) -> bool:
        with self.send_lock:
            try:
                self.sock.sendall((json.dumps(msg) + "\n").encode())
                return True
            except OSError:
                return False


@dataclass
class _WrapRun:
    """The latest lte-run in a pane. See working/PLAN-wrap-mode.md."""

    conn: _WrapConn
    wrapper_pid: Optional[int]
    target_pid: Optional[int]
    argv: list
    status: Optional[ExitStatus] = None  # set by the 'exited' message
    lost: bool = False                   # connection closed with no 'exited'


class _PaneWatcher(threading.Thread):
    """One background thread per session. Watches target pids via
    os.pidfd_open(), which fires the instant an arbitrary pid exits -- no
    polling required, and the pid does not have to be a child of this
    process -- and calls 'on_exit(pane_id)' exactly once per pane, the
    moment every one of its target pids has exited.

    Falls back to low-frequency psutil polling for any pid pidfd_open can't
    be opened on (Linux < 5.3, or a pid that is already gone by the time
    it's armed).

    Also serves the wrap-mode control socket: the listener and every lte-run
    connection share this thread's selector, and each message is handed to
    'on_wrap_message(conn, msg)', each EOF to 'on_wrap_closed(conn)'.
    """

    _POLL_INTERVAL = 0.5

    def __init__(self, on_exit, on_wrap_message=None, on_wrap_closed=None):
        super().__init__(daemon=True, name="PaneWatcher")
        self._on_exit = on_exit
        self._on_wrap_message = on_wrap_message
        self._on_wrap_closed = on_wrap_closed
        self._lock = threading.Lock()
        self._selector = selectors.DefaultSelector()
        self._watches = {}  # pane_id -> _Watch
        self._stopped = False
        self._wake_r, self._wake_w = os.pipe()
        self._selector.register(self._wake_r, selectors.EVENT_READ, data=None)

    def _wake(self):
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass

    def watch(self, pane_id, procs):
        """Begin watching 'procs' (an iterable of psutil.Process) for
        'pane_id'. No-op if that pane is already being watched. Fires
        'on_exit(pane_id)' -- from this thread -- the moment every one of
        'procs' has exited; if they are all already dead, fires immediately,
        from the calling thread, instead of registering anything."""
        watch = _Watch(pane_id)
        finished = True
        for proc in procs:
            alive = _proc_alive(proc)
            fd = None
            if alive and hasattr(os, "pidfd_open"):
                try:
                    fd = os.pidfd_open(proc.pid)
                except (OSError, ProcessLookupError):
                    # Raced with the process exiting between the alive-check
                    # above and the open -- re-check rather than assume.
                    fd = None
                    alive = _proc_alive(proc)
            if not alive:
                continue
            finished = False
            if fd is not None:
                watch.fds[fd] = proc
            else:
                watch.poll_procs.append(proc)

        if finished:
            self._on_exit(pane_id)
            return

        with self._lock:
            if self._stopped:
                for fd in watch.fds:
                    os.close(fd)
                return
            self._watches[pane_id] = watch
            for fd in watch.fds:
                self._selector.register(fd, selectors.EVENT_READ, data=pane_id)
        self._wake()

    def add_listener(self, sock):
        """Accept lte-run connections on 'sock'. The caller keeps ownership
        and closes it after stop()."""
        with self._lock:
            self._selector.register(sock, selectors.EVENT_READ, data=_LISTENER)
        self._wake()

    def unwatch(self, pane_id):
        """Stop watching a pane without firing its callback. Safe to call
        whether or not that pane is currently being watched."""
        with self._lock:
            watch = self._watches.pop(pane_id, None)
            if watch is None:
                return
            for fd in watch.fds:
                try:
                    self._selector.unregister(fd)
                except KeyError:
                    pass
                os.close(fd)
        self._wake()

    def stop(self):
        """Stop the thread and release all pidfds. Idempotent."""
        with self._lock:
            self._stopped = True
            for watch in self._watches.values():
                for fd in watch.fds:
                    try:
                        self._selector.unregister(fd)
                    except KeyError:
                        pass
                    os.close(fd)
            self._watches.clear()
            for key in list(self._selector.get_map().values()):
                if isinstance(key.data, _WrapConn):
                    self._selector.unregister(key.fileobj)
                    key.data.sock.close()
        self._wake()
        self.join(timeout=2.0)
        self._selector.close()
        os.close(self._wake_r)
        os.close(self._wake_w)

    def run(self):
        while True:
            with self._lock:
                if self._stopped:
                    return
                has_poll = any(w.poll_procs for w in self._watches.values())
            timeout = self._POLL_INTERVAL if has_poll else None
            try:
                events = self._selector.select(timeout=timeout)
            except (ValueError, OSError):
                # stop() closed the selector out from under us -- a benign
                # race between this call and stop()'s cleanup, not a bug to
                # surface. Just exit.
                return

            finished = []
            wrap_events = []
            with self._lock:
                if self._stopped:
                    return
                for key, _ in events:
                    if key.data is None:  # the wake pipe
                        try:
                            os.read(self._wake_r, 4096)
                        except OSError:
                            pass
                        continue
                    # Sockets stay blocking: the selector guarantees accept()
                    # and recv() won't block, and it keeps sendall() simple.
                    if key.data is _LISTENER:
                        try:
                            conn_sock, _ = key.fileobj.accept()
                        except OSError:
                            continue
                        self._selector.register(
                            conn_sock, selectors.EVENT_READ, data=_WrapConn(conn_sock))
                        continue
                    if isinstance(key.data, _WrapConn):
                        conn = key.data
                        try:
                            chunk = conn.sock.recv(65536)
                        except OSError:
                            chunk = b""
                        if not chunk:
                            self._selector.unregister(conn.sock)
                            conn.sock.close()
                            wrap_events.append(("closed", conn, None))
                            continue
                        conn.buf += chunk
                        *lines, conn.buf = conn.buf.split(b"\n")
                        for line in lines:
                            try:
                                wrap_events.append(("message", conn, json.loads(line)))
                            except ValueError:
                                logger.debug(f"ignoring malformed lte-run message: {line!r}")
                        continue
                    pane_id = key.data
                    watch = self._watches.get(pane_id)
                    if watch is None:
                        continue
                    watch.fds.pop(key.fd, None)
                    try:
                        self._selector.unregister(key.fd)
                    except KeyError:
                        pass
                    os.close(key.fd)
                    if not watch.fds and not watch.poll_procs:
                        finished.append(pane_id)
                        del self._watches[pane_id]

                for pane_id, watch in list(self._watches.items()):
                    if not watch.poll_procs:
                        continue
                    watch.poll_procs = [p for p in watch.poll_procs if _proc_alive(p)]
                    if not watch.fds and not watch.poll_procs:
                        finished.append(pane_id)
                        del self._watches[pane_id]

            for kind, conn, msg in wrap_events:
                try:
                    if kind == "message" and self._on_wrap_message:
                        self._on_wrap_message(conn, msg)
                    elif kind == "closed" and self._on_wrap_closed:
                        self._on_wrap_closed(conn)
                except Exception:
                    logger.exception("lte-run event handler raised")

            for pane_id in finished:
                try:
                    self._on_exit(pane_id)
                except Exception:
                    logger.exception(f"on_exit handler for pane {pane_id} raised")


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
        """
        session, window, pane = _create_session(
            session_name, kill_existing=kill_existing, **kwargs
        )
        session.__class__ = cls
        session.session_name = session_name
        session.__post_init__(window, pane, title_status=title_status, status_file=status_file)
        return session

    def __post_init__(self, window, pane, title_status: bool = True, status_file: Optional[str] = None):
        # Set internal state
        self.current_window = window
        self.current_pane = pane

        # Set default options. These allow for pane titles to be displayed
        self.set_option("renumber-windows", "on")
        try:
            self.set_option("pane-border-status", "top", _global=True)
        except:  # global arg name changed in newer versions of libtmux
            self.set_option("pane-border-status", "top", global_=True)

        self.unnamed_window = True
        self.unnamed_pane = {self.current_window.id: True}

        self.pane_names = {}  # Mapping of pane_name -> Pane object
        self.commands = {}
        self.command_before = {"all": None}

        self.pane_modes = {}      # pane.id -> PANE_MODE_*
        self._exit_status = {}    # pane.id -> ExitStatus (cached; see one-shot note)

        # Target-pid tracking, exit events, and the status surface (see
        # working/PLAN-exit-events.md).
        self.title_status = title_status
        self.status_file = status_file
        self.pane_targets = {}        # pane.id -> tuple of psutil.Process (the pids
                                       # the pane's command itself spawned)
        self._exit_callbacks = {}     # pane.id -> list of on_exit(PaneStatus) callbacks
        self._watched_panes = set()   # pane.id -- already armed, avoid double-registering
        self._watcher = None          # lazily-started _PaneWatcher
        self._status_lock = threading.Lock()

        # Wrap mode (working/PLAN-wrap-mode.md). Set up unconditionally so that
        # 'lte-run' typed by hand works in any pane of the session.
        self._wrap_runs = {}      # pane.id -> _WrapRun (latest run only)
        self._wrap_started = {}   # pane.id -> threading.Event, set on 'started'
        self._wrap_pending = set()  # pane.id typed with enter=False, not yet run
        self._wrap_dir = _make_wrap_dir()
        self._wrap_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._wrap_listener.bind(os.path.join(self._wrap_dir, SOCKET_NAME))
        self._wrap_listener.listen(64)
        self.set_option(DIR_OPTION, self._wrap_dir)
        self._ensure_watcher().add_listener(self._wrap_listener)

    def new_window(
        self, name: str, layout: Union[None, str] = "tiled", **window_kwargs
    ) -> Window:
        """Create a new window with the given name. Due to the nature of tmux, this will by also
        create a default, unnamed pane. The next call to new_pane will reuse and rename this pane
        instead of actually creating a new pane."""

        if self.unnamed_window:
            window = self.windows[0]
            window.rename_window(name)
            self.unnamed_window = False
            # Update current_window to the renamed window
            self.current_window = window
            self.current_pane = window.panes[0]
        else:
            if name in [w.name for w in self.windows]:
                raise ValueError(f"Window name {name} already exists.")

            window = super().new_window(name, **window_kwargs)

            self.current_window = window
            self.current_pane = window.panes[0]
            self.unnamed_pane[self.current_window.id] = True

        if layout:
            # 'layout' is an attribute already used by libtmux, hence layout_str.
            # This must be set on the reused first window too: split_window()
            # only re-tiles when it is present, and without that every split
            # halves the previous pane (30 -> 14 -> 7 -> 3 -> 2 rows), so the
            # sixth pane fails with 'no space for new pane'.
            window.layout_str = layout

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
        
    def _claim_pane(self, name, **split_kwargs):
        """Return (pane, reused) for the pane 'name' should occupy -- either the
        window's initial unnamed pane (first call after new_window) or a freshly
        split one. 'reused' matters to callers that must respawn the pane, since a
        reused pane is already running its login shell."""
        if self.unnamed_pane[self.current_window.id]:
            pane = self.current_window.panes[0]
            self.set_pane_title(pane, name)
            self.unnamed_pane[self.current_window.id] = False
            return pane, True

        full_name = self._full_pane_name(self.current_window, name)
        if full_name in self.pane_names:
            raise ValueError(
                f"Pane name {name} already exists in window {self.current_window.name}."
            )
        return self.split_window(self.current_window, name, **split_kwargs), False

    def new_pane(
        self,
        name: str,
        command: str,
        enter: bool = True,
        mode: str = PANE_MODE_SHELL,
        on_exit=None,
        target_pid_timeout: float = 1.0,
        **pane_kwargs,
    ) -> Pane:
        """Create a new pane named 'name' and run 'command' in it.

        mode=PANE_MODE_SHELL (default)
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

        'on_exit', if given, is a callback fired with a PaneStatus the moment the
        command exits for any reason (normal exit, crash, or a signal -- including
        a Ctrl-C typed by a user who attached to the session). Equivalent to
        calling watch_pane(pane, on_exit) right after creation; see watch_pane()
        for callback-thread caveats.

        'target_pid_timeout' bounds how long (shell mode only) to wait for the
        command's immediate child process(es) to appear -- the pids that
        is_running()/exit_status()/on_exit are actually judged on. See
        _capture_target_pids().

        Caveat: exec mode relies on split_window(shell=...), which needs
        libtmux >= 0.21. _claim_pane()'s reuse path uses respawn-pane instead,
        so it works on any version.
        """
        if mode not in _PANE_MODES:
            raise ValueError(f"mode must be one of {_PANE_MODES}, got {mode!r}")
        if self.unnamed_window:
            raise ValueError("Cannot create a new pane before creating a new window.")

        before = (
            self.command_before.get(self.current_window.id)
            or self.command_before["all"]
        )

        if mode == PANE_MODE_EXEC:
            pane = self._new_exec_pane(name, command, before, **pane_kwargs)
        elif mode == PANE_MODE_WRAP:
            pane = self._new_wrap_pane(name, command, before, enter, **pane_kwargs)
        else:
            pane = self._new_shell_pane(name, command, before, enter, **pane_kwargs)

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

    def _new_shell_pane(self, name, command, before, enter, **pane_kwargs):
        pane, _reused = self._claim_pane(name, **pane_kwargs)
        if before:
            pane.send_keys(before, enter=True)
            # Let setup finish, so _capture_target_pids() snapshots the target
            # command and not the setup command. Uses the any-children test
            # (via is_running()'s fallback), since no target is recorded yet.
            self.wait_for_pane_idle(pane, timeout=30.0)
        pane.send_keys(command, enter)
        return pane

    def _new_wrap_pane(self, name, command, before, enter, **pane_kwargs):
        pane, _reused = self._claim_pane(name, **pane_kwargs)
        if before:
            pane.send_keys(before, enter=True)
            self.wait_for_pane_idle(pane, timeout=30.0)
        self._send_wrapped(pane, command, enter)
        return pane

    def _new_exec_pane(self, name, command, before, **pane_kwargs):
        # A command_before still works here, but it has to run in the same process
        # that then becomes 'command' -- hence 'exec'. Without it the pane process
        # would be the setup shell, and tmux would report ITS status, not the
        # command's. This is the idiom callers need for e.g. sourcing a ROS
        # workspace before launching.
        shell_command = command
        if before:
            inner = f"{before} && exec {command}"
            shell_command = f"bash -c {shlex.quote(inner)}"

        # 'shell' is how this mode launches the command; a caller-supplied one
        # would be silently overwritten, so reject it rather than confuse.
        if "shell" in pane_kwargs:
            raise ValueError("'shell' is not accepted in exec mode; pass 'command' instead.")

        pane, reused = self._claim_pane(name, shell=shell_command, **pane_kwargs)

        # Must happen before the command can exit, or a fast-failing command takes
        # its own exit status (and possibly the whole session) down with the pane.
        self._set_remain_on_exit(pane)

        if reused:
            # The reused initial pane is still running its login shell -- the
            # 'shell' kwarg above never applied to it. Replace its process.
            pane.cmd("respawn-pane", "-k", shell_command)
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
        result = pane.cmd("set-option", "-p", "remain-on-exit", "on")
        if getattr(result, "stderr", None):
            logger.debug("per-pane remain-on-exit unavailable; falling back to window scope")
            self.current_window.set_window_option("remain-on-exit", "on")

    # TODO - specify layout when creating the window. If 'None', then don't select layout.
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
        pane.send_keys(command, enter)
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
        window_name = pane.window.name
        full_name = self._full_pane_name(pane, pane_name)
        self.pane_names[full_name] = pane

    def print(self):
        """Print layout of the session, including windows and panes."""
        rep = f"Session: {self.session_name}\n"
        tab = "    "
        for window in self.windows:
            rep += f"{tab}Window: {window.name}\n"
            for pane in window.panes:
                pane_name = self.get_pane_name(pane)
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
        """Select the the first pane in the first window."""
        self.current_window = self.windows[0]
        self.current_pane = self.windows[0].panes[0]
        return (
            super()
            .select_window(self.windows[0].id)
            .select_pane(self.windows[0].panes[0].id)
        )

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
        out = pane.cmd("display-message", "-p", fmt).stdout
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
        pane.send_keys(f"echo {_EXIT_MARKER}:$?:", enter=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(poll_interval)
            text = "\n".join(pane.cmd("capture-pane", "-p", "-J").stdout)
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
        was running did not shut down cleanly (e.g. a bag recorder likely failed to
        finalize its metadata). Call exit_status() afterwards for the outcome; it is
        captured here while it is still readable."""
        pane = self.resolve_target_pane(target_pane)
        pane_name = self.get_pane_name(pane) or pane.id

        if not self.is_running(pane):
            self.exit_status(pane)
            return True

        # Step 1: SIGINT (Ctrl-C). Delivered to the pane's foreground process
        # group, same as a user hitting Ctrl-C in a real terminal. Catchable --
        # gives the process a chance to run its graceful-shutdown path. Works in
        # every pane mode; a wrap pane's lte-run delivers it to the same group.
        if not self._wrap_signal(pane, signal.SIGINT):
            pane.send_keys("C-c", enter=False)
        if self.wait_for_pane_idle(pane, timeout=sigint_timeout, poll_interval=poll_interval):
            self.exit_status(pane)
            return True

        logger.warning(
            f"Pane {pane_name} still running after SIGINT ({sigint_timeout}s). Escalating to SIGTERM."
        )
        # Step 2: SIGTERM. Still catchable, but a firmer "terminate now" sent
        # directly to the processes, bypassing the pane.
        self._signal_pane_processes(pane, "terminate")
        if self.wait_for_pane_idle(pane, timeout=sigterm_timeout, poll_interval=poll_interval):
            self.exit_status(pane)
            return True

        logger.warning(
            f"Pane {pane_name} still running after SIGTERM ({sigterm_timeout}s). Escalating to SIGKILL."
        )
        # Step 3: SIGKILL. Un-catchable -- the kernel kills the process immediately
        # with no cleanup. Last resort.
        self._signal_pane_processes(pane, "kill")
        stopped = self.wait_for_pane_idle(pane, timeout=sigkill_timeout, poll_interval=poll_interval)
        if stopped:
            self.exit_status(pane)
        else:
            logger.warning(f"Pane {pane_name} still running after SIGKILL ({sigkill_timeout}s).")
        return stopped

    def _signal_pane_processes(self, pane, method):
        """Send 'terminate' or 'kill' to everything the pane is running.

        For a shell pane that means the shell's descendants -- the shell itself has
        to survive, or the pane dies with it. For an exec pane the command IS the
        pane process, so it must be signalled too; its descendants alone may be
        empty, which is what made the old descendants-only logic a silent no-op.

        For a wrap pane, lte-run delivers the signal to its target group. Never
        fall back to pane_child_processes() there: it includes the wrapper, and
        killing the wrapper loses the status it exists to report."""
        if self._pane_mode(pane) == PANE_MODE_WRAP:
            sig = signal.SIGTERM if method == "terminate" else signal.SIGKILL
            if self._wrap_signal(pane, sig):
                return
            for proc in [p for p in self.pane_targets.get(pane.id, ()) if _proc_alive(p)]:
                try:
                    getattr(proc, method)()
                except psutil.NoSuchProcess:
                    pass
            return

        targets = list(pane_child_processes(pane))
        if self._pane_mode(pane) == PANE_MODE_EXEC:
            try:
                targets.append(psutil.Process(int(pane.pane_pid)))
            except (psutil.NoSuchProcess, ValueError, TypeError):
                pass
        for proc in targets:
            try:
                getattr(proc, method)()
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

        if mode == PANE_MODE_EXEC:
            pane.cmd("respawn-pane", "-k", command)
            pane.refresh()  # pane_pid is a cached field; respawn-pane gave it a new one
        else:
            if self.is_running(pane):
                self.stop_pane(pane)
                # stop_pane() captures how the OLD run ended; that is not what
                # the caller is about to ask about.
                self._exit_status.pop(pane.id, None)
            if mode == PANE_MODE_WRAP:
                self._send_wrapped(pane, command)
            else:
                pane.send_keys(command, enter=True)

        self.commands[self.get_pane_name(pane)] = [command]

        # Target-pid tracking is per-run: the old pid is gone the moment the
        # command is replaced, so is_running()/the watcher must pick up the new
        # one, same as a fresh new_pane().
        self.pane_targets[pane.id] = self._capture_target_pids(pane, mode, 1.0)
        self._watched_panes.discard(pane.id)
        self._refresh_pane_title(pane)
        if self.title_status or self.status_file is not None:
            self.watch_pane(pane)
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
                self._exit_status.pop(pane_id, None)  # a new run
                self._wrap_runs[pane_id] = _WrapRun(
                    conn, msg.get("wrapper_pid"), msg.get("target_pid"), msg.get("argv", []))
                self.pane_modes[pane_id] = PANE_MODE_WRAP  # also upgrades a shell pane
                self.pane_targets[pane_id] = targets
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

    def _send_wrapped(self, pane, command, enter=True):
        """Type 'command' into a shell pane under lte-run."""
        target = command
        if any(op in command for op in _SHELL_OPERATORS):
            target = f"bash -c {shlex.quote(command)}"
        # Armed before sending, so a fast 'started' can't be missed. The mode
        # must be set first too, or 'started' would be ignored as untracked.
        self._wrap_started[pane.id] = threading.Event()
        self.pane_modes[pane.id] = PANE_MODE_WRAP
        pane.send_keys(f"{_wrap_prefix()} {target}", enter)

    def _wrap_signal(self, pane, sig) -> bool:
        """Ask the pane's lte-run to signal its target. False if there is no
        live connection to ask (not a wrap pane, finished, or lost)."""
        run = self._wrap_runs.get(pane.id)
        if run is None or run.lost or run.status is not None:
            return False
        return run.conn.send(cmd="signal", signal=int(sig))

    def kill_session(self, *args, **kwargs):
        """Stop the pane watcher before killing the session, so the
        background thread doesn't try to touch pane state mid-teardown."""
        self.stop_watching()
        self._close_wrap()
        return super().kill_session(*args, **kwargs)

    def kill(self, *args, **kwargs):
        """Stop the pane watcher before killing the session, so the
        background thread doesn't try to touch pane state mid-teardown."""
        self.stop_watching()
        self._close_wrap()
        return super().kill(*args, **kwargs)

    def _close_wrap(self):
        """Close the wrap-mode listener and remove its dir. Idempotent."""
        if self._wrap_listener is not None:
            self._wrap_listener.close()
            self._wrap_listener = None
        if self._wrap_dir is not None:
            shutil.rmtree(self._wrap_dir, ignore_errors=True)
            self._wrap_dir = None

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
            state = "running" if r.running else str(r.exit_status)
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
        suffix = "running" if status.running else str(status.exit_status)
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
            with open(self.status_file, "w") as f:
                json.dump(payload, f, indent=2)
        except OSError:
            logger.exception(f"Could not write status file {self.status_file}")

    # =============== Getters ===============
    def get_pane_name(self, pane: Pane) -> Optional[str]:
        """Return the fully qualified name of the given pane (i.e. 'window_name:pane_name'), or None
        if the pane is unnamed."""
        self._check_pane_membership(pane)
        for name, p in self.pane_names.items():
            if p == pane:
                return name
        return None

    def get_window_name(self, window: Window) -> Optional[str]:
        """Return the name of the given window, or None if the window is unnamed."""
        self._check_window_membership(window)
        return window.name

    def get_pane(self, identifier: str) -> Optional[Pane]:
        """Return the Pane object associated with the given 'identifier' which can either be the
        fully qualified pane name or the pane id. Return None if no pane is found."""
        if identifier[0] == "%":
            ids = {p.id: p for p in self.panes}
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
        """Helper function to generate a fully qualified name for a pane (i.e.
        'window_name:pane_name')"""
        if isinstance(context, Window):
            full_name = f"{context.name}:{name}"
        elif isinstance(context, Pane):
            full_name = f"{context.window.name}:{name}"
        else:
            raise ValueError(
                f"Context must be a Window or Pane object, not {type(context)}."
            )

        return full_name

    def _check_pane_membership(self, pane: Pane):
        """Check if the given pane is part of this session. Raises an exception if not."""
        panes = []
        for window in self.windows:
            panes.extend(window.panes)
        if pane not in panes:
            raise ValueError(f"Pane {pane} not found in this session.")

    def _check_window_membership(self, window: Window):
        """Check if the given window is part of this session. Raises an exception if not."""
        if window not in self.windows:
            raise ValueError(f"Window {window} not found in this session.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(levelname)s:  %(message)s",
        handlers=[logging.StreamHandler()],
    )

    TmuxSession.create("my_sesh")
