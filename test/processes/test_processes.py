"""Processes without pytest's plugin: a program's own group of processes."""
import sys
import threading
import time

import pytest

from libtmux_easy.processes import ProcessFailure, Processes

# Ignores the first SIGINT for a while, then exits 0 on the next.
SLOW_STOP = (f"{sys.executable} -c \"import signal, time; print('up', flush=True); "
             "signal.signal(signal.SIGINT, lambda *a: (time.sleep(1.5), exit(0))); "
             "time.sleep(60)\"")


def test_subprocess_group(tmp_path):
    exits = []
    procs = Processes("prog", log_dir=str(tmp_path), stop_codes={"*": (0, 130)},
                      on_exit=lambda name, status, died: exits.append((name, died)))
    a = procs.start("w:a", "echo hello $GREETING; sleep 60", ready="hello", env={"GREETING": "x"})
    assert "hello x" in a.output()
    procs.start("w:oneshot", "true", exits=0)
    procs.wait_for_exit("w:oneshot")
    procs.start("w:dies", "sleep 0.2; exit 3")
    with pytest.raises(ProcessFailure, match=r"w:dies exited 3"):
        procs.wait_until(lambda: False, timeout=5)
    assert ("w:oneshot", False) in exits and ("w:dies", True) in exits
    assert (tmp_path / "w.a.log").read_text().startswith("hello x")
    procs.close()
    assert ("w:a", False) in exits


def test_close_stops_all_at_once(tmp_path):
    """Three that take 1.5 s each to stop: about 1.5 s in all, not 4.5."""
    procs = Processes("par", log_dir=str(tmp_path))
    for n in "abc":
        procs.start(f"w:{n}", SLOW_STOP, ready="up")
    t = time.monotonic()
    procs.close()
    assert time.monotonic() - t < 3.5


def test_kill_all(tmp_path):
    """kill_all() while close() waits: no grace, and no death."""
    procs = Processes("kill", log_dir=str(tmp_path), sigint_timeout=30)
    procs.start("w:a", f"{sys.executable} -c \"import signal, time; print('up', flush=True); "
                       "signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(60)\"",
                ready="up")
    threading.Timer(0.5, procs.kill_all).start()
    t = time.monotonic()
    procs.close(check=False)
    assert time.monotonic() - t < 5
    assert not procs.died and not procs.status("w:a").running


def test_tmux_group(session, tmp_path):
    """Panes in a session of the program's, bare window names."""
    procs = Processes("prog", tmux=session, prefix="", log_dir=str(tmp_path),
                      stop_codes={"*": (0, 130)})
    procs.start("sim:a", "echo ready; sleep 60", ready="ready")
    procs.start("sim:b", "sleep 60")
    assert "sim" in [w.name for w in session.windows]
    assert procs.status("sim:a").running
    procs.close()
    assert not procs.status("sim:b").running


def test_build_needs_tmux(tmp_path):
    procs = Processes("b", log_dir=str(tmp_path))
    with pytest.raises(ValueError, match="needs a tmux session"):
        procs.build(lambda session: None)
    procs.close()


def test_per_process_sigint_timeout(tmp_path):
    """close(): each process escalates on its own deadline, all at once."""
    ignore = (f"{sys.executable} -c \"import signal, time; print('up', flush=True); "
              "signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(60)\"")
    procs = Processes("own", log_dir=str(tmp_path), sigint_timeout=30)
    procs.start("w:quick", ignore, ready="up", sigint_timeout=0.3)
    procs.start("w:slow", SLOW_STOP, ready="up")
    t = time.monotonic()
    procs.close(check=False)
    assert time.monotonic() - t < 3.5  # not 30 s for 'quick'
    assert procs.status("w:quick").stopped == "SIGTERM"
    assert procs.status("w:slow").stopped == "SIGINT"
    # stop() honours it too, and a restart keeps it
    procs = Processes("own2", log_dir=str(tmp_path / "2"), sigint_timeout=30)
    p = procs.start("w:quick", ignore, ready="up", sigint_timeout=0.3)
    p.stop(check=False)
    p.start().wait_for("up")
    t = time.monotonic()
    p.stop(check=False)
    assert time.monotonic() - t < 3 and procs.status("w:quick").stopped == "SIGTERM"
    procs.close()


# Ignores SIGINT and SIGTERM: only SIGKILL ends it.
DEAF = (f"{sys.executable} -c \"import signal, time; print('up', flush=True); "
        "signal.signal(signal.SIGINT, signal.SIG_IGN); "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)\"")


def _sigterm_timeouts(procs):
    """The group's sigterm_timeout and start()'s, each on its own deadlines."""
    procs.start("w:deaf", DEAF, ready="up", sigterm_timeout=0.3)
    procs.start("w:group", DEAF, ready="up")  # the group's: 0.5 s
    t = time.monotonic()
    procs.close(check=False)
    assert time.monotonic() - t < 3  # not 5 s of SIGTERM grace each
    assert procs.status("w:deaf").stopped == "SIGKILL"
    assert procs.status("w:group").stopped == "SIGKILL"


def test_sigterm_timeout(tmp_path):
    """SIGTERM -> SIGKILL grace: 5 s by default, as before; settable."""
    assert Processes("d", log_dir=str(tmp_path / "d")).sigterm_timeout == 5.0
    _sigterm_timeouts(Processes("term", log_dir=str(tmp_path), sigint_timeout=0.2,
                                sigterm_timeout=0.5))
    procs = Processes("term2", log_dir=str(tmp_path / "2"), sigint_timeout=0.2,
                      sigterm_timeout=30)
    p = procs.start("w:deaf", DEAF, ready="up", sigterm_timeout=0.3)
    p.stop(check=False)
    p.start().wait_for("up")  # a restart keeps it
    t = time.monotonic()
    p.stop(check=False)
    assert time.monotonic() - t < 3 and procs.status("w:deaf").stopped == "SIGKILL"
    p.start().wait_for("up")
    t = time.monotonic()
    procs.stop("w:deaf", check=False, sigterm_timeout=0.1)  # stop()'s own
    assert time.monotonic() - t < 3 and procs.status("w:deaf").stopped == "SIGKILL"
    procs.close()


def test_sigterm_timeout_tmux(session, tmp_path):
    _sigterm_timeouts(Processes("term", tmux=session, prefix="", log_dir=str(tmp_path),
                                sigint_timeout=0.2, sigterm_timeout=0.5))


def test_sigterm_timeout_adopted(tmp_path):
    """adopt()'s panes: the group's sigterm_timeout."""
    script = tmp_path / "s.py"
    script.write_text("from libtmux_easy import TmuxSession\n"
                      "s = TmuxSession.create('deaf', mode='wrap')\n"
                      "s.new_window('w')\n"
                      f"s.new_pane('x', {DEAF!r})\n")
    procs = Processes("adopt", log_dir=str(tmp_path / "logs"), sigint_timeout=0.2,
                      sigterm_timeout=0.3)
    procs.adopt(f"{sys.executable} {script}")
    procs.wait_for("w:x", "up", timeout=10)
    t = time.monotonic()
    procs.close(check=False)
    assert time.monotonic() - t < 3
    assert procs.status("w:x").stopped == "SIGKILL"
