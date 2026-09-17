"""
Automated tests for libtmux_easy.

These tests create actual tmux sessions and verify behavior.
They clean up after themselves by killing test sessions.
"""
import time

import pytest
from libtmux_easy import TmuxSession, session_exists
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
        # First window reuses unnamed window, doesn't set layout_str
        window = session.new_window("tiled-window", layout="tiled")

        # Create second window to test layout_str is actually set
        window2 = session.new_window("second", layout="main-horizontal")
        assert hasattr(window2, "layout_str")
        assert window2.layout_str == "main-horizontal"

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
