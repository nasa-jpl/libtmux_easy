"""Helpers shared by the test modules."""
import os
import subprocess
import sys
import time
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")  # for subprocesses: this checkout


def wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def fmt(session, target, fmt_):
    """A tmux format, expanded for 'target'."""
    return session.server.cmd("display-message", "-p", "-t", target, fmt_).stdout[0]


def pane_text(pane):
    return "\n".join(pane.server.cmd("capture-pane", "-p", "-J", "-t", pane.id).stdout)


def title(pane):
    return pane.server.cmd("display-message", "-p", "-t", pane.id, "#{pane_title}").stdout[0]


def lte_session(*argv, env=None, cwd=None, timeout=60):
    """Run lte-session (this checkout's), headless: outside tmux, no TTY."""
    env = {k: v for k, v in (os.environ if env is None else env).items()
           if k not in ("TMUX", "TMUX_PANE")}
    env["PYTHONPATH"] = SRC
    return subprocess.run([sys.executable, "-m", "libtmux_easy.launch", *argv],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          env=env, cwd=cwd, timeout=timeout)
