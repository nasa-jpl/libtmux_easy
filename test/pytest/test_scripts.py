"""Testing a tmux session script (docs/testing-scripts.md): the three methods
-- layout (start() a script's panes()), build(), adopt() -- in an inner
pytest, with subprocesses and in a detached tmux session."""

import pytest

from inner import MODES

# One system, written so all three methods can test it: panes() is the data,
# build() lays it out, running the file brings it up as a session script does.
SYSTEM = """
import sys
from libtmux_easy import TmuxSession
CMD = f"{sys.executable} proc.py"

def panes():
    return [("sim:a", CMD), ("sim:b", CMD), ("ctl:job", "echo done; exit 4")]

def build(session):
    for window in ("sim", "ctl"):
        session.new_window(window)
        for name, cmd in panes():
            if name.startswith(window + ":"):
                session.new_pane(name.split(":")[1], cmd)
    session.wait_for_output("sim:a", "hello_42", timeout=10)

if __name__ == "__main__":
    build(TmuxSession.create("system", mode="wrap", pane_capture=True))
"""

THREE_WAYS = """
import sys
import system
from libtmux_easy.testing import Processes
EXITS = {"ctl:job": 4}

class Checks:
    # The same tests, whichever method brought the system up. In file order:
    # the last two stop panes.
    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_names(self):
        assert [n for n in self.procs.names if n != "script:script"] == ["sim:a", "sim:b", "ctl:job"]

    def test_output(self):
        self.procs["sim:b"].wait_for("hello_42", timeout=10)
        assert "hello_42" in self.procs.output("sim:a")
        assert "hello_42" in open(self.procs.log("sim:a")).read()

    def test_job_exit(self):
        assert self.procs["ctl:job"].wait_for_exit(10).exit_status.code == 4

    def test_kill_is_seen(self):
        with self.procs.expect_exit("sim:b"):
            self.procs.kill("sim:b")
        assert self.procs.status("sim:b").exit_status.signal == 9

    def test_stop(self):
        assert not self.procs["sim:a"].stop().running

class TestLayout(Checks):
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls)
        for name, cmd in system.panes():
            cls.procs.start(name, cmd, ready=None if name in EXITS else "hello_42",
                            exits=EXITS.get(name))

class TestBuild(Checks):
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls)
        cls.procs.build(system.build, exits=EXITS)

class TestAdopt(Checks):
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls)
        cls.procs.adopt(f"{sys.executable} system.py", exits=EXITS)
"""


@pytest.mark.parametrize("mode", MODES)
def test_one_system_three_ways(run, mode):
    proc, out = run(THREE_WAYS, mode=mode, files={"system.py": SYSTEM})
    for cls in ("TestLayout", "TestBuild", "TestAdopt"):
        for test in (
            "test_names",
            "test_output",
            "test_job_exit",
            "test_kill_is_seen",
            "test_stop",
        ):
            assert f"PASSED test_inner.py::{cls}::{test}" in out, out
    assert proc.returncode == 0, out


# Beyond the shared checks: mixed modes, the script's own watches, restarts,
# a script that stays (ready=), one whose pane dies, plain tmux.
# A session script: build() for Processes.build(); run as it is, for adopt().
SCRIPT = """
import sys
from libtmux_easy import TmuxSession
CMD = f"{sys.executable} proc.py"

def build(session):
    session.new_window("sim")
    session.new_pane("a", CMD)
    session.wait_for_output("sim:a", "hello_42", timeout=10)  # its own gate
    session.new_pane("b", CMD, mode="shell")
    session.new_window("ctl")
    session.new_pane("job", "echo done; exit 4")

def monitor(session, alert):
    session.watch_pane("sim:a", on_exit=lambda status: alert(str(status)))

if __name__ == "__main__":
    session = TmuxSession.create("scripted", mode="wrap")
    build(session)
    print("built", flush=True)
    session.dashboard()   # nothing under LTE_HEADLESS
    session.attach()      # nor this
    if "--stay" in sys.argv:
        import time
        time.sleep(600)
"""

BUILD_TESTS = """
import pytest, script
from libtmux_easy.testing import Processes

class TestBuild:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls)
        cls.procs.build(script.build, exits={"ctl:job": 4})
        cls.alerts = []
        script.monitor(cls.procs.session, cls.alerts.append)

    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_names(self):
        assert self.procs.names == ["sim:a", "sim:b", "ctl:job"]
        self.procs["sim:b"].wait_for("hello_42")
        assert "hello_42" in open(self.procs["sim:a"].log()).read()

    def test_monitor_sees_a_kill(self):
        with self.procs.expect_exit("sim:a"):
            self.procs["sim:a"].kill()
        self.procs.wait_until(lambda: self.alerts, timeout=5)
        assert "signal 9" in self.alerts[0]

    def test_restart(self):
        self.procs["sim:a"].start()
        self.procs["sim:a"].wait_for("hello_42")

    def test_names_unique_in_the_class(self):
        with pytest.raises(ValueError, match="sim:a is taken"):
            Processes().start("sim:a", "sleep 1")
"""


@pytest.mark.parametrize("mode", MODES)
def test_build(run, mode):
    proc, out = run(BUILD_TESTS, mode=mode, files={"script.py": SCRIPT})
    for test in (
        "test_names",
        "test_monitor_sees_a_kill",
        "test_restart",
        "test_names_unique_in_the_class",
    ):
        assert f"PASSED test_inner.py::TestBuild::{test}" in out, out
    assert "ERROR" not in out.split("short test summary")[-1], out


ADOPT_TESTS = """
import sys
from libtmux_easy.testing import Processes

class TestAdopt:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls)
        cls.panes = cls.procs.adopt(f"{sys.executable} script.py", exits={"ctl:job": 4})

    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_names(self):
        assert [p.name for p in self.panes] == ["sim:a", "sim:b", "ctl:job"]
        assert "built" in self.procs.output("script")
        self.procs["sim:b"].wait_for("hello_42")
        assert self.procs["ctl:job"].wait_for_exit(5).exit_status.code == 4

    def test_shell_pane_stops(self):
        assert not self.procs["sim:b"].stop().running

class TestStays:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls, stop_codes={"*": (0, 130)})
        cls.procs.adopt(f"{sys.executable} script.py --stay", ready="built",
                        exits={"ctl:job": 4})

    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_script_runs(self):
        assert self.procs["script"].status().running

class TestDies:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls)
        cls.procs.adopt(f"{sys.executable} script.py")  # ctl:job's exit 4: a death

    def test_job(self):
        import time; time.sleep(1)

class TestPlainTmux:
    # a script with no libtmux_easy: a command pane, a loop; an idle shell is left out
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls, stop_codes={"*": (0, 130)})
        cls.procs.adopt("sh plain.sh")

    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_plain_panes(self):
        assert self.procs.names == ["script:script", "w:0", "loop:0"]
        assert [self.procs.status(n).mode for n in self.procs.names[1:]] == ["exec", "exec"]
        self.procs["w:0"].wait_for("up_7")
        self.procs["loop:0"].wait_for("tick")
        assert self.procs.status("w:0").running and self.procs.status("loop:0").running
"""

PLAIN = """
tmux new-session -d -s plain -n w 'echo up_7; exec sleep 60'
tmux new-window -t plain -n loop 'while sleep 0.2; do echo tick; done'
tmux new-window -t plain -n sh 'bash --norc'
"""


@pytest.mark.parametrize("mode", MODES)
def test_adopt(run, mode):
    proc, out = run(ADOPT_TESTS, mode=mode, files={"script.py": SCRIPT, "plain.sh": PLAIN})
    assert "PASSED test_inner.py::TestAdopt::test_names" in out, out
    assert "PASSED test_inner.py::TestAdopt::test_shell_pane_stops" in out, out
    assert "PASSED test_inner.py::TestStays::test_script_runs" in out, out
    assert "ERROR test_inner.py::TestStays" not in out, out
    assert "FAILED test_inner.py::TestDies::test_job" in out, out
    assert "ctl:job exited 4" in out, out
    assert "script's session kept: tmux -S" in out, out
    assert "PASSED test_inner.py::TestPlainTmux::test_plain_panes" in out, out
    assert "ERROR test_inner.py::TestPlainTmux" not in out, out


# adopt()'s edges: what it finds, what it reads, how it fails.
EDGES = """
import sys, pytest
from libtmux_easy.testing import Processes
PY = sys.executable
SIGINT_OK = {"*": (0, 130)}  # sleep dies of Ctrl-C

def script(tmp_path, text):
    path = tmp_path / "s.py"
    path.write_text("from libtmux_easy import TmuxSession\\n" + text)
    return f"{PY} {path}"

def test_two_sessions(tmp_path):
    with Processes(stop_codes=SIGINT_OK) as procs:
        procs.adopt(script(tmp_path, '''
for name in ("one", "two"):
    s = TmuxSession.create(name, mode="wrap")
    s.new_window("w")
    s.new_pane("x", "sleep 60")
'''))
        assert sorted(procs.names) == ["one.w:x", "script:script", "two.w:x"]

@pytest.mark.parametrize("capture", [True, False])
def test_reads_from_the_first_byte(tmp_path, capture):
    # printed before adopt(): from the script's capture; else, though the
    # script's watch piped it late, the scrollback
    with Processes(stop_codes=SIGINT_OK) as procs:
        procs.adopt(script(tmp_path, f'''
s = TmuxSession.create("early", mode="wrap", pane_capture={capture})
s.new_window("w")
s.new_pane("x", "echo early_1; sleep 60")
s.wait_for_output("w:x", "early_1", timeout=10)
'''))
        procs["w:x"].wait_for("early_1", timeout=5)
        assert procs.output("w:x").splitlines().count("early_1") == 1
        assert "early_1" in open(procs.log("w:x")).read()

def test_no_panes(tmp_path):
    with Processes() as procs:
        with pytest.raises(AssertionError, match="made no tmux panes"):
            procs.adopt(script(tmp_path, "print('nothing here')"))

def test_script_fails(tmp_path):
    with Processes() as procs:
        with pytest.raises(AssertionError, match="exit"):
            procs.adopt(script(tmp_path, "raise SystemExit(2)"))
        procs.close(check=False)

def test_adopted_panes_cant_start_again(tmp_path):
    with Processes(stop_codes=SIGINT_OK) as procs:
        [x] = procs.adopt(script(tmp_path, '''
s = TmuxSession.create("again", mode="wrap")
s.new_window("w")
s.new_pane("x", "sleep 60")
'''))
        with pytest.raises(ValueError, match="can't be started again"):
            x.start()

def test_adopt_and_build_come_first():
    with Processes(stop_codes=SIGINT_OK) as procs:
        procs.start("w:a", "sleep 60")
        with pytest.raises(ValueError, match="call it first"):
            procs.adopt("true")
        with pytest.raises(ValueError, match="call it first"):
            procs.build(lambda session: None)
"""


@pytest.mark.parametrize("mode", MODES)
def test_adopt_edges(run, mode):
    proc, out = run(EDGES, mode=mode)
    for test in (
        "test_two_sessions",
        "test_reads_from_the_first_byte[True]",
        "test_reads_from_the_first_byte[False]",
        "test_no_panes",
        "test_script_fails",
        "test_adopted_panes_cant_start_again",
        "test_adopt_and_build_come_first",
    ):
        assert f"PASSED test_inner.py::{test}" in out, out
    assert proc.returncode == 0, out
