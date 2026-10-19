"""Output capture and watches: capture(), search_output(), watch_output(),
wait_for_output(), expect(), new_pane(on_output=, capture=), lte-run --tee."""
import os
import threading
import time

import pytest

from helpers import fmt, wait_until
from libtmux_easy import PANE_MODE_EXEC, PANE_MODE_SHELL, PANE_MODE_WRAP, PaneExited
from libtmux_easy import output
from libtmux_easy._tmux import tmux
from libtmux_easy.tmux_lib import _wrap_prefix

MODES = (PANE_MODE_SHELL, PANE_MODE_EXEC, PANE_MODE_WRAP)


def collect():
    lines = []
    return lines, lambda m: lines.append(m.line)


def piped(session, pane):
    return fmt(session, pane.id, "#{pane_pipe}") == "1"


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return b""


def test_plain_lines():
    data = b"\x1b[31mERROR\x1b[0m: boom\r\nwork 10%\rwork 99%\r\n\npartial"
    assert list(output.plain_lines(data)) == [(0, "ERROR: boom"), (22, "work 99%"),
                                              (42, "partial")]


# ----- watches -----

@pytest.mark.parametrize("mode", MODES)
def test_on_output_each_mode(session, mode):
    """From the first byte, and the typed command (which says READY too)
    isn't output."""
    lines, cb = collect()
    session.new_pane("p", "echo READY $((1+1)); sleep 30", mode=mode, on_output={"READY": cb})
    assert wait_until(lambda: lines)
    time.sleep(0.3)
    assert lines == ["READY 2"]


def test_plain_text(session):
    lines, cb = collect()
    session.new_pane("p", r"printf '\033[31mERROR\033[0m: boom\r\nwork 10%%\rwork 99%%\rERROR done\n'; sleep 30",
                     mode=PANE_MODE_EXEC, on_output={"ERROR": cb})
    assert wait_until(lambda: len(lines) == 2)
    assert lines == ["ERROR: boom", "ERROR done"]


def test_partial_line(session):
    """A prompt with no newline matches once it has been quiet, and only once."""
    lines, cb = collect()
    session.new_pane("p", "printf 'Password: '; sleep 1; echo; sleep 30",
                     mode=PANE_MODE_EXEC, on_output={"Password": cb})
    assert wait_until(lambda: lines)
    time.sleep(1.5)
    assert lines == ["Password: "]


def test_every_match_and_once(session):
    pane = session.new_pane("p", "sleep 0.5; seq 1 5; sleep 30", mode=PANE_MODE_EXEC)
    every = session.watch_output(pane, r"^\d$")
    first = session.watch_output(pane, r"^\d$", once=True)
    assert [every.wait(5).line for _ in range(5)] == ["1", "2", "3", "4", "5"]
    assert first.done and [m.line for m in first.matches] == ["1"]


def test_wait_for_output_sees_earlier_output(session):
    """Not captured before the call: the scrollback counts."""
    pane = session.new_pane("p", "echo listening on 8080; sleep 30", mode=PANE_MODE_WRAP)
    time.sleep(0.5)  # printed before anyone waits
    m = session.wait_for_output(pane, r"listening on (\d+)", timeout=5)
    assert m.match.group(1) == "8080" and m.name == "main:p"


def test_wait_for_output_sees_earlier_capture(session, tmp_path):
    pane = session.new_pane("p", "echo listening on 8080; sleep 30", mode=PANE_MODE_EXEC,
                            capture=str(tmp_path / "p.log"))
    time.sleep(0.5)
    m = session.wait_for_output(pane, r"listening on (\d+)", timeout=5)
    assert m.match.group(1) == "8080" and m.offset == 0


def test_wait_for_output_live_and_timeout(session):
    pane = session.new_pane("p", "sleep 0.5; echo ready", mode=PANE_MODE_SHELL)
    t0 = time.monotonic()
    assert session.wait_for_output(pane, "ready", timeout=5, regex=False).line == "ready"
    assert time.monotonic() - t0 < 2
    with pytest.raises(TimeoutError):
        session.wait_for_output(pane, "never", timeout=0.5)


@pytest.mark.parametrize("mode", (PANE_MODE_EXEC, PANE_MODE_WRAP))
def test_wait_for_output_pane_exited(session, mode):
    pane = session.new_pane("p", "sh -c 'echo nope; exit 3'", mode=mode)
    t0 = time.monotonic()
    with pytest.raises(PaneExited) as info:
        session.wait_for_output(pane, "never", timeout=10)
    assert info.value.status.exit_status.code == 3
    assert time.monotonic() - t0 < 3


def test_wait_for_output_next_typed_run(session):
    """The last run ended, and send_keys() typed another that is slow to
    start (the shell is busy): wait for it, don't call the pane exited."""
    pane = session.new_pane("p", "sh -c 'echo one'", mode=PANE_MODE_WRAP)
    session.wait_for_exit(pane, timeout=5)
    session.send_keys("sleep 1.5", target_pane=pane)
    session.send_keys(f"{_wrap_prefix()} sh -c 'echo two'", target_pane=pane)
    assert session.wait_for_output(pane, r"^two$", timeout=10, since="now").line == "two"


def test_last_words_wrap(session):
    """A line printed just before the command exits: the pane's shell outlives
    it, so tmux keeps it (exec mode can lose it, see output-match.md)."""
    lines, cb = collect()
    session.new_pane("p", "sh -c 'echo boom; exit 3'", mode=PANE_MODE_WRAP, on_output={"boom": cb})
    assert wait_until(lambda: lines == ["boom"])


def test_respawn_keeps_watch(session):
    lines, cb = collect()
    pane = session.new_pane("p", "echo hello; sleep 0.5", mode=PANE_MODE_EXEC,
                            on_output={"hello": cb})
    assert wait_until(lambda: lines == ["hello"])
    session.respawn_pane(pane)
    assert wait_until(lambda: lines == ["hello", "hello"])


def test_cancel_and_unwatch(session):
    lines, cb = collect()
    pane = session.new_pane("p", "bash --norc", mode=PANE_MODE_EXEC)
    w = session.watch_output(pane, "x", cb)
    session.watch_output(pane, "y", cb)
    w.cancel()
    session.send_keys("echo x; echo y", target_pane=pane)
    assert wait_until(lambda: "y" in lines)
    assert "x" not in lines
    session.unwatch_output(pane)
    session.send_keys("echo y", target_pane=pane)
    time.sleep(0.5)
    assert lines == ["y"]


def test_flood_elsewhere(session):
    """A pane printing as fast as it can, captured but unwatched, doesn't hold
    up a match in another. Unwatched, its file is never read."""
    flood = session.new_pane("flood", "yes", mode=PANE_MODE_EXEC, capture=True)
    pane = session.new_pane("p", "sleep 1; echo needle; sleep 30", mode=PANE_MODE_EXEC)
    t0 = time.monotonic()
    session.wait_for_output(pane, "needle", timeout=10)
    assert time.monotonic() - t0 < 3
    offset = session._reader._sources[flood.id].offset
    time.sleep(0.5)
    assert session._reader._sources[flood.id].offset == offset
    assert os.path.getsize(session.output_path(flood)) > offset


def test_callback_can_watch(session):
    """Callbacks run under the reader's lock; it's reentrant."""
    got = threading.Event()
    pane = session.new_pane("p", "sleep 0.5; echo one; sleep 0.5; echo two; sleep 30",
                            mode=PANE_MODE_EXEC)
    session.watch_output(pane, "one", lambda m: session.watch_output(pane, "two", lambda _: got.set()),
                         once=True)
    assert got.wait(5)


def test_expect(session):
    pane = session.new_pane("p", "bash --norc", mode=PANE_MODE_EXEC)
    with session.expect(pane, r"pong (\d+)", timeout=5) as e:
        session.send_keys("echo pong $((40+2))", target_pane=pane)
    assert e.match.match.group(1) == "42"
    with pytest.raises(TimeoutError):
        with session.expect(pane, "never", timeout=0.5):
            pass


def test_watch_window_and_session(session):
    """A window or session watch covers panes made later, and only its scope."""
    w_main = session.watch_output("main", "hit")
    w_all = session.watch_output(None, "hit")
    session.new_pane("a", "sleep 0.3; echo hit a; sleep 30", mode=PANE_MODE_EXEC)
    session.new_window("other")
    session.new_pane("b", "sleep 0.3; echo hit b; sleep 30", mode=PANE_MODE_EXEC)
    assert wait_until(lambda: len(w_all.matches) == 2)
    assert sorted(m.name for m in w_all.matches) == ["main:a", "other:b"]
    assert [m.name for m in w_main.matches] == ["main:a"]


# ----- capture -----

def test_capture_pane_from_first_byte(session, tmp_path):
    """capture=PATH, and capture=True (the default dir)."""
    path = tmp_path / "web.log"
    pane = session.new_pane("web", r"printf '\033[32mfirst\033[0m\n'; sleep 30",
                            mode=PANE_MODE_EXEC, capture=str(path))
    assert wait_until(lambda: path.exists() and b"first" in read(path))
    assert b"\x1b[32m" in read(path)  # raw
    assert session.output_path(pane) == str(path)
    assert [m.line for m in session.search_output(pane, "first")] == ["first"]

    default = session.new_pane("api", "echo hi; sleep 30", mode=PANE_MODE_EXEC, capture=True)
    default_path = session.output_path(default)
    assert os.path.basename(default_path) == "main.api.log"
    assert wait_until(lambda: b"hi" in read(default_path))

    session.kill()
    assert path.exists() and os.path.exists(default_path)  # chosen to pipe: kept


def test_capture_exec_skips_the_starting_shell(make_session, tmp_path):
    """The window's first pane, reused: what its shell prints while it
    starts (a prompt, here noise) isn't captured as the exec command's."""
    noisy = "sh -c 'yes NOISE & sleep 3; kill $!; exec sh'"  # prints nonstop
    tmux("set-option", "-g", "default-command", noisy)  # create() makes the pane
    try:
        session = make_session()
    finally:
        tmux("set-option", "-gu", "default-command")
    path = tmp_path / "out.log"
    pane = session.new_pane("p", "echo first; sleep 30", mode=PANE_MODE_EXEC, capture=str(path))
    assert wait_until(lambda: b"first" in read(path))
    assert [m.line for m in session.search_output(pane, ".")] == ["first"]


def test_watch_only_files_deleted(make_session):
    s = make_session()
    pane = s.new_pane("p", "echo x; sleep 30", mode=PANE_MODE_EXEC, on_output={"x": None})
    path = s.output_path(pane)
    assert wait_until(lambda: os.path.exists(path) and b"x" in read(path))
    s.kill()
    assert not os.path.exists(path)

    s = make_session("lte-test-2")
    s.keep_output = True
    pane = s.new_pane("p", "echo x; sleep 30", mode=PANE_MODE_EXEC, on_output={"x": None})
    path = s.output_path(pane)
    s.kill()
    assert os.path.exists(path)


def test_session_capture(make_session, tmp_path):
    """Library panes get readable names; panes made by hand are piped too,
    from their first line."""
    s = make_session(capture=str(tmp_path))
    s.new_pane("web", "echo hello; sleep 30", mode=PANE_MODE_EXEC)
    hand = s.server.cmd("split-window", "-t", s.current_window.id, "-P", "-F", "#{pane_id}",
                        "echo by hand; sleep 30").stdout[0]
    win = s.server.cmd("new-window", "-t", f"{s.id}:", "-n", "w x", "-P", "-F", "#{pane_id}",
                       "echo new window; sleep 30").stdout[0]
    assert wait_until(lambda: b"hello" in read(tmp_path / "main.web.log"))
    assert wait_until(lambda: b"by hand" in read(tmp_path / f"main.{hand}.log"))
    assert wait_until(lambda: b"new window" in read(tmp_path / f"w_x.{win}.log"))
    assert [m.line for m in s.search_output(None, "hello|by hand|new window")] != []


def test_capture_high_pane_ids(session, tmp_path):
    """An unnamed pane's file is 'window.%N.log': tmux would read '%4.' as
    strftime in pipe-pane's command unless escaped."""
    for i in range(5):
        session.new_pane(f"p{i}", "sleep 30", mode=PANE_MODE_EXEC)
    hand = session.server.cmd("split-window", "-d", "-t", session.current_window.id, "-P",
                              "-F", "#{pane_id}", "sleep 30").stdout[0]
    session.capture("main", str(tmp_path))
    path = tmp_path / f"main.{hand}.log"
    assert int(hand[1:]) >= 4 and session.output_path(hand) == str(path)
    assert wait_until(path.exists)
    assert sorted(os.listdir(tmp_path)) == sorted(
        [f"main.p{i}.log" for i in range(5)] + [path.name])


def test_window_capture_and_opt_out(session, tmp_path):
    session.new_window("logs", capture=str(tmp_path))
    a = session.new_pane("a", "echo a; sleep 30", mode=PANE_MODE_EXEC)
    b = session.new_pane("b", "echo b; sleep 30", mode=PANE_MODE_EXEC, capture=False)
    session.new_window("quiet")
    c = session.new_pane("c", "echo c; sleep 30", mode=PANE_MODE_EXEC)
    assert wait_until(lambda: (tmp_path / "logs.a.log").exists())
    assert piped(session, a) and not piped(session, b) and not piped(session, c)
    assert session.output_path(c) is None  # never captured


def test_capture_later_mark_search_stop(session, tmp_path):
    pane = session.new_pane("p", "bash --norc", mode=PANE_MODE_EXEC)
    session.send_keys("echo before", target_pane=pane)
    cap = session.capture(pane, str(tmp_path / "p.log"))
    session.send_keys("echo one", target_pane=pane)
    assert wait_until(lambda: cap.search("^one"))
    mark = cap.mark()
    session.send_keys("echo two", target_pane=pane)
    assert wait_until(lambda: cap.search("^two", since=mark))
    assert not cap.search("^one", since=mark)
    assert not cap.search("^before")  # from now on
    assert "two" in cap.text(since=mark)
    cap.stop()
    assert not piped(session, pane)


def test_watch_now_ignores_earlier_capture(session, tmp_path):
    """A pane captured before a 'now' watch (the reader already running):
    what it printed before the watch isn't a match."""
    session.new_pane("other", "sleep 30", mode=PANE_MODE_EXEC, on_output={"x": None})
    pane = session.new_pane("p", "bash --norc", mode=PANE_MODE_EXEC)
    session.capture(pane, str(tmp_path / "p.log"))
    session.send_keys("echo pong 1", target_pane=pane)
    assert wait_until(lambda: b"pong 1" in read(tmp_path / "p.log"))
    with session.expect(pane, r"pong \d", timeout=5) as e:
        session.send_keys("echo pong 2", target_pane=pane)
    assert e.match.line == "pong 2"


# ----- lte-run --tee -----

def test_tee_starts_pipe_then_watch(session, tmp_path):
    """No capture: lte-run --tee pipes the pane; a later watch reads its file."""
    tee = tmp_path / "run.log"
    pane = session.new_pane("p", "echo started; sleep 1; echo later; sleep 30",
                            mode=PANE_MODE_WRAP, tee=str(tee))
    assert wait_until(lambda: tee.exists() and b"started" in read(tee))
    assert session.output_path(pane) == str(tee)
    assert session.wait_for_output(pane, "started", timeout=5).line == "started"
    assert session.wait_for_output(pane, "later", timeout=5, since="now").line == "later"


def test_tee_overwrite_and_append(session, tmp_path):
    """A rerun overwrites the file (the watch follows), -a appends."""
    tee = tmp_path / "run.log"
    lines, cb = collect()
    pane = session.new_pane("p", "echo run1", mode=PANE_MODE_WRAP, tee=str(tee))
    session.watch_output(pane, "^run", cb, since="start")
    assert wait_until(lambda: lines == ["run1"])
    session.respawn_pane(pane, "echo run2")
    assert wait_until(lambda: lines == ["run1", "run2"])
    assert b"run1" not in read(tee)
    session.send_keys(f"lte-run --tee {tee} -a -- echo run3", target_pane=pane)
    assert wait_until(lambda: lines == ["run1", "run2", "run3"])
    assert b"run2" in read(tee) and b"run3" in read(tee)


def test_tee_takes_over_capture(make_session, tmp_path):
    """Under a session capture, the run's output goes to --tee's file; the
    pipe closes when the command ends."""
    s = make_session(capture=str(tmp_path / "cap"))
    tee = tmp_path / "run.log"
    pane = s.new_pane("p", "echo hi; sleep 0.5", mode=PANE_MODE_WRAP, tee=str(tee))
    assert wait_until(lambda: tee.exists() and b"hi" in read(tee))
    assert s.output_path(pane) == str(tee)
    assert wait_until(lambda: not piped(s, pane))


def test_tee_never_loses_pipe_to_capture(session, tmp_path):
    """The library pipes and unpipes a pane over and over while a typed
    lte-run --tee starts in it: the check and the pipe are one tmux call on
    both sides, so the run keeps its pipe and its file gets every line."""
    pane = session.new_pane("p", "bash --norc", mode=PANE_MODE_SHELL)
    expected = [f"line{n}" for n in range(1, 6)]
    for i in range(6):
        tee = tmp_path / f"run{i}.log"
        stop = threading.Event()

        def hammer():
            while not stop.is_set():
                session._unpipe(pane)
                session._pipe(pane, str(tmp_path / "lib.log"), quiet=True)

        thread = threading.Thread(target=hammer)
        thread.start()
        try:
            session.send_keys(f"{_wrap_prefix()} --tee {tee} -- sh -c "
                              "'for n in 1 2 3 4 5; do echo line$n; sleep 0.05; done'",
                              target_pane=pane)
            assert wait_until(lambda: tee.exists() and read(tee).split() == [
                line.encode() for line in expected], timeout=5), f"run {i} lost output"
        finally:
            stop.set()
            thread.join()


def test_reader_idle(session):
    """Nothing written: the reader blocks until its safety-net refresh (no
    polling); no watches: it blocks outright."""
    pane = session.new_pane("p", "echo hi; sleep 30", mode=PANE_MODE_EXEC, on_output={"hi": None})
    reader = session._reader
    assert reader._inotify is not None
    time.sleep(0.5)
    assert reader._timeout() > output.POLL
    session.unwatch_output(pane)
    assert reader._timeout() is None


def test_reader_polls_without_inotify(session, monkeypatch):
    def unavailable():
        raise OSError("no inotify")
    monkeypatch.setattr(output, "_Inotify", unavailable)
    lines, cb = collect()
    session.new_pane("p", "sleep 0.3; echo hit; sleep 30", mode=PANE_MODE_EXEC,
                     on_output={"hit": cb})
    assert session._reader._inotify is None
    assert wait_until(lambda: lines == ["hit"])


def test_kill_closes_reader(session):
    pane = session.new_pane("p", "sleep 30", mode=PANE_MODE_EXEC)
    w = session.watch_output(pane, "x")
    reader = session._reader
    session.kill_session()
    assert wait_until(lambda: not reader.alive) and w.done
