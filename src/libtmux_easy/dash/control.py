"""The sidebar's links to the script: the status file in, actions out."""

import json
import signal
import socket


class Control:
    """Sends actions to the session's script over its control socket (the
    one lte-run reports to). A fresh connection per action: the script may
    have exited, or restarted, since the last one."""

    def __init__(self, path):
        self._path = path  # a path, or a callable returning one (discovery)

    @property
    def path(self):
        return self._path() if callable(self._path) else self._path

    def online(self):
        return self._send(None)

    def send(self, cmd, pane_id):
        return self._send({"cmd": cmd, "pane": pane_id})

    def _send(self, msg):
        if not self.path:
            return False
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(1.0)
            sock.connect(self.path)
            if msg is not None:
                sock.sendall((json.dumps(msg) + "\n").encode())
            return True
        except OSError:
            return False
        finally:
            sock.close()


def read_status(path):
    """The session's status file as a list of dicts (see PaneStatus), or
    None when it can't be read (not written yet, or mid-rewrite)."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def describe(entry):
    """(short label, color class) for one status-file entry."""
    if entry.get("starting"):
        return "starting", "starting"
    if entry.get("running"):
        return "running", "running"
    stopped = entry.get("stopped")
    if stopped:
        # Escalation past SIGINT is worth seeing: it means no clean shutdown.
        return ("stopped" if stopped == "SIGINT" else f"stopped {stopped[3:]}"), "stopped"
    exit_status = entry.get("exit_status") or {}
    code, sig = exit_status.get("code"), exit_status.get("signal")
    if sig is not None:
        try:
            name = signal.Signals(sig).name
        except ValueError:
            name = f"signal {sig}"
        return name + ("?" if exit_status.get("inferred") else ""), "failed"
    if code is not None:
        return f"exit {code}", "ok" if code == 0 else "failed"
    return "unknown", "unknown"


def describe_long(entry):
    if entry.get("starting"):
        return "starting: typed, lte-run has not reported yet"
    if entry.get("running"):
        return "running"
    if entry.get("stopped"):
        plain = dict(entry, stopped=None)
        return f"stopped, needed {entry['stopped']}: {describe_long(plain)}"
    exit_status = entry.get("exit_status") or {}
    code, sig = exit_status.get("code"), exit_status.get("signal")
    if sig is not None:
        text = f"killed by {describe(entry)[0].rstrip('?')} ({sig})"
        return text + ", inferred from 128+N" if exit_status.get("inferred") else text
    if code is not None:
        return f"exited {code}"
    return "unknown"
