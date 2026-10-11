"""
Automated tests for libtmux_easy.proc -- process discovery/inspection helpers.

get_child_pids tests spawn real subprocesses (via subprocess.Popen) and
verify psutil-based discovery against them. pane_child_processes/
get_session_pids tests create actual tmux sessions, mirroring test_session.py.
"""
import os
import subprocess
import time


from libtmux_easy.proc import get_child_pids, pane_child_processes, get_session_pids


class TestGetChildPids:
    def test_finds_direct_child(self):
        """
        test process (os.getpid())
          `-- sleep 5   (proc.pid)

        The subprocess should show up as a direct child.
        """
        proc = subprocess.Popen(["sleep", "5"])
        time.sleep(0.2)
        try:
            assert proc.pid in get_child_pids(os.getpid())
        finally:
            proc.terminate()
            proc.wait()

    def test_recursive_flag_controls_grandchild_visibility(self):
        """
        test process (os.getpid())
          `-- sh -c "sleep 5"   (proc.pid, direct child)
                `-- sleep 5     (grandchild, forked by sh)

        recursive=False from the test process should stop at sh (the
        grandchild is invisible); recursive=True should reach it too.
        """
        proc = subprocess.Popen(["sh", "-c", "sleep 5"])
        time.sleep(0.2)
        try:
            direct = get_child_pids(os.getpid(), recursive=False)
            recursive = get_child_pids(os.getpid(), recursive=True)

            assert direct == [proc.pid]
            assert proc.pid in recursive
            assert set(recursive) - set(direct), "expected a grandchild-only pid"
        finally:
            proc.terminate()
            proc.wait()

    def test_leaf_process_has_no_children(self):
        """
        test process (os.getpid())
          `-- sleep 5   (proc.pid, a leaf with no children of its own)
        """
        proc = subprocess.Popen(["sleep", "5"])
        time.sleep(0.2)
        try:
            assert get_child_pids(proc.pid) == []
        finally:
            proc.terminate()
            proc.wait()

    def test_nonexistent_pid_returns_empty(self):
        """A PID that doesn't exist should return [] rather than raising"""
        assert get_child_pids(2**30) == []


class TestPaneChildProcesses:
    def test_running_process_found(self, session):
        """
        the session
          `-- window "main"
                `-- pane "sleeper"   (pane.pane_pid, the pane's shell)
                      `-- sleep 100
        """
        pane = session.new_pane("sleeper", "sleep 100")
        time.sleep(0.5)

        procs = pane_child_processes(pane)

        assert any(p.name() == "sleep" for p in procs)

        for p in procs:
            p.terminate()

    def test_idle_pane_has_no_children(self, session):
        """
        the session
          `-- window "main"
                `-- pane "shell"   (pane.pane_pid, idle -- no children)
        """
        pane = session.new_pane("shell", "# ready")
        time.sleep(0.2)

        assert pane_child_processes(pane) == []


class TestGetSessionPids:
    def test_reports_running_pane_by_window_and_pane_id(self, session):
        """
        the session
          `-- window "main"
                `-- pane "sleeper"   (pane.pane_pid)
                      `-- sleep 100
        """
        pane = session.new_pane("sleeper", "sleep 100")
        time.sleep(0.5)

        result = get_session_pids(session)

        assert "main" in result
        assert pane.id in result["main"]
        assert len(result["main"][pane.id]) >= 1

        for p in pane_child_processes(pane):
            p.terminate()

    def test_idle_session_reports_no_entries(self, session):
        """
        the session
          `-- window "main"
                `-- pane "shell"   (pane.pane_pid, idle -- no children)
        """
        session.new_pane("shell", "# ready")
        time.sleep(0.2)

        assert get_session_pids(session) == {}

    def test_mixed_session_only_reports_running_pane(self, session):
        """
        the session
          `-- window "main"
                |-- pane "shell"      (idle -- no children, omitted from result)
                `-- pane "sleeper"    (busy_pane.pane_pid)
                      `-- sleep 100
        """
        session.new_pane("shell", "# ready")
        busy_pane = session.new_pane("sleeper", "sleep 100")
        time.sleep(0.5)

        result = get_session_pids(session)

        assert list(result["main"].keys()) == [busy_pane.id]

        for p in pane_child_processes(busy_pane):
            p.terminate()
