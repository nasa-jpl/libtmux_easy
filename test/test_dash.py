"""The dashboard: the viewport's pane swapping, actions over the control
socket, the home log, and the sidebar. Only dashboard() and the sidebar
need Textual."""
import asyncio
import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import time

import pytest

from helpers import fmt as _fmt, pane_text, wait_until as _wait_until
from libtmux_easy import PANE_MODE_WRAP, dash
from libtmux_easy._tmux import option, tmux
from libtmux_easy.wrap import SOCKET_NAME

needs_textual = pytest.mark.skipif(importlib.util.find_spec("textual") is None,
                                   reason="needs Textual")


@pytest.fixture
def viewport(session):
    """A dashboard window built by hand: a stand-in sidebar and a home pane,
    with the window options dashboard() would set."""
    run = session._tmux_run
    out = run("new-window", "-d", "-t", f"{session.id}:", "-n", "dash",
              "-P", "-F", "#{window_id} #{pane_id}", "cat")
    window, home = out[0].split()
    sidebar = run("split-window", "-h", "-b", "-d", "-l", "30", "-t", home,
                  "-P", "-F", "#{pane_id}", "cat")[0].strip()
    for name, value in ((dash.HOME_OPTION, home), (dash.VIEW_OPTION, home),
                        (dash.HOME_CMD_OPTION, "cat"), (dash.WIDTH_OPTION, "30")):
        run("set-option", "-wq", "-t", window, name, value)
    return dash.Viewport(window, run=run, sidebar=sidebar)


class TestViewport:
    def test_show_swaps_pane_in_and_home_out(self, session, viewport):
        a = session.new_pane("a", "sleep 60")
        home = viewport.home

        viewport.show(a.id)

        panes = viewport.panes()
        assert panes[a.id] == viewport.window
        assert panes[home] == _fmt(session, f"{session.id}:main", "#{window_id}")
        assert viewport.view == a.id

    def test_browsing_keeps_layouts(self, session, viewport):
        a = session.new_pane("a", "sleep 60")
        b = session.new_pane("b", "sleep 60")
        session.new_window("other")
        c = session.new_pane("c", "sleep 60")
        layouts = {w: _fmt(session, f"{session.id}:{w}", "#{window_layout}") for w in ("main", "other")}

        for pane in (a, b, c, a):
            viewport.show(pane.id)
            assert viewport.view == pane.id
        viewport.restore()

        assert viewport.view == viewport.home
        assert {w: _fmt(session, f"{session.id}:{w}", "#{window_layout}") for w in layouts} == layouts
        assert session.get_pane_name(b) == "main:b"

    def test_library_tracks_a_pane_on_show(self, session, viewport):
        pane = session.new_pane("x", "sleep 60", mode=PANE_MODE_WRAP)
        viewport.show(pane.id)

        assert session.is_running(pane) is True
        assert session.stop_pane(pane, sigint_timeout=5.0) is True
        assert session.exit_status(pane).signal == 2

    def test_repair_after_viewed_pane_exits(self, session, viewport):
        pane = session.new_pane("x", "true")
        session.wait_for_pane_idle(pane, timeout=5.0)
        home = viewport.home
        viewport.show(pane.id)

        pane.send_keys("exit", enter=True)  # the viewport slot goes with it
        assert _wait_until(lambda: pane.id not in viewport.panes())
        viewport.repair()

        assert viewport.view == home
        assert viewport.panes()[home] == viewport.window
        assert _fmt(session, viewport.sidebar, "#{pane_width}") == "30"

    def test_put_back_after_home_killed(self, session, viewport):
        a = session.new_pane("a", "sleep 60")
        b = session.new_pane("b", "sleep 60")
        main = _fmt(session, f"{session.id}:main", "#{window_id}")
        old_home = viewport.home
        viewport.show(a.id)

        session._tmux_run("kill-pane", "-t", old_home)
        viewport.show(b.id)

        panes = viewport.panes()
        assert panes[a.id] == main  # rejoined its window
        assert panes[b.id] == viewport.window
        assert viewport.home not in (old_home, "")
        assert panes[viewport.home] == main  # the new home fills b's slot


    def test_go_to_puts_pane_back_and_selects_it(self, session, viewport):
        a = session.new_pane("a", "sleep 60")
        session.new_pane("b", "sleep 60")
        viewport.show(a.id)

        viewport.go_to(a.id)

        assert viewport.view == viewport.home
        assert _fmt(session, session.id, "#{window_name} #{pane_id}") == f"main {a.id}"

    def test_extra_pane_stays_put(self, session, viewport):
        a = session.new_pane("a", "sleep 60")
        b = session.new_pane("b", "true")
        session.wait_for_pane_idle(b, timeout=5.0)
        session._dash_window, session._dash_sidebar = viewport.window, viewport.sidebar
        extra = session.dashboard_pane("echo extra_$((6*7))", title="extra", size=5)
        assert _wait_until(lambda: "extra_42" in pane_text(extra))

        for pane in (a, b):
            viewport.show(pane.id)
            assert viewport.panes()[extra.id] == viewport.window
        viewport.repair()  # remembers the intact layout

        b.send_keys("exit", enter=True)  # the viewport slot goes with it
        assert _wait_until(lambda: b.id not in viewport.panes())
        viewport.repair()

        assert viewport.panes()[viewport.home] == viewport.window
        assert viewport.panes()[extra.id] == viewport.window
        assert _fmt(session, extra.id, "#{pane_height}") == "5"
        assert _fmt(session, viewport.sidebar, "#{pane_width}") == "30"
        session._dash_window = None

    def test_dashboard_pane_needs_dashboard(self, session):
        with pytest.raises(RuntimeError, match="no dashboard"):
            session.dashboard_pane("true")


class TestControl:
    def test_stop_and_respawn(self, session):
        pane = session.new_pane("x", "sleep 60", mode=PANE_MODE_WRAP)
        control = dash.Control(os.path.join(session._wrap_dir, SOCKET_NAME))
        assert control.online()

        assert control.send("stop", pane.id)
        assert _wait_until(lambda: not session.is_running(pane), timeout=10.0)
        assert session.exit_status(pane).signal == 2

        old = session.pane_status(pane).target_pids
        assert control.send("respawn", pane.id)
        assert _wait_until(lambda: session.pane_status(pane).target_pids not in ((), old))
        assert session.is_running(pane)

    def test_stop_all_command(self, session):
        a = session.new_pane("a", "sleep 60", mode=PANE_MODE_WRAP)
        b = session.new_pane("b", "sleep 60", mode=PANE_MODE_WRAP)
        control = dash.Control(os.path.join(session._wrap_dir, SOCKET_NAME))

        assert control.send("stop_all", None)

        assert _wait_until(lambda: not session.is_running(a) and not session.is_running(b),
                           timeout=10.0)
        assert session.exit_status(a).signal == session.exit_status(b).signal == 2

    def test_shutdown_stops_then_kills(self, make_session):
        sess = make_session("lte-test-shutdown")
        pane = sess.new_pane("x", "sleep 60", mode=PANE_MODE_WRAP)
        statuses = []
        sess.watch_pane(pane, statuses.append)
        control = dash.Control(os.path.join(sess._wrap_dir, SOCKET_NAME))

        assert control.send("shutdown", None)

        assert _wait_until(lambda: not sess.server.has_session("lte-test-shutdown"), timeout=10.0)
        assert statuses and statuses[0].exit_status.signal == 2  # SIGINT, not SIGHUP
        sess.kill_session()  # already killed: a no-op, not an error

    def test_unknown_pane_ignored(self, session):
        control = dash.Control(os.path.join(session._wrap_dir, SOCKET_NAME))
        assert control.send("stop", "%9999")
        assert control.send("rm -rf", "%0")
        time.sleep(0.3)

    def test_offline(self, tmp_path):
        assert dash.Control(str(tmp_path / "nope.sock")).online() is False
        assert dash.Control(None).send("stop", "%0") is False


@needs_textual
def test_sidebar_labels():
    from libtmux_easy.dash.app import describe, describe_long
    exit2 = {"code": None, "signal": 2, "inferred": False}
    assert describe({"running": True}) == ("running", "running")
    assert describe({"exit_status": exit2, "stopped": "SIGINT"}) == ("stopped", "stopped")
    assert describe({"exit_status": exit2, "stopped": "SIGKILL"}) == ("stopped KILL", "stopped")
    assert describe({"exit_status": exit2}) == ("SIGINT", "failed")
    assert describe({"exit_status": {"code": 0}}) == ("exit 0", "ok")
    assert describe({"exit_status": {}}) == ("unknown", "unknown")
    assert describe_long({"exit_status": exit2, "stopped": "SIGTERM"}) == \
        "stopped, needed SIGTERM: killed by SIGINT (2)"


class TestHomeLog:
    def test_tee_copies_stdout_and_logging(self, session, capsys):
        logger = logging.getLogger("test_dash")
        logger.setLevel(logging.INFO)
        session.tee_home()
        print("to stdout")
        logger.info("to logging")
        sys.stdout.flush()

        with open(session.home_log) as f:
            text = f.read()
        assert "to stdout" in text
        assert "INFO test_dash: to logging" in text
        assert "to stdout" in capsys.readouterr().out  # still printed too

    def test_untee_on_kill(self, session):
        before = sys.stdout
        session.tee_home()
        assert sys.stdout is not before
        session.kill()
        assert sys.stdout is before


@needs_textual
class TestDashboard:
    def test_opens_and_closes(self, session):
        a = session.new_pane("a", "sleep 60", mode=PANE_MODE_WRAP)
        session.dashboard(select=False)
        window = session._dash_window
        assert session._tmux_run("show-options", "-qv", "-t", session.id,
                                 dash.WINDOW_OPTION) == [window]
        assert _wait_until(lambda: "main" in pane_text(session.get_pane(session._dash_sidebar)))

        dash.Viewport(window, run=session._tmux_run).show(a.id)
        session.close_dashboard()

        assert window not in [w.id for w in session.windows]
        assert session.get_pane_name(a) == "main:a"
        assert session._tmux_run("show-options", "-qv", "-t", session.id, dash.WINDOW_OPTION) == []
        assert _fmt(session, a.id, "#{window_name}") == "main"


    def test_is_the_first_window(self, make_session):
        """Even opened before the first new_window(), which must still reuse
        the session's own initial window, not the dashboard's."""
        sess = make_session(window=None)
        sess.dashboard(select=False)
        sess.new_window("main")
        sess.new_pane("a", "sleep 60", mode=PANE_MODE_WRAP)
        sess.new_window("later")

        assert [w.name for w in sess.windows] == ["dash", "main", "later"]
        sess.select_first()
        assert _fmt(sess, sess.id, "#{window_name}") == "main"


def test_open_window_from_outside_a_session_object(tmp_path):
    """A program that is not a TmuxSession opens the window by session name
    and feeds it itself. The sidebar command is
    replaced, so this needs no Textual."""
    subprocess.run(["tmux", "kill-session", "-t", "lte-test-ext"], capture_output=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", "lte-test-ext"], check=True)
    try:
        home_log = tmp_path / "home.log"
        window, sidebar = dash.open_window(
            "lte-test-ext", str(tmp_path / "status.json"), str(home_log),
            str(tmp_path / "ctl.sock"), sidebar_cmd="cat")
        assert option("lte-test-ext", dash.WINDOW_OPTION) == window
        assert option("lte-test-ext", dash.SIDEBAR_OPTION) == sidebar
        home = option(window, dash.HOME_OPTION, "-wq")
        assert option(window, dash.VIEW_OPTION, "-wq") == home

        home_log.write_text("hello from outside\n")
        assert _wait_until(lambda: "hello from outside" in "\n".join(
            tmux("capture-pane", "-p", "-t", home)))
    finally:
        subprocess.run(["tmux", "kill-session", "-t", "lte-test-ext"], capture_output=True)


def test_dashboard_without_textual_explains(monkeypatch, session):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None)
    which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None if name == "lte-dash" else which(name, *a, **k))
    with pytest.raises(RuntimeError, match="pipx install"):
        session.dashboard()
    assert session._dash_window is None


class FakeViewport:
    window, sidebar, home, view = "@0", "%0", "%1", "%1"

    def __init__(self, panes=()):
        self._panes = {p: "@1" for p in panes}

    def repair(self): pass
    def show(self, pane_id): pass
    def panes(self): return self._panes
    def option(self, name): return ""


def entry(pane_id, name, running=False, code=None, stopped=None):
    """A status-file entry."""
    return {"pane_id": pane_id, "name": name, "mode": "wrap", "command": "x",
            "target_pids": [], "running": running, "stopped": stopped,
            "exit_status": {"code": code, "signal": None, "inferred": False}}


@needs_textual
def test_sidebar_survives_status_file_switch(tmp_path):
    """lte-session: each test's TmuxSession.current() writes its own status
    file. The sidebar crashed (KeyError) when the second one replaced the
    first, and missed the first one's final write."""
    from libtmux_easy.dash.app import DashApp

    first, second = tmp_path / "a.json", tmp_path / "b.json"
    first.write_text(json.dumps([entry("%5", "nodes:talker", True)]))
    current = {"path": str(first)}

    async def scenario():
        app = DashApp(FakeViewport(), lambda: current["path"], dash.Control(None))
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            # The first file's last write and the switch, within one poll.
            first.write_text(json.dumps([entry("%5", "nodes:talker", False, code=0)]))
            second.write_text(json.dumps([entry("%9", "tools:build", True)]))
            current["path"] = str(second)
            app.poll()
            await pilot.pause(0.1)
            return dict(app.entries), app.return_code

    entries, code = asyncio.run(scenario())
    assert code in (None, 0)
    assert entries["%5"]["exit_status"]["code"] == 0  # the final write was seen
    assert entries["%9"]["running"] is True


@needs_textual
def test_sidebar_summary_counts():
    from libtmux_easy.dash.app import DashApp
    from textual.widgets import Static

    async def scenario():
        app = DashApp(FakeViewport(("%5", "%6", "%7", "%8")), None, dash.Control(None))
        async with app.run_test() as pilot:
            app.entries = {e["pane_id"]: e for e in (
                entry("%5", "a:one", running=True), entry("%6", "a:two", running=True),
                entry("%7", "a:three", code=0, stopped="SIGINT"), entry("%8", "b:four", code=3))}
            app.poll()
            await pilot.pause(0.1)
            return str(app.query_one("#summary", Static).render())

    assert asyncio.run(scenario()) == "2 running  1 stopped  1 failed"
