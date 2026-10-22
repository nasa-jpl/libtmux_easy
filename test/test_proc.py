"""
Automated tests for libtmux_easy.proc -- process discovery/inspection helpers.

get_child_pids tests spawn real subprocesses (via subprocess.Popen) and
verify psutil-based discovery against them. pane_child_processes/
get_session_pids tests create actual tmux sessions, mirroring test_session.py.
"""
import os
import subprocess

from helpers import wait_until
from libtmux_easy.proc import (cmdline, descendant_tree, get_child_pids, get_session_pids,
                               pane_child_processes)


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


def test_descendant_tree():
    """
    1 init
      |-- 10 a
      |     |-- 11 b
      |     |     `-- 13 d
      |     `-- 12 c
      `-- 20 e

    ps axf order: depth first, siblings by pid. Leaves and unknown pids give [].
    """
    table = {pid: ("S", ppid, pid) for pid, ppid in
             ((1, 0), (20, 1), (12, 10), (10, 1), (13, 11), (11, 10))}
    assert descendant_tree(10, table) == [(1, 11), (2, 13), (1, 12)]
    assert descendant_tree(1, table) == [(1, 10), (2, 11), (3, 13), (2, 12), (1, 20)]
    assert descendant_tree(13, table) == []
    assert descendant_tree(99, table) == []


def test_descendant_tree_and_cmdline_of_real_processes():
    proc = subprocess.Popen(["sh", "-c", "sleep 5; true"])
    try:
        assert wait_until(lambda: descendant_tree(proc.pid) != [])
        ((depth, sleeper),) = descendant_tree(proc.pid)
        assert depth == 1
        assert cmdline(proc.pid) == "sh -c sleep 5; true"
        assert cmdline(sleeper) == "sleep 5"
    finally:
        proc.terminate()
        proc.wait()
    assert cmdline(2**30) == ""


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
