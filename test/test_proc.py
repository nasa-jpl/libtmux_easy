"""
Automated tests for libtmux_easy.proc -- process discovery/inspection helpers.

get_child_pids tests spawn real subprocesses (via subprocess.Popen) and
verify psutil-based discovery against them. pane_child_processes/
get_session_pids tests create actual tmux sessions, mirroring test_session.py.
"""
import os
import subprocess

from helpers import wait_until
from libtmux_easy.proc import get_child_pids, pane_child_processes, get_session_pids


def test_get_child_pids():
    """
    test process (os.getpid())
      `-- sh -c "sleep 5; true"   (proc.pid, direct child)
            `-- sleep 5           (grandchild, forked by sh; a leaf)

    recursive=False stops at sh; recursive=True reaches the sleep too, which
    has no children of its own. A pid that doesn't exist gives [].
    """
    proc = subprocess.Popen(["sh", "-c", "sleep 5; true"])
    try:
        assert wait_until(lambda: len(get_child_pids(os.getpid(), recursive=True)) == 2)
        direct = get_child_pids(os.getpid(), recursive=False)
        recursive = get_child_pids(os.getpid(), recursive=True)

        assert direct == [proc.pid]
        (grandchild,) = set(recursive) - set(direct)
        assert get_child_pids(grandchild) == []
    finally:
        proc.terminate()
        proc.wait()
    assert get_child_pids(2**30) == []


def test_pane_and_session_pids(session):
    """
    the session
      `-- window "main"
            |-- pane "shell"      (idle -- no children, omitted from result)
            `-- pane "sleeper"    (busy.pane_pid)
                  `-- sleep 100
    """
    idle = session.new_pane("shell", "# ready")
    busy = session.new_pane("sleeper", "sleep 100")
    assert wait_until(lambda: any(p.name() == "sleep" for p in pane_child_processes(busy)))

    assert pane_child_processes(idle) == []
    result = get_session_pids(session)
    assert list(result) == ["main"]
    assert list(result["main"]) == [busy.id]
    assert len(result["main"][busy.id]) >= 1

    for p in pane_child_processes(busy):
        p.terminate()
    assert wait_until(lambda: get_session_pids(session) == {})
