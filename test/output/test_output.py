"""Output capture and watches: capture(), search_output(), watch_output(),
wait_for_output(), expect(), new_pane(on_output=, pane_capture=, cmd_capture=)."""
import contextlib
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


def test_wait_for_output_line_before_the_reader(session, tmp_path, monkeypatch):
    """A scrollback, then a line printed after the pipe starts but before the
    reader gets the watch: it counts. Was missed (Processes' first pane in
    tmux mode, a fast shell); the reader is held back here to hit it."""
    go = tmp_path / "go"
    pane = session.new_pane("p", f"echo early; until [ -e {go} ]; do sleep 0.01; done; "
                                 "echo late; sleep 30", mode=PANE_MODE_WRAP)
    wait_until(lambda: "early" in session.output_text(pane))
    add = output.OutputReader.add

    def held(self, watch, since=None):
        go.touch()
        wait_until(lambda: b"late" in read(session.output_path(pane)))
        add(self, watch, since)

    monkeypatch.setattr(output.OutputReader, "add", held)
    assert session.wait_for_output(pane, r"^late", timeout=3).line == "late"


def test_watch_tee_first_no_scrollback_twice(session, tmp_path):
    """lte-run --tee piped the pane already: its file, not the scrollback
    too (they overlap), so each line comes once."""
    pane = session.new_pane("p", "echo one; sleep 30", mode=PANE_MODE_WRAP,
                            cmd_capture=str(tmp_path / "p.log"))
    wait_until(lambda: b"one" in read(tmp_path / "p.log"))
    lines, cb = collect()
    session.watch_output(pane, r"^one", cb)
    time.sleep(1.0)
    assert lines == ["one"]


def test_wait_for_output_sees_earlier_capture(session, tmp_path):
    pane = session.new_pane("p", "echo listening on 8080; sleep 30", mode=PANE_MODE_EXEC,
                            pane_capture=str(tmp_path / "p.log"))
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
    it, so tmux keeps it (exec mode can lose it, see output.md)."""
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
    flood = session.new_pane("flood", "yes", mode=PANE_MODE_EXEC, pane_capture=True)
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


def test_spare_echo_hides_no_output(session, tmp_path):
    """Typed as if early ('echoes=2') into a shell that is up: it echoes once,
    and a real line ending with the typed text is output."""
    f = tmp_path / "f"
    typed = f"cat {f}"
    f.write_text(f"out: {typed}\n")
    lines, cb = collect()
    pane = session.new_pane("p", "true", mode=PANE_MODE_SHELL, on_output={"": cb})
    session.wait_for_exit(pane, timeout=5)
    session._type(pane, typed, echoes=2)
    assert wait_until(lambda: f"out: {typed}" in lines)
    assert [line for line in lines if typed in line] == [f"out: {typed}"]


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
    """pane_capture=PATH, and pane_capture=True (the default dir)."""
    path = tmp_path / "web.log"
    pane = session.new_pane("web", r"printf '\033[32mfirst\033[0m\n'; sleep 30",
                            mode=PANE_MODE_EXEC, pane_capture=str(path))
    assert wait_until(lambda: path.exists() and b"first" in read(path))
    assert b"\x1b[32m" in read(path)  # raw
    assert session.output_path(pane) == str(path)
    assert [m.line for m in session.search_output(pane, "first")] == ["first"]

    default = session.new_pane("api", "echo hi; sleep 30", mode=PANE_MODE_EXEC, pane_capture=True)
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
    pane = session.new_pane("p", "echo first; sleep 30", mode=PANE_MODE_EXEC, pane_capture=str(path))
    assert wait_until(lambda: b"first" in read(path))
    assert [m.line for m in session.search_output(pane, ".")] == ["first"]


def test_watch_only_files_deleted(make_session):
    s = make_session()
    with contextlib.suppress(FileNotFoundError):  # an earlier run's: it would be kept
        os.remove(os.path.join(s._output_dir(), "main.p.log"))
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
    os.remove(path)


def test_watch_files_in_the_output_dir(make_session):
    """A watch's file goes where captures do; one there already (an earlier
    run's capture, appended to) is kept, a pane captured on purpose too."""
    s = make_session("lte-test-3")
    for name in ("main.new.log", "main.kept.log"):  # an earlier failed run's
        with contextlib.suppress(FileNotFoundError):
            os.remove(os.path.join(s._output_dir(), name))
    old = os.path.join(s._output_dir(), "main.old.log")
    with open(old, "w") as f:
        f.write("earlier run\n")
    a = s.new_pane("old", "echo x; sleep 30", mode=PANE_MODE_EXEC, on_output={"x": None})
    b = s.new_pane("new", "echo x; sleep 30", mode=PANE_MODE_EXEC)
    s.watch_output(b, "x")
    c = s.new_pane("kept", "echo x; sleep 30", mode=PANE_MODE_EXEC)
    s.watch_output(c, "x")
    s.capture(c)  # on purpose now
    paths = [s.output_path(p) for p in (a, b, c)]
    assert {os.path.dirname(p) for p in paths} == {s._output_dir()}
    assert wait_until(lambda: all(os.path.exists(p) for p in paths))
    s.kill()
    assert [os.path.exists(p) for p in paths] == [True, False, True]
    assert read(old).startswith(b"earlier run")
    for p in (paths[0], paths[2]):
        os.remove(p)


def test_session_capture(make_session, tmp_path):
    """Library panes get readable names; panes made by hand are piped too,
    from their first line."""
    s = make_session(pane_capture=str(tmp_path))
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
    session.new_window("logs", pane_capture=str(tmp_path))
    a = session.new_pane("a", "echo a; sleep 30", mode=PANE_MODE_EXEC)
    b = session.new_pane("b", "echo b; sleep 30", mode=PANE_MODE_EXEC, pane_capture=False)
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
    assert wait_until(lambda: session.search_output(pane, "^one"))
    mark = session.output_mark(pane)
    session.send_keys("echo two", target_pane=pane)
    assert wait_until(lambda: session.search_output(pane, "^two", since=mark))
    assert not session.search_output(pane, "^one", since=mark)
    assert not session.search_output(pane, "^before")  # from the capture's start
    assert not session.search_output(pane, ".", since="now")
    assert "two" in session.output_text(pane, since=mark)
    assert cap.path() == str(tmp_path / "p.log")
    cap.stop()
    assert not piped(session, pane)


def test_since_start_is_the_default(session, tmp_path):
    """watch_output(), like wait_for_output() and the reads, counts what
    the pane printed already; since="now" doesn't."""
    pane = session.new_pane("p", "echo old; sleep 30", mode=PANE_MODE_EXEC,
                            pane_capture=str(tmp_path / "p.log"))
    assert session.wait_for_output(pane, "old", timeout=5)
    every, now = session.watch_output(pane, "old"), session.watch_output(pane, "old", since="now")
    assert wait_until(lambda: every.count == 1)
    time.sleep(0.3)
    assert now.count == 0
    with pytest.raises(ValueError, match="since"):
        session.watch_output(pane, "old", since="later")


def test_mark_on_uncaptured_pane(session):
    """output_mark() captures a pane not captured yet, so a watch from the
    mark sees all that comes after it, and nothing before."""
    pane = session.new_pane("p", "bash --norc", mode=PANE_MODE_EXEC)
    session.send_keys("echo before", target_pane=pane)
    assert wait_until(lambda: session.search_output(pane, "^before"))  # scrollback
    mark = session.output_mark(pane)
    assert piped(session, pane)
    session.send_keys("echo after", target_pane=pane)
    assert session.wait_for_output(pane, "^after", timeout=5, since=mark).line == "after"
    with pytest.raises(TimeoutError):
        session.wait_for_output(pane, "^before", timeout=0.5, since=mark)


def test_window_mark(session):
    """A window's mark is {pane: offset}; a pane made after it counts from
    its first byte."""
    a = session.new_pane("a", "bash --norc", mode=PANE_MODE_EXEC)
    session.send_keys("echo a1", target_pane=a)
    mark = session.output_mark("main")
    assert set(mark) == {a.id}
    session.send_keys("echo a2", target_pane=a)
    session.new_pane("b", "echo b1; sleep 30", mode=PANE_MODE_EXEC)
    assert wait_until(lambda: sorted(m.line for m in session.search_output(
        "main", "^[ab][12]$", since=mark)) == ["a2", "b1"])


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


# ----- cmd_capture (lte-run --tee) -----

def test_tee_starts_pipe_then_watch(session, tmp_path):
    """No capture: lte-run --tee pipes the pane; a later watch reads its file."""
    tee = tmp_path / "run.log"
    pane = session.new_pane("p", "echo started; sleep 1; echo later; sleep 30",
                            mode=PANE_MODE_WRAP, cmd_capture=str(tee))
    assert wait_until(lambda: tee.exists() and b"started" in read(tee))
    assert session.output_path(pane) == str(tee)
    assert session.wait_for_output(pane, "started", timeout=5).line == "started"
    assert session.wait_for_output(pane, "later", timeout=5, since="now").line == "later"


def test_tee_overwrite_and_append(session, tmp_path):
    """A rerun overwrites the file (the watch follows), -a appends."""
    tee = tmp_path / "run.log"
    lines, cb = collect()
    pane = session.new_pane("p", "echo run1", mode=PANE_MODE_WRAP, cmd_capture=str(tee))
    session.watch_output(pane, "^run", cb, since="start")
    assert wait_until(lambda: lines == ["run1"])
    session.respawn_pane(pane, "echo run2")
    assert wait_until(lambda: lines == ["run1", "run2"])
    assert b"run1" not in read(tee)
    session.send_keys(f"lte-run --tee {tee} -a -- echo run3", target_pane=pane)
    assert wait_until(lambda: lines == ["run1", "run2", "run3"])
    assert b"run2" in read(tee) and b"run3" in read(tee)


def test_send_keys_cmd_capture(session, tmp_path):
    """send_keys(cmd_capture=) runs the command under lte-run --tee: its
    output only, in plain text; a shell pane can't."""
    pane = session.new_pane("p", "true", mode=PANE_MODE_WRAP)
    assert session.wait_for_exit(pane, timeout=5).code == 0
    log = tmp_path / "cmd.log"
    session.send_keys(r"printf '\033[1mbold\033[0m\n'; exit 3", target_pane=pane,
                      cmd_capture=str(log))
    assert wait_until(lambda: session.exit_status(pane).code == 3)
    assert read(log) == b"bold\n"
    shell = session.new_pane("s", "sleep 30", mode=PANE_MODE_SHELL)
    with pytest.raises(ValueError, match="cmd_capture"):
        session.send_keys("true", target_pane=shell, cmd_capture=str(tmp_path / "x.log"))

def test_tee_takes_over_capture(make_session, tmp_path):
    """Under a session capture, the run's output goes to --tee's file; the
    pipe closes when the command ends."""
    s = make_session(pane_capture=str(tmp_path / "cap"))
    tee = tmp_path / "run.log"
    pane = s.new_pane("p", "echo hi; sleep 0.5", mode=PANE_MODE_WRAP, cmd_capture=str(tee))
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
