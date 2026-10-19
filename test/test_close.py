"""lte-close and the library's close API: close actions, the graceful stop
(script first, then every other pane), and the close key."""
import subprocess
import threading

import pytest

from helpers import wait_until as _wait_until
from libtmux_easy import PANE_MODE_WRAP
from libtmux_easy import close
from libtmux_easy._tmux import tmux


def _alive(name):
    return subprocess.run(["tmux", "has-session", "-t", name], capture_output=True).returncode == 0


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(close, "PAUSE", 0)


@pytest.fixture
def plain():
    """A session made with plain tmux, no library: an exec pane that stops on
    SIGINT, and a shell running a job that ignores it."""
    name = "lte-test-close"
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 60"], check=True)
    subprocess.run(["tmux", "new-window", "-t", f"{name}:", "bash --norc -c \"trap '' INT; sleep 60\""],
                   check=True)
    subprocess.run(["tmux", "new-window", "-t", f"{name}:", "bash --norc"], check=True)  # idle shell
    yield tmux("display-message", "-p", "-t", name, "#{session_id}")[0].strip()
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


def test_stops_every_pane_then_kills(plain):
    lines = []
    _wait_until(lambda: len(close.Closer(plain)._panes()) == 3)

    close.Closer(plain, timeouts=(1.0, 1.0, 1.0), out=lines.append)()

    text = "\n".join(lines)
    assert "Stopping 2 pane(s)" in text  # the idle shell has nothing to stop
    assert "(sleep): stopped by SIGINT" in text
    assert "(sleep): stopped by SIGTERM" in text
    assert not _alive("lte-test-close")


def test_actions_run_before_and_after(plain, tmp_path):
    log = tmp_path / "log"
    close.add_action(plain, f"echo after >> {log}")
    close.add_action(plain, f"echo before >> {log}; pgrep -f 'sleep 60' >/dev/null && echo up >> {log}",
                     when="before")
    assert [a["when"] for a in close.actions(plain)] == ["after", "before"]

    close.Closer(plain, timeouts=(1.0, 1.0, 1.0), out=lambda _: None)()

    assert log.read_text().split() == ["before", "up", "after"]


def test_cli_add_list_clear(plain, capsys):
    assert close.main(["-t", plain, "add", "--before", "--", "echo", "a b"]) == 0
    assert close.main(["-t", plain, "add", "./cleanup.sh"]) == 0
    capsys.readouterr()
    close.main(["-t", plain, "list"])
    listed = capsys.readouterr().out
    assert "before  echo 'a b'" in listed and "after   ./cleanup.sh" in listed
    close.main(["-t", plain, "clear"])
    assert close.actions(plain) == []


def test_window_runs_it(plain):
    """lte-close (no --here): a 'close' window does the work, then the session goes."""
    window = close.open_close_window(plain, timeouts=(1.0, 1.0, 1.0))
    assert window
    assert tmux("display-message", "-p", "-t", window, "#{window_name}")[0] == "close"
    assert _wait_until(lambda: not _alive("lte-test-close"), timeout=15.0)


def test_script_stops_its_own_panes(session, tmp_path):
    """The script answers 'close': its callbacks run, it stops its panes the
    wrap-mode way, and lte-close runs the shell actions and kills the session."""
    calls = []
    pane = session.new_pane("server", "sleep 60", mode=PANE_MODE_WRAP)
    session.on_close(lambda s: calls.append(("before", s.is_running(pane))), when="before")
    session.on_close(lambda s: calls.append(("after", s.is_running(pane))))
    session.on_close(f"echo shell >> {tmp_path / 'log'}")
    lines = []

    closer = threading.Thread(target=close.Closer(session.id, out=lines.append))
    closer.start()
    closer.join(30)

    assert calls == [("before", True), ("after", False)]
    assert (tmp_path / "log").read_text() == "shell\n"
    assert "The session's script is stopping its panes:" in lines
    assert "  main:server: stopped" in lines
    assert not _alive("lte-test")


def test_script_gives_the_key_back_when_closed(session):
    tmux("new-session", "-d", "-s", "lte-test-keepalive")  # the server outlives lte-test
    try:
        assert "libtmux_easy.close" in "\n".join(tmux("list-keys", "-T", "prefix", "Q"))
        close.Closer(session.id, out=lambda _: None)()
        assert "libtmux_easy.close" not in "\n".join(tmux("list-keys", "-T", "prefix", "Q"))
    finally:
        tmux("kill-session", "-t", "lte-test-keepalive")


def test_close_in_process(session, tmp_path):
    calls = []
    pane = session.new_pane("server", "sleep 60", mode=PANE_MODE_WRAP)
    session.on_close(lambda s: calls.append(s.is_running(pane)), when="before")
    session.on_close(lambda s: calls.append(s.exit_status(pane).signal))
    session.on_close(f"echo shell >> {tmp_path / 'log'}")

    session.close()

    assert calls == [True, 2]
    assert (tmp_path / "log").read_text() == "shell\n"
    assert not _alive("lte-test")


def test_on_close_rejects_bad_when(session):
    with pytest.raises(ValueError):
        session.on_close("true", when="during")


def test_bind_close_key(session):
    def binding():
        return "\n".join(tmux("list-keys", "-T", "prefix", "C-x"))

    tmux("bind-key", "-T", "prefix", "C-x", "kill-session")  # e.g. from ~/.tmux.conf
    try:
        assert session.bind_close_key("C-x") == "C-x"  # asked for: wraps theirs
        assert "libtmux_easy.close" in binding() and "kill-session" in binding()

        session.kill_session()
        assert "libtmux_easy.close" not in binding() and "kill-session" in binding()
    finally:
        tmux("unbind-key", "-T", "prefix", "C-x")


def _q():
    return "\n".join(tmux("list-keys", "-T", "prefix", "Q"))


def test_create_borrows_q_if_free(make_session):
    sess = make_session("lte-test-q")
    assert "libtmux_easy.close" in _q()
    sess.kill_session()
    assert _q() == ""


def test_user_binding_of_q_wins(make_session):
    tmux("new-session", "-d", "-s", "lte-test-keepalive")  # a server to bind on
    tmux("bind-key", "-T", "prefix", "Q", "display-message", "mine")
    try:
        sess = make_session("lte-test-q")
        assert "libtmux_easy.close" not in _q()
        assert sess.bind_close_key() is None
    finally:
        tmux("unbind-key", "-T", "prefix", "Q")
        tmux("kill-session", "-t", "lte-test-keepalive")


@pytest.mark.parametrize("chosen", ["", "C-q"])
def test_tmux_conf_picks_the_close_key(make_session, chosen):
    tmux("new-session", "-d", "-s", "lte-test-keepalive")
    tmux("set-option", "-g", "@lte_key_close", chosen)
    try:
        make_session("lte-test-q")
        assert _q() == ""
        if chosen:
            assert "libtmux_easy.close" in "\n".join(tmux("list-keys", "-T", "prefix", chosen))
    finally:
        tmux("set-option", "-gu", "@lte_key_close")
        tmux("kill-session", "-t", "lte-test-keepalive")
