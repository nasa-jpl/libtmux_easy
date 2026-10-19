"""Shared fixtures. The tests drive a real tmux, on a server of their own."""
import os
import shutil
import subprocess
import tempfile

import pytest

from libtmux_easy import TmuxSession

# A private tmux server per test run, so the tests never touch your sessions
# and two runs don't collide. TMUX_TMPDIR moves the default socket for every
# plain 'tmux': the library, libtmux, lte-dash, the tests, child processes.
# $TMUX would win over it, so forget any tmux pytest was started from.
_TMPDIR = tempfile.mkdtemp(prefix="lte-tests-")
os.environ["TMUX_TMPDIR"] = _TMPDIR
for _var in ("TMUX", "TMUX_PANE", "LTE_DIR", "LTE_RUN_TEE"):
    os.environ.pop(_var, None)
# The same bindings with or without a ~/.tmux.conf (test_starter.py turns it on).
os.environ["LTE_TMUX_DEFAULTS"] = "0"
# Start the server with no config (not yours: ~/.tmux.conf, /etc/tmux.conf),
# and keep it up when a test kills its last session: a later 'tmux' would
# start a fresh server, which would read ~/.tmux.conf.
KEEPALIVE = "zz-keepalive"  # no test name is a prefix of it: tmux matches by prefix
subprocess.run(["tmux", "-f", "/dev/null", "new-session", "-d", "-s", KEEPALIVE], check=True)


def pytest_report_header():
    socket = os.path.join(_TMPDIR, f"tmux-{os.getuid()}", "default")
    return f"tmux server: tmux -S {socket} ls"


def pytest_sessionfinish():
    subprocess.run(["tmux", "kill-server"], capture_output=True)
    shutil.rmtree(_TMPDIR, ignore_errors=True)


@pytest.fixture
def make_session():
    """make_session(name="lte-test", window="main", **create_kwargs); each
    session made is killed after the test."""
    made = []

    def make(name="lte-test", window="main", **kwargs):
        sess = TmuxSession.create(name, kill_existing=True, **kwargs)
        if window:
            sess.new_window(window)
        made.append(sess)
        return sess

    yield make
    for sess in made:
        try:
            sess.kill()
        except Exception:  # already gone, e.g. replaced by a later create()
            pass


@pytest.fixture
def session(make_session):
    """A session with one window, 'main'."""
    return make_session()
