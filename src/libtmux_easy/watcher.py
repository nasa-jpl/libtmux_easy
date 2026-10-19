"""The per-session background thread: pidfd exit watches and the wrap-mode
control socket. See _PaneWatcher."""

import json
import logging
import os
import selectors
import threading
from dataclasses import dataclass
from typing import Optional

from .proc import proc_alive
from .status import ExitStatus

logger = logging.getLogger(__name__)


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
    """The latest lte-run in a pane. See docs/wrap-mode.md."""

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
            alive = proc_alive(proc)
            fd = None
            if alive and hasattr(os, "pidfd_open"):
                try:
                    fd = os.pidfd_open(proc.pid)
                except (OSError, ProcessLookupError):
                    # Raced with the process exiting between the alive-check
                    # above and the open -- re-check rather than assume.
                    fd = None
                    alive = proc_alive(proc)
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
                    watch.poll_procs = [p for p in watch.poll_procs if proc_alive(p)]
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
