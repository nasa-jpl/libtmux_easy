#!/usr/bin/env python3
"""lte-run: run a command under libtmux_easy supervision.

    lte-run [--] COMMAND [ARG...]

Prepend it to any command typed into a pane of a libtmux_easy session. It
reports the command's pid and exact exit status to the session's Python
process, and accepts control messages (e.g. "send SIGINT") back. Outside such
a session, or once that process has gone away, it simply runs the command --
same output, same exit status -- so it is always safe to prepend.
"""

import json
import os
import resource
import signal
import socket
import subprocess
import sys
import threading
import time

PROTOCOL_VERSION = 1
DIR_OPTION = "@lte_dir"   # tmux session option set by TmuxSession
DIR_ENV = "LTE_DIR"       # overrides DIR_OPTION; for tests and non-tmux use
SOCKET_NAME = "ctl.sock"

# The terminal sends these to the whole foreground process group, so the
# target already gets them directly. Ignoring them is what lets the wrapper
# outlive the target and report how it ended.
_TERMINAL_SIGNALS = (signal.SIGINT, signal.SIGQUIT)

# These arrive at the wrapper specifically (kill <pid>, the pane closing).
# Passed on rather than dying, so the target is not orphaned.
_FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


def find_dir():
    """The session's control directory, or None when unsupervised."""
    if os.environ.get(DIR_ENV):
        return os.environ[DIR_ENV]
    pane = os.environ.get("TMUX_PANE")
    if not pane or not os.environ.get("TMUX"):
        return None
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{%s}" % DIR_OPTION],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


class Channel:
    """Newline-delimited JSON over a unix stream socket. Every method is a
    no-op when unconnected, so supervised and unsupervised runs share one
    code path."""

    def __init__(self, directory):
        self._sock = None
        self._send_lock = threading.Lock()
        if directory is None:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(os.path.join(directory, SOCKET_NAME))
        except OSError:
            sock.close()
            return  # nobody listening, e.g. the session's script has exited
        # Close-on-exec by default (PEP 446), so the target does not inherit
        # it -- which matters: an inherited copy would hide our EOF.
        self._sock = sock

    def send(self, **msg):
        sock = self._sock
        if sock is None:
            return
        with self._send_lock:
            try:
                sock.sendall((json.dumps(msg) + "\n").encode())
            except OSError:
                self._sock = None  # listener went away; carry on unsupervised

    def messages(self):
        """Yield control messages until the connection closes."""
        sock = self._sock
        if sock is None:
            return
        buf = b""
        while True:
            try:
                chunk = sock.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                try:
                    yield json.loads(line)
                except ValueError:
                    pass


def _proc_table():
    """pid -> (ppid, pgrp), from /proc."""
    table = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat") as f:
                # Field 2 (comm) may contain spaces and parens; split after it.
                fields = f.read().rsplit(")", 1)[1].split()
            table[int(name)] = (int(fields[1]), int(fields[2]))
        except (OSError, ValueError, IndexError):
            pass  # exited mid-scan
    return table


def signal_target(sig, target):
    """Deliver 'sig' to the target and every descendant of ours in our process
    group -- what a Ctrl-C at the terminal reaches, minus the wrapper itself.

    The descendant filter matters without job control (e.g. 'sh -c "lte-run
    ..."'), where our process group also contains whatever started us."""
    try:
        table = _proc_table()
    except OSError:
        table = {}
    if not table:  # no /proc: best effort
        try:
            os.kill(target, sig)
        except OSError:
            pass
        return

    me, pgid = os.getpid(), os.getpgrp()
    children = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    stack, seen = list(children.get(me, ())), set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, ()))
        if pid == target or table[pid][1] == pgid:
            try:
                os.kill(pid, sig)
            except OSError:
                pass


def main(argv=None, channel=None):
    """lte-run. 'channel' replaces the session socket: any object with
    send(**msg) and messages(), like Channel (lte-session uses this)."""
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__)
        return 2

    if channel is None:
        channel = Channel(find_dir())

    # Pass the inherited dispositions on to the target. Python installs its
    # own SIGINT handler at startup unless SIGINT was already ignored, so
    # "not SIG_IGN" here means "default" at the OS level.
    inherited_ign = {s: signal.getsignal(s) == signal.SIG_IGN for s in _TERMINAL_SIGNALS}
    for s in _TERMINAL_SIGNALS:
        signal.signal(s, signal.SIG_IGN)

    pid = os.fork()
    if pid == 0:
        for s, ign in inherited_ign.items():
            signal.signal(s, signal.SIG_IGN if ign else signal.SIG_DFL)
        try:
            os.execvp(argv[0], argv)
        except OSError as exc:
            sys.stderr.write(f"lte-run: {argv[0]}: {exc.strerror}\n")
            os._exit(127 if isinstance(exc, FileNotFoundError) else 126)

    channel.send(event="started", version=PROTOCOL_VERSION,
                 pane=os.environ.get("TMUX_PANE"), wrapper_pid=os.getpid(),
                 target_pid=pid, argv=argv, time=time.time())

    for s in _FORWARDED_SIGNALS:
        signal.signal(s, lambda signum, _frame: signal_target(signum, pid))

    def serve():
        for msg in channel.messages():
            if msg.get("cmd") == "signal":
                try:
                    signal_target(int(msg["signal"]), pid)
                except (KeyError, TypeError, ValueError):
                    pass

    threading.Thread(target=serve, daemon=True).start()

    _, status = os.waitpid(pid, 0)  # retried across signal handlers (PEP 475)

    if os.WIFSIGNALED(status):
        code, sig = None, os.WTERMSIG(status)
    else:
        code, sig = os.WEXITSTATUS(status), None
    channel.send(event="exited", target_pid=pid, code=code, signal=sig, time=time.time())

    if sig is not None:
        # Die the same way, so a calling shell sees a signal death rather than
        # an exit code (e.g. Ctrl-C still aborts an enclosing shell loop).
        # No core file for the wrapper itself.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        signal.signal(sig, signal.SIG_DFL)
        os.kill(os.getpid(), sig)
        return 128 + sig  # only reached if 'sig' does not terminate
    return code


if __name__ == "__main__":
    sys.exit(main())
