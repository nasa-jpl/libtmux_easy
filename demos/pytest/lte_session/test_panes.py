"""Tests that start their own panes, run under lte-session:

    lte-session -- pytest -s demos/pytest/lte_session

Each test's panes appear in the dashboard while it runs, and stay after it
as '<test>/<window>', showing how each one ended. -s: input() and pdb work
in the home pane. Plain pytest works too: each test gets a detached session.
"""
import sys

PY = sys.executable
PORT = 8769


def test_server_answers(tmux_session):
    s = tmux_session  # the lte-session's session, adopted (TmuxSession.current())
    s.new_window("web")
    server = s.new_pane("server", f"{PY} -u -m http.server {PORT} --bind 127.0.0.1", mode="wrap")
    s.wait_for_output(server, r"Serving HTTP", timeout=10)

    client = s.new_pane("client", f"curl -s -o /dev/null -w 'status %{{http_code}}\\n' "
                                  f"127.0.0.1:{PORT}/", mode="wrap")
    assert s.wait_for_exit(client, timeout=10).code == 0
    assert "status 200" in s.output_text(client)

    s.stop_pane(server)  # Ctrl-C: http.server exits cleanly
    assert s.pane_status(server).stopped == "SIGINT"
    assert s.exit_status(server).code == 0


def test_worker_exit_codes(tmux_session):
    s = tmux_session
    s.new_window("workers")
    ok = s.new_pane("ok", "echo working; sleep 1; echo done", mode="wrap")
    bad = s.new_pane("bad", "echo working; sleep 1; echo 'error: disk full' >&2; exit 3",
                     mode="wrap")

    assert s.wait_for_exit(ok, timeout=10).ok
    assert s.wait_for_exit(bad, timeout=10).code == 3
    (hit,) = s.search_output(bad, r"error: (.*)")
    assert hit.match.group(1) == "disk full"


def test_stop_all(tmux_session):
    s = tmux_session
    s.new_window("daemons")
    polite = s.new_pane("polite", "sleep 60", mode="wrap")
    stubborn = s.new_pane("stubborn", "trap '' INT; sleep 60", mode="wrap")

    s.stop_all(sigint_timeout=1)  # SIGINT, then SIGTERM for what ignores it
    assert s.exit_status(polite).signal == 2
    assert s.pane_status(stubborn).stopped == "SIGTERM"
