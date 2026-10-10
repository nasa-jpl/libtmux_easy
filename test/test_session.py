"""
Automated tests for libtmux_easy.

These tests create actual tmux sessions and verify behavior.
They clean up after themselves by killing test sessions.
"""
import time

import pytest
from libtmux_easy import TmuxSession, session_exists, PANE_MODE_EXEC
from libtmux_easy.proc import pane_child_processes
import libtmux


class TestSessionCreation:
    def test_create_session(self):
        """Test basic session creation"""
        session = TmuxSession.create("test-session-1", kill_existing=True)

        assert session is not None
        assert session.session_name == "test-session-1"
        assert session_exists("test-session-1")

        # Cleanup
        try:
            session.kill_session()
        except:
            session.kill()

    def test_create_kills_existing(self):
        """Test that create with kill_existing=True replaces old session"""
        session1 = TmuxSession.create("test-session-2", kill_existing=True)
        session2 = TmuxSession.create("test-session-2", kill_existing=True)

        assert session2 is not None

        # Cleanup
        try:
            session2.kill_session()
        except:
            session2.kill()

    def test_create_raises_if_existing_and_not_kill_existing(self):
        """Test that create with kill_existing=False raises if the session already exists"""
        session1 = TmuxSession.create("test-session-3", kill_existing=True)

        with pytest.raises(ValueError, match="already exists"):
            TmuxSession.create("test-session-3", kill_existing=False)

        # Cleanup
        try:
            session1.kill_session()
        except:
            session1.kill()


class TestWindows:
    @pytest.fixture
    def session(self):
        """Create a test session"""
        sess = TmuxSession.create("test-windows", kill_existing=True)
        yield sess
        try:
            sess.kill_session()
        except:
            sess.kill()

    def test_new_window(self, session):
        """Test creating a new window"""
        window = session.new_window("test-window")

        assert window is not None
        assert window.name == "test-window"
        assert len(session.windows) == 1

    def test_unique_window_names(self, session):
        """Test that duplicate window names raise error"""
        session.new_window("window1")

        with pytest.raises(ValueError, match="already exists"):
            session.new_window("window1")

    def test_select_window(self, session):
        """Test window selection"""
        w1 = session.new_window("win1")
        w2 = session.new_window("win2")

        session.select_window("win1")
        assert session.current_window.name == "win1"


class TestPanes:
    @pytest.fixture
    def session(self):
        """Create a test session with a window"""
        sess = TmuxSession.create("test-panes", kill_existing=True)
        sess.new_window("main")
        yield sess
        try:
            sess.kill_session()
        except:
            sess.kill()

    def test_new_pane(self, session):
        """Test creating a new pane"""
        pane = session.new_pane("pane1", "echo test")

        assert pane is not None
        assert session.get_pane_name(pane) == "main:pane1"

    def test_unique_pane_names_per_window(self, session):
        """Test that duplicate pane names in same window raise error"""
        session.new_pane("pane1", "echo 1")
        session.new_pane("pane2", "echo 2")

        # Should raise ValueError for duplicate name
        with pytest.raises(ValueError, match="already exists"):
            session.new_pane("pane1", "echo 3")

    def test_pane_names_unique_per_window_not_session(self, session):
        """Test that pane names can repeat across windows"""
        session.new_pane("pane1", "echo window1")

        session.new_window("window2")
        pane2 = session.new_pane("pane1", "echo window2")  # Should not raise

        assert session.get_pane_name(pane2) == "window2:pane1"

    def test_get_pane_by_name(self, session):
        """Test pane lookup by name"""
        created_pane = session.new_pane("test-pane", "echo test")
        retrieved_pane = session.get_pane("main:test-pane")

        assert retrieved_pane is not None
        assert retrieved_pane.id == created_pane.id

    def test_select_pane(self, session):
        """Test pane selection"""
        p1 = session.new_pane("p1", "echo 1")
        p2 = session.new_pane("p2", "echo 2")

        # Verify we can select by name
        result = session.select_pane("main:p1")
        assert result is not None
        # Pane was selected in tmux
        assert result.id == p1.id


class TestCommands:
    @pytest.fixture
    def session(self):
        """Create a test session with window and pane"""
        sess = TmuxSession.create("test-commands", kill_existing=True)
        sess.new_window("main")
        sess.new_pane("shell", "# ready")
        yield sess
        try:
            sess.kill_session()
        except:
            sess.kill()

    def test_send_keys(self, session):
        """Test sending commands to pane"""
        # Should not raise
        session.send_keys("echo test", enter=False)

    def test_send_keys_to_target(self, session):
        """Test sending commands to specific pane"""
        session.new_pane("pane2", "# ready")

        # Should not raise
        session.send_keys("echo target", target_pane="main:pane2", enter=False)

    def test_command_before_global(self, session):
        """Test global command_before"""
        session.send_command_before("cd /tmp")

        assert session.command_before["all"] == "cd /tmp"

    def test_command_before_window(self, session):
        """Test window-specific command_before"""
        session.send_command_before("cd /tmp", window="main")

        assert session.current_window.id in session.command_before


class TestLayouts:
    @pytest.fixture
    def session(self):
        """Create a test session"""
        sess = TmuxSession.create("test-layouts", kill_existing=True)
        yield sess
        try:
            sess.kill_session()
        except:
            sess.kill()

    def test_window_with_layout(self, session):
        """Test creating window with specific layout"""
        # The first window reuses the session's initial window; it must get
        # layout_str too, or split_window() never re-tiles it.
        window = session.new_window("tiled-window", layout="tiled")
        assert window.layout_str == "tiled"

        window2 = session.new_window("second", layout="main-horizontal")
        assert window2.layout_str == "main-horizontal"

    def test_first_window_fits_many_panes(self, session):
        """Regression: the first window used to skip re-tiling, so every split
        halved the previous pane and the 6th failed with 'no space for new
        pane' even in a default 80x24 window."""
        session.new_window("many")
        for i in range(8):
            session.new_pane(f"p{i}", "sleep 30", target_pid_timeout=0.1)
        assert len(session.get_window("many").panes) == 8

    def test_main_horizontal_layout(self, session):
        """Test main-horizontal layout"""
        window = session.new_window("horiz", layout="main-horizontal")
        session.new_pane("p1", "echo 1")
        session.new_pane("p2", "echo 2")

        # Just verify no errors occurred
        assert len(window.panes) >= 2


class TestVersionCompatibility:
    def test_libtmux_version_detected(self):
        """Test that version detection works"""
        from libtmux_easy.tmux_lib import libtmux_version
        from packaging import version

        assert libtmux_version is not None
        assert libtmux_version >= version.parse("0.10.1")


class TestTeardown:
    @pytest.fixture
    def session(self):
        """Create a test session with a window"""
        sess = TmuxSession.create("test-teardown", kill_existing=True)
        sess.new_window("main")
        yield sess
        try:
            sess.kill_session()
        except:
            sess.kill()

    def test_stop_pane_sigint_cleanly_stops(self, session):
        """Test that stop_pane SIGINTs a well-behaved process and returns True"""
        pane = session.new_pane("sleeper", "sleep 100")
        time.sleep(0.5)  # let the process actually start

        clean = session.stop_pane(pane, sigint_timeout=5.0)

        assert clean is True

    def test_stop_pane_already_idle(self, session):
        """Test that stop_pane returns True immediately for an idle pane"""
        pane = session.new_pane("shell", "# ready")
        time.sleep(0.2)

        clean = session.stop_pane(pane, sigint_timeout=5.0)

        assert clean is True

    def test_wait_for_pane_idle_timeout(self, session):
        """Test that wait_for_pane_idle returns False if the pane still has a running
        process when the timeout elapses (it only polls -- it never sends any signal)"""
        pane = session.new_pane("sleeper", "sleep 100")
        time.sleep(0.5)

        idle = session.wait_for_pane_idle(pane, timeout=1.0, poll_interval=0.1)

        assert idle is False

        # Cleanup
        for proc in pane_child_processes(session.resolve_target_pane(pane)):
            proc.terminate()

    def test_stop_pane_escalates_past_sigint(self, session):
        """Test that stop_pane escalates to SIGTERM when a process ignores SIGINT,
        and still ends up stopping it"""
        pane = session.new_pane("trapper", "trap '' INT; sleep 100")
        time.sleep(0.5)

        clean = session.stop_pane(pane, sigint_timeout=1.0, sigterm_timeout=5.0)

        assert clean is True


class TestExitStatusShellMode:
    @pytest.fixture
    def session(self):
        """Create a test session with a window"""
        sess = TmuxSession.create("test-exit-status-shell", kill_existing=True)
        sess.new_window("main")
        yield sess
        try:
            sess.kill_session()
        except:
            sess.kill()

    def test_self_exit_code_readable(self, session):
        """A command that exits on its own: '$?' holds its real code."""
        pane = session.new_pane("x", "sh -c 'sleep 1; exit 42'")
        session.wait_for_pane_idle(pane, timeout=5.0)

        status = session.exit_status(pane)

        assert status.code == 42
        assert status.signal is None
        assert status.ok is False

    def test_zero_exit_is_ok(self, session):
        pane = session.new_pane("x", "sh -c 'exit 0'")
        session.wait_for_pane_idle(pane, timeout=5.0)

        status = session.exit_status(pane)

        assert status.code == 0
        assert status.ok is True

    def test_sigint_reported_as_inferred_signal(self, session):
        """Ctrl-C shows up as 128+2, decoded to signal=2 with inferred=True."""
        pane = session.new_pane("x", "sh -c 'exec sleep 300'")
        time.sleep(0.5)

        session.stop_pane(pane, sigint_timeout=5.0)
        status = session.exit_status(pane)

        assert status.signal == 2
        assert status.inferred is True
        assert status.ok is False

    def test_sigint_handler_exit_code_preserved(self, session):
        """The ros2-launch shape: traps SIGINT and exits 7."""
        cmd = (
            "python3 -c "
            "'import signal, sys, time; "
            "signal.signal(signal.SIGINT, lambda *a: sys.exit(7)); "
            "time.sleep(300)'"
        )
        pane = session.new_pane("x", cmd)
        time.sleep(0.5)

        session.stop_pane(pane, sigint_timeout=5.0)
        status = session.exit_status(pane)

        assert status.code == 7

    def test_status_cached_across_calls(self, session):
        """Second call must not re-probe -- the first echo clobbered '$?'."""
        pane = session.new_pane("x", "sh -c 'sleep 1; exit 42'")
        session.wait_for_pane_idle(pane, timeout=5.0)

        first = session.exit_status(pane)
        second = session.exit_status(pane)

        assert first == second
        assert second.code == 42

    def test_unknown_while_running(self, session):
        pane = session.new_pane("x", "sleep 100")
        time.sleep(0.5)

        status = session.exit_status(pane)

        assert status.known is False

        # Cleanup
        for proc in pane_child_processes(session.resolve_target_pane(pane)):
            proc.terminate()


class TestExitStatusExecMode:
    @pytest.fixture
    def session(self):
        """Create a test session with a window. Commands below all have a
        deliberate delay before exiting so remain-on-exit has time to apply
        before the pane's process ends (see tmux_lib.py note on fact 6)."""
        sess = TmuxSession.create("test-exit-status-exec", kill_existing=True)
        sess.new_window("main")
        yield sess
        try:
            sess.kill_session()
        except:
            sess.kill()

    def test_exit_code_from_tmux(self, session):
        pane = session.new_pane("x", "sh -c 'sleep 1; exit 42'", mode=PANE_MODE_EXEC)
        session.wait_for_pane_idle(pane, timeout=5.0)

        status = session.exit_status(pane)

        assert status.code == 42
        assert status.inferred is False

    def test_signal_distinguished_from_code(self, session):
        """Exec mode separates 'killed by SIGINT' from 'exited 130'."""
        pane = session.new_pane("x", "sh -c 'exec sleep 300'", mode=PANE_MODE_EXEC)
        time.sleep(0.5)

        session.stop_pane(pane, sigint_timeout=5.0)
        status = session.exit_status(pane)

        assert status.signal == 2
        assert status.code is None
        assert status.inferred is False

    def test_pane_survives_command_exit(self, session):
        """remain-on-exit keeps the pane (and its status) after the process ends."""
        pane = session.new_pane("x", "sh -c 'sleep 1; exit 0'", mode=PANE_MODE_EXEC)
        session.wait_for_pane_idle(pane, timeout=5.0)

        assert session._pane_fmt(pane, "#{pane_dead}") == "1"
        assert pane.id in [p.id for p in session.current_window.panes]

    def test_reused_first_pane_runs_command(self, session):
        """Bug 1: the window's initial pane must actually run the command
        rather than sit at a bash prompt."""
        pane = session.new_pane("x", "sh -c 'sleep 1; exit 7'", mode=PANE_MODE_EXEC)
        session.wait_for_pane_idle(pane, timeout=5.0)

        assert session.exit_status(pane).code == 7

    def test_stop_pane_actually_stops_childless_command(self, session):
        """Bug 2: 'exec sleep 300' has no descendants, so the old
        descendants-only liveness check reported a clean stop without
        signalling anything."""
        pane = session.new_pane("x", "sh -c 'exec sleep 300'", mode=PANE_MODE_EXEC)
        time.sleep(0.5)

        assert session.is_running(pane) is True
        assert session.stop_pane(pane, sigint_timeout=5.0) is True
        assert session.is_running(pane) is False

    def test_command_before_composes_with_exec(self, session):
        """send_command_before must still apply, via 'setup && exec cmd', and
        the status must be the COMMAND's, not the setup shell's."""
        session.send_command_before("true")
        pane = session.new_pane("x", "sh -c 'exit 5'", mode=PANE_MODE_EXEC)
        session.wait_for_pane_idle(pane, timeout=5.0)

        assert session.exit_status(pane).code == 5

    def test_respawn_pane_reruns_and_clears_status(self, session):
        pane = session.new_pane("x", "sh -c 'sleep 1; exit 11'", mode=PANE_MODE_EXEC)
        session.wait_for_pane_idle(pane, timeout=5.0)
        assert session.exit_status(pane).code == 11

        session.respawn_pane(pane, "sh -c 'exit 22'")
        session.wait_for_pane_idle(pane, timeout=5.0)

        assert session.exit_status(pane).code == 22


class TestIsRunning:
    @pytest.fixture
    def session(self):
        """Create a test session with a window"""
        sess = TmuxSession.create("test-is-running", kill_existing=True)
        sess.new_window("main")
        yield sess
        try:
            sess.kill_session()
        except:
            sess.kill()

    def test_shell_pane_running_then_idle(self, session):
        pane = session.new_pane("x", "sh -c 'sleep 1'")
        time.sleep(0.3)

        assert session.is_running(pane) is True

        session.wait_for_pane_idle(pane, timeout=5.0)
        assert session.is_running(pane) is False

    def test_exec_pane_running_then_dead(self, session):
        pane = session.new_pane("x", "sh -c 'sleep 1'", mode=PANE_MODE_EXEC)
        time.sleep(0.3)

        assert session.is_running(pane) is True

        session.wait_for_pane_idle(pane, timeout=5.0)
        assert session.is_running(pane) is False


class TestIntegration:
    def test_full_workflow(self):
        """Test complete session setup workflow"""
        session = TmuxSession.create("integration-test", kill_existing=True)

        # Create first window with panes
        session.new_window("work")
        session.new_pane("editor", "echo vim", enter=False)
        session.new_pane("shell", "echo bash", enter=False)

        # Create second window with layout
        session.new_window("monitoring", layout="main-horizontal")
        session.new_pane("logs", "echo logs", enter=False)
        session.new_pane("htop", "echo htop", enter=False)

        # Verify structure
        assert len(session.windows) == 2
        assert session.get_window("work") is not None
        assert session.get_window("monitoring") is not None
        assert session.get_pane("work:editor") is not None
        assert session.get_pane("monitoring:logs") is not None

        # Cleanup
        try:
            session.kill_session()
        except:
            session.kill()
