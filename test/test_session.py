"""TmuxSession: sessions, windows and panes; exit status in shell and exec
mode; stopping (stop_pane, stop_all) and the stopped state."""
import time

import pytest

from helpers import pane_text, title, wait_until
from libtmux_easy import PANE_MODE_EXEC, PANE_MODE_WRAP, TmuxSession, session_exists
from libtmux_easy import dash


def test_create_and_kill_existing(make_session):
    session = make_session("lte-test-create", window=None)
    assert session.session_name == "lte-test-create"
    assert session_exists("lte-test-create")
    with pytest.raises(ValueError, match="already exists"):
        TmuxSession.create("lte-test-create", kill_existing=False)
    assert make_session("lte-test-create", window=None) is not None


class TestWindowsAndPanes:
    def test_first_window_reuses_the_initial_one(self, make_session):
        session = make_session(window=None)
        window = session.new_window("w")
        assert window.name == "w"
        assert len(session.windows) == 1

    def test_names_lookup_and_select(self, session):
        """Names are unique per window, and find panes and windows."""
        p1 = session.new_pane("p1", "true")
        p2 = session.new_pane("p2", "true")
        with pytest.raises(ValueError, match="already exists"):
            session.new_pane("p1", "true")
        with pytest.raises(ValueError, match="already exists"):
            session.new_window("main")
        session.new_window("other")
        assert session.get_pane_name(session.new_pane("p1", "true")) == "other:p1"

        assert session.get_pane("main:p1").id == p1.id
        assert session.get_pane(p2.id).id == p2.id
        assert session.select_pane("main:p1").id == p1.id
        assert session.select_window("other").name == "other"

    def test_send_keys_and_command_before(self, session):
        session.send_command_before("export LTE_X=from_before")
        pane = session.new_pane("p", "echo before=$LTE_X")
        session.send_keys("echo sent_$((6*7))", target_pane="main:p")

        assert wait_until(lambda: "sent_42" in pane_text(pane))
        assert "before=from_before" in pane_text(pane)
        assert session.commands["main:p"] == ["echo before=$LTE_X", "echo sent_$((6*7))"]

    def test_default_mode(self, make_session):
        session = make_session(mode=PANE_MODE_EXEC)
        exec_pane = session.new_pane("a", "sleep 30")
        assert session.set_default_mode(PANE_MODE_WRAP) == PANE_MODE_EXEC
        wrap_pane = session.new_pane("b", "sleep 30")
        shell_pane = session.new_pane("c", "sleep 30", mode="shell")  # explicit wins

        assert session.pane_modes[exec_pane.id] == PANE_MODE_EXEC
        assert session.pane_modes[wrap_pane.id] == PANE_MODE_WRAP
        assert session.pane_modes[shell_pane.id] == "shell"
        with pytest.raises(ValueError, match="mode must be one of"):
            session.set_default_mode("bogus")
        with pytest.raises(ValueError, match="mode must be one of"):
            TmuxSession.create("lte-test-bad-mode", mode="bogus")

    def test_env_levels(self, make_session):
        """Session, window and pane env merge, later levels winning; the
        window's reused first pane gets it too; SKIP_ENV never goes in."""
        session = make_session(window=None, env={"LTE_A": "session", "LTE_B": "session",
                                                 "TERM": "bogus", "TMUX_PANE": "%bogus"})
        session.new_window("w", env={"LTE_B": "window", "LTE_C": "window"})
        show = "echo a=$LTE_A,b=$LTE_B,c=$LTE_C,term=$TERM,pane=$TMUX_PANE"
        first = session.new_pane("first", show)
        second = session.new_pane("second", show, env={"LTE_C": "pane"})

        assert wait_until(lambda: "a=session,b=window,c=window," in pane_text(first))
        assert wait_until(lambda: "a=session,b=window,c=pane," in pane_text(second))
        for pane in (first, second):
            assert "term=bogus" not in pane_text(pane)
            assert f"pane={pane.id}" in pane_text(pane)

    def test_copy_environment(self, session):
        """Panes started afterwards get it, with no sourcing step; a hand-made
        pane too, as it is the session's tmux environment."""
        session.copy_environment({"LTE_COPIED": "hello"})
        pane = session.new_pane("p", "echo got=$LTE_COPIED")
        assert wait_until(lambda: "got=hello" in pane_text(pane))
        out = session.server.cmd("show-environment", "-t", session.id, "LTE_COPIED").stdout
        assert out == ["LTE_COPIED=hello"]

    def test_exec_respawn_keeps_env(self, session, tmp_path):
        out = tmp_path / "v"
        pane = session.new_pane("p", f"sh -c 'echo $LTE_V > {out}'", mode=PANE_MODE_EXEC,
                                env={"LTE_V": "kept"})
        assert wait_until(lambda: out.exists() and out.read_text() == "kept\n")
        out.unlink()
        session.respawn_pane(pane)
        assert wait_until(lambda: out.exists() and out.read_text() == "kept\n")

    def test_layouts(self, make_session):
        session = make_session(window=None)
        # The first window must get layout_str too, or splits are never
        # re-tiled and the 6th fails with 'no space for new pane'.
        window = session.new_window("many", layout="main-horizontal")
        assert window.layout_str == "main-horizontal"
        for i in range(8):
            session.new_pane(f"p{i}", "sleep 30", target_pid_timeout=0.1)
        assert len(session.get_window("many").panes) == 8

    def test_title_shows_status(self, session):
        """Titles look like 'p [running]'. A shell-split tmux command string
        used to drop everything after the space."""
        pane = session.new_pane("p", "sh -c 'sleep 0.3; exit 3'")
        assert title(pane) == "p [running]"
        assert wait_until(lambda: title(pane) == "p [exited 3]")

    def test_text_with_dash_t_goes_to_its_pane(self, session):
        """libtmux's Pane.cmd() drops its own '-t <pane>' when any argument
        merely contains '-t' (e.g. '--topic'), so the keys went to the active
        pane. Here the first pane is the active one."""
        first = session.new_pane("first", "true")
        second = session.new_pane("second", "echo tag-$((6*7)) --topic x")
        session.set_pane_title(second, "my-topic")

        assert wait_until(lambda: "tag-42" in pane_text(second))
        assert "tag-42" not in pane_text(first)
        assert title(second).startswith("my-topic")
        assert not title(first).startswith("my-topic")


class TestShellMode:
    def test_exit_code(self, session):
        pane = session.new_pane("x", "sh -c 'sleep 0.5; exit 42'")
        assert session.is_running(pane)
        assert session.exit_status(pane).known is False

        session.wait_for_pane_idle(pane, timeout=5.0)
        status = session.exit_status(pane)
        assert (status.code, status.signal, status.ok) == (42, None, False)
        assert session.exit_status(pane) == status  # cached: a re-probe would read 0

    def test_sigint_is_inferred(self, session):
        """Ctrl-C shows up as $? = 130: signal 2, but only inferred."""
        pane = session.new_pane("x", "sh -c 'exec sleep 300'")
        session.stop_pane(pane, sigint_timeout=5.0)
        status = session.exit_status(pane)
        assert (status.signal, status.inferred) == (2, True)

    def test_sigint_handler_exit_code(self, session):
        cmd = ("python3 -c 'import signal, sys, time; "
               "signal.signal(signal.SIGINT, lambda *a: sys.exit(7)); time.sleep(300)'")
        pane = session.new_pane("x", cmd)
        time.sleep(0.5)  # until the handler is installed
        session.stop_pane(pane, sigint_timeout=5.0)
        assert session.exit_status(pane).code == 7


def _exec_status(session, pane):
    """exit_status() of an exec pane; xfail if tmux lost it. tmux sometimes
    never records an exec pane's status (docs/KNOWN_ISSUES.md), more often
    under pytest for a command that sleeps, then exits."""
    status = session.exit_status(pane)
    if status.code is None and status.signal is None:
        pytest.xfail("tmux lost the status (docs/KNOWN_ISSUES.md)")
    return status


class TestExecMode:

    def test_exit_code_from_tmux(self, session):
        """Also: the window's initial pane runs the command, not a shell."""
        pane = session.new_pane("x", "sh -c 'sleep 0.5; exit 42'", mode=PANE_MODE_EXEC)
        session.wait_for_pane_idle(pane, timeout=5.0)

        assert session._pane_fmt(pane, "#{pane_dead}") == "1"  # pane kept
        status = _exec_status(session, pane)
        assert status.code == 42
        assert status.inferred is False

    @pytest.mark.xfail(reason="tmux often never records a SIGINT'd exec pane's status "
                              "(docs/KNOWN_ISSUES.md)", strict=False)
    def test_signal_distinguished_from_code(self, session):
        pane = session.new_pane("x", "sh -c 'exec sleep 300'", mode=PANE_MODE_EXEC)
        assert session.is_running(pane)
        assert session.stop_pane(pane, sigint_timeout=5.0)
        status = session.exit_status(pane)
        assert (status.signal, status.code, status.inferred) == (2, None, False)

    def test_command_before_and_respawn(self, session):
        """The status is the command's, not the setup shell's."""
        session.send_command_before("true")
        pane = session.new_pane("x", "sh -c 'exit 5'", mode=PANE_MODE_EXEC)
        session.wait_for_pane_idle(pane, timeout=5.0)
        assert _exec_status(session, pane).code == 5

        session.respawn_pane(pane, "sh -c 'exit 22'")
        session.wait_for_pane_idle(pane, timeout=5.0)
        assert _exec_status(session, pane).code == 22


class TestStopping:
    def test_stop_pane(self, session):
        sleeper = session.new_pane("sleeper", "sleep 100")
        idle = session.new_pane("idle", "true")

        assert session.wait_for_pane_idle(sleeper, timeout=0.5, poll_interval=0.1) is False
        assert session.stop_pane(sleeper, sigint_timeout=5.0) is True
        assert session.stop_pane(idle, sigint_timeout=5.0) is True

    def test_stop_pane_escalates_past_sigint(self, session):
        pane = session.new_pane("trapper", "trap '' INT; sleep 100")
        time.sleep(0.5)
        assert session.stop_pane(pane, sigint_timeout=1.0) is True
        assert session.pane_status(pane).stopped == "SIGTERM"

    def test_stop_all_in_parallel(self, session):
        """One pane ignores SIGINT and needs SIGTERM; in parallel, the whole
        thing takes about one escalation."""
        quick = [session.new_pane(f"q{i}", "sleep 60", mode=PANE_MODE_WRAP) for i in range(3)]
        stubborn = session.new_pane("stubborn", "sh -c 'trap \"\" INT; exec sleep 60'",
                                    mode=PANE_MODE_WRAP)
        done = session.new_pane("done", "true", mode=PANE_MODE_WRAP)
        session.wait_for_exit(done, timeout=5.0)

        start = time.time()
        results = session.stop_all(sigint_timeout=1.0)

        assert time.time() - start < 3.0
        assert results == {"main:q0": True, "main:q1": True, "main:q2": True,
                           "main:stubborn": True}  # 'done' was not running
        assert {session.pane_status(p).stopped for p in quick} == {"SIGINT"}
        assert all(session.exit_status(p).signal == 2 for p in quick)
        assert session.exit_status(stubborn).signal == 15
        assert session.pane_status(stubborn).state == "stopped"
        assert session.pane_status(done).state == "ok"

    def test_stopped_vs_ended_on_its_own(self, session):
        stopped = session.new_pane("s", "sleep 60", mode=PANE_MODE_WRAP)
        crashed = session.new_pane("c", "sh -c 'kill -INT $$'", mode=PANE_MODE_WRAP)
        session.wait_for_exit(crashed, timeout=5.0)
        session.stop_pane(stopped, sigint_timeout=5.0)

        # Same exit status; only one was asked to stop.
        assert session.exit_status(stopped) == session.exit_status(crashed)
        assert session.pane_status(stopped).state == "stopped"
        assert session.pane_status(crashed).state == "failed"
        assert str(session.pane_status(stopped)) == "stopped (SIGINT): killed by signal 2"

    def test_rerun_clears_stopped(self, session):
        pane = session.new_pane("x", "sleep 60", mode=PANE_MODE_WRAP)
        session.stop_pane(pane, sigint_timeout=5.0)
        session.respawn_pane(pane, "sh -c 'sleep 0.3; exit 1'")

        assert session.wait_for_exit(pane, timeout=5.0).code == 1
        assert session.pane_status(pane).stopped is None
        assert session.pane_status(pane).state == "failed"


def test_status_file_and_title(make_session, tmp_path):
    path = str(tmp_path / "status.json")
    session = make_session(status_file=path)
    pane = session.new_pane("x", "sleep 60", mode=PANE_MODE_WRAP)
    assert [(e["name"], e["running"]) for e in dash.read_status(path)] == [("main:x", True)]

    session.stop_pane(pane, sigint_timeout=5.0)

    assert wait_until(lambda: title(pane) == "x [stopped]")
    # The title and the file are updated one after the other: wait for both.
    assert wait_until(lambda: dash.read_status(path)[0]["stopped"] == "SIGINT")


def test_status_after_session_closed(session, caplog):
    """Closed under us (lte-close, tmux kill-session): exit events don't
    raise, and status() reports the last known state."""
    events = []
    done = session.new_pane("done", "sh -c 'exit 3'", mode=PANE_MODE_WRAP)
    session.wait_for_exit(done, timeout=5)
    session.new_pane("busy", "sleep 60", mode=PANE_MODE_WRAP, on_exit=events.append)

    session.server.cmd("kill-session", "-t", session.id)

    assert wait_until(lambda: events)
    by_name = {s.name: s for s in session.status()}
    assert by_name["main:done"].exit_status.code == 3
    assert not by_name["main:busy"].running
    assert "raised" not in caplog.text
