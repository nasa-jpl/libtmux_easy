"""The sidebar's links to the script: the status file in, actions out."""

import json
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
