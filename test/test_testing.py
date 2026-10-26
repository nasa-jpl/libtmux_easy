"""The pytest plugin (libtmux_easy.testing), run in an inner pytest, with
subprocesses and in a detached tmux session."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")
MODES = {"subprocess": [], "tmux": ["--tmux", "--no-attach"]}

# Prints, then runs until SIGINT and exits 0 -- or with 'die', exits 3 first.
PROC = """
import sys, time
try:
    print("hello_" + str(6 * 7), flush=True)
    time.sleep(0.5 if "die" in sys.argv else 60)
except KeyboardInterrupt:
    sys.exit(0)
sys.exit(3)
"""


@pytest.fixture
def run(tmp_path):
    """run(tests, *args, mode=, env=): an inner pytest of 'tests' in
    tmp_path; returns (proc, its output). Kills the tmux sessions it names
    (kept on failure) afterwards."""
    sessions = []

    def run(tests, *args, mode="subprocess", env=None, files=None):
        (tmp_path / "conftest.py").write_text('pytest_plugins = ["libtmux_easy.testing"]\n')
        (tmp_path / "proc.py").write_text(PROC)
        for name, text in (files or {}).items():
            (tmp_path / name).write_text(text)
        (tmp_path / "test_inner.py").write_text(tests)
        full_env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        full_env.update(env or {}, PYTHONPATH=SRC)
        proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                               "-p", "no:libtmux", "-rA", *MODES[mode], *args, "test_inner.py"],
                              cwd=tmp_path, env=full_env, capture_output=True, text=True,
                              timeout=120)
        out = proc.stdout + proc.stderr
        sessions.extend(re.findall(r"tmux attach -t ([\w-]+)", out))
        servers.extend(re.findall(r"tmux -S (\S+) attach", out))
        return proc, out

    servers = []
    yield run
    for session in set(sessions):
        subprocess.run(["tmux", "kill-session", "-t", f"={session}"], capture_output=True)
    for server in set(servers):  # adopt()'s, kept on failure
        subprocess.run(["tmux", "-S", server, "kill-server"], capture_output=True)
        shutil.rmtree(Path(server).parents[1], ignore_errors=True)


TESTS = """
import sys, time
from libtmux_easy.testing import Processes
CMD = f"{sys.executable} proc.py"

class TestRun:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls)
        cls.procs.start("w:ok", CMD, ready="hello_42")
        cls.procs.start("w:other", CMD + " %s", ready="hello_42")

    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_output(self):
        self.procs.wait_for("w:ok", "hello_42", timeout=5)
        time.sleep(%s)
"""
PASSES = TESTS % ("", 0)
DIES = TESTS % ("die", 2)


@pytest.mark.parametrize("mode", MODES)
def test_passes_and_shuts_down_cleanly(run, mode):
    proc, out = run(PASSES, mode=mode)
    assert proc.returncode == 0, out
    assert "processes kept" not in out


@pytest.mark.parametrize("mode", MODES)
def test_death_fails_the_test(run, mode):
    proc, out = run(DIES, mode=mode)
    assert proc.returncode == 1, out
    assert "FAILED test_inner.py::TestRun::test_output" in out
    assert "w:other exited 3" in out
    if mode == "tmux":
        session = re.search(r"processes kept: tmux attach -t ([\w-]+)", out)
        assert session, out  # kept on failure
        assert subprocess.run(["tmux", "has-session", "-t", session[1]]).returncode == 0
    else:
        assert "[w:ok] hello_42" not in out  # output only to the logs


def test_default_is_subprocess(run):
    proc, out = run(PASSES)
    assert proc.returncode == 0, out
    assert "lte mode: subprocess" in out
    assert "[w:ok] hello_42" not in out
    log_dir = re.findall(r"process logs: (\S+)", out)[-1]  # header, then the group's
    assert open(os.path.join(log_dir, "w.ok.log")).read() == "hello_42\n"


def test_echo_all(run):
    proc, out = run(PASSES, "--echo", "*")
    assert proc.returncode == 0, out
    assert "[w:ok] hello_42" in out and "[w:other] hello_42" in out


def test_echo_some(run):
    proc, out = run(PASSES, "--echo", "w:o?")
    assert proc.returncode == 0, out
    assert "[w:ok] hello_42" in out and "[w:other]" not in out


def test_tmux_logs(run):
    proc, out = run(PASSES, mode="tmux")
    log_dir = re.search(r"process logs: (\S+)", out)[1]
    assert "hello_42" in open(os.path.join(log_dir, "TestRun0", "w.ok.log")).read()


@pytest.mark.parametrize("flags", [["--tmux"], ["--no-attach"]])
def test_tmux_without_a_terminal_is_detached(run, flags):
    proc, out = run(PASSES, *flags)
    assert proc.returncode == 0, out
    assert "lte mode: tmux" in out


def test_keep_open_leaves_processes_running(run):
    proc, out = run(PASSES, "--keep-open")
    assert proc.returncode == 0, out
    session = re.search(r"processes still running: tmux attach -t ([\w-]+)", out)
    assert session, out
    panes = subprocess.run(["tmux", "list-panes", "-s", "-t", session[1], "-F",
                            "#{pane_dead} #{pane_current_command}"],
                           capture_output=True, text=True).stdout
    assert panes.count("0 ") >= 2 and "python" in panes, panes


def test_tmux_not_installed(run, tmp_path):
    (tmp_path / "bin").mkdir()
    proc, out = run(PASSES, "--keep-open", env={"PATH": str(tmp_path / "bin")})
    assert proc.returncode == 4, out
    assert "--tmux: tmux is not installed" in out


# Ready once it says so; later prints a line fail_on catches.
SERVER = """
import sys, time
print("listening on 8080", flush=True)
time.sleep(0.3)
print("tick 1", flush=True)
try:
    time.sleep(1)
    print("BOOM", flush=True)
    time.sleep(60)
except KeyboardInterrupt:
    sys.exit(0)
"""

OUTPUT_TESTS = """
import sys
from libtmux_easy.testing import Processes

class TestSrv:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls, fail_on={"*": "BOOM"})
        cls.procs.start("w:srv", f"{sys.executable} srv.py", ready=%r, timeout=2)

    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_ready(self):
        assert "listening on 8080" in self.procs.output("w:srv")
        assert self.procs.wait_for("w:srv", r"tick (\\d)").group(1) == "1"

    def test_fail_on(self):
        self.procs.wait_for("w:srv", "BOOM")
"""


@pytest.mark.parametrize("mode", MODES)
def test_ready_wait_for_and_fail_on(run, mode):
    proc, out = run(OUTPUT_TESTS % r"listening on \d+", mode=mode, files={"srv.py": SERVER})
    assert proc.returncode == 1, out
    assert "1 failed, 1 passed in" in out  # and no teardown error
    assert "FAILED test_inner.py::TestSrv::test_fail_on" in out
    assert "w:srv printed 'BOOM' (fail_on 'BOOM')" in out


@pytest.mark.parametrize("mode", MODES)
def test_not_ready(run, mode):
    proc, out = run(OUTPUT_TESTS % "never printed", mode=mode, files={"srv.py": SERVER})
    assert proc.returncode == 1, out
    assert "ERROR test_inner.py::TestSrv::test_ready" in out
    assert "w:srv printed no line matching 'never printed' within 2s" in out


# A test of its own processes: start, stop, restart; then a death.
LAUNCHER_TESTS = """
import sys, time
import pytest
from libtmux_easy.testing import Processes
CMD = f"{sys.executable} proc.py"

def test_start_stop_restart():
    procs = Processes()
    procs.start("w:a", CMD, ready=r"hello_42")
    procs.start("w:b", CMD, ready=r"hello_42")   # same window
    procs.start("other:c", CMD, ready=r"hello_42")
    assert procs.stop("w:a").exit_status.ok      # a clean stop is not a death
    procs.start("w:a", CMD, ready=r"hello_42")   # and the name can start again
    assert procs.output("w:a").count("hello_42") == 1
    assert "hello_42" in open(procs.log("w:a")).read()

def test_handle():
    procs = Processes()
    a = procs.start("w:a", CMD, ready=r"hello_42")
    assert a.name == "w:a" and a.status().running
    assert a.wait_for(r"hello_([0-9]+)").group(1) == "42"
    assert "hello_42" in a.output() and "hello_42" in open(a.log()).read()
    assert a.stop().exit_status.ok and not a.status().running
    procs.start("w:a", CMD, ready=r"hello_42")   # the handle follows the name
    assert a.status().running
    a.stop()
    a.start()                                       # same command, ready= too
    assert a.output().count("hello_42") == 1

def test_with():
    with Processes() as procs:
        a = procs.start("w:a", CMD, ready=r"hello_42")
    assert not a.status().running                   # closed, and checked, at the end

def test_with_unclean():
    with Processes(sigint_timeout=0.5) as procs:
        procs.start("w:stubborn", "trap '' INT; sleep 60")

@pytest.fixture
def made_in_a_fixture():
    with pytest.raises(TypeError, match="outside a test"):
        Processes()

def test_outside_a_test(made_in_a_fixture):
    pass

def test_already_running():
    procs = Processes()
    procs.start("solo", CMD, ready=r"hello_42")
    procs.start("solo", CMD)

def test_death():
    procs = Processes()
    procs.start("w:dies", CMD + " die", ready=r"hello_42")
    time.sleep(2)

def test_fail_on_per_start():
    procs = Processes()
    procs.start("w:bad", f"echo BAD; exec {CMD}", fail_on="BAD")
    time.sleep(1)

def test_unclean_stop():
    procs = Processes()
    procs.start("w:a", CMD, ready=r"hello_42")
    procs.start("w:stubborn", "trap '' INT; sleep 60")
    procs.stop("w:stubborn", sigint_timeout=0.5)
"""


@pytest.mark.parametrize("mode", MODES)
def test_own_processes(run, mode):
    proc, out = run(LAUNCHER_TESTS, mode=mode)
    assert "PASSED test_inner.py::test_start_stop_restart" in out, out
    assert "PASSED test_inner.py::test_handle" in out, out
    assert "PASSED test_inner.py::test_with" in out, out
    assert "FAILED test_inner.py::test_with_unclean" in out, out
    assert "PASSED test_inner.py::test_outside_a_test" in out, out
    assert "FAILED test_inner.py::test_already_running" in out, out
    assert "solo:solo is already running" in out, out
    assert "FAILED test_inner.py::test_death" in out, out
    assert "w:dies exited 3" in out, out
    assert "FAILED test_inner.py::test_fail_on_per_start" in out, out
    assert "w:bad printed 'BAD' (fail_on 'BAD')" in out, out
    assert "FAILED test_inner.py::test_unclean_stop" in out, out
    assert "w:stubborn did not stop cleanly" in out, out
    assert "ERROR" not in out.split("short test summary")[-1], out  # teardowns clean


def test_concurrent_tmux_runs_dont_clash(tmp_path):
    runs = []
    for i in range(2):
        d = tmp_path / str(i)
        d.mkdir()
        (d / "conftest.py").write_text('pytest_plugins = ["libtmux_easy.testing"]\n')
        (d / "proc.py").write_text(PROC)
        (d / "test_inner.py").write_text(TESTS % ("", 3))
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env["PYTHONPATH"] = SRC
        runs.append(subprocess.Popen(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "no:libtmux",
             "--tmux", "test_inner.py"], cwd=d, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True))
    for proc in runs:
        out = proc.communicate(timeout=60)[0]
        assert proc.returncode == 0, out


class _FakeSession:
    def __init__(self, *names):
        self.windows = [type("W", (), {"name": n})() for n in names]


def test_unique_prefix():
    from libtmux_easy.testing import _unique_prefix
    assert _unique_prefix(_FakeSession("dash", "other/w"), "mod") == "mod"
    assert _unique_prefix(_FakeSession("mod/w", "mod#2/w"), "mod") == "mod#3"
    assert _unique_prefix(_FakeSession("module/w"), "mod") == "mod"


# setup_class / teardown_class; fail_on functions; wait_until; Processes()
# in a test beside the class's group; LTE_LOG_DIR; BASH_ENV not sourced.
CLASS_TESTS = """
import os, sys, time
import pytest
from libtmux_easy.testing import Processes, check_health
CMD = f"{sys.executable} proc.py"

class TestA:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls, fail_on={"*": lambda line: "saw BAD" if "BAD" in line else None})
        cls.procs.start("w:a", CMD, ready=r"hello_42")
        cls.procs.wait_until(lambda: "hello_42" in cls.procs.output("w:a"), timeout=5)
        cls.procs.start("w:env", f"echo dir=$LTE_LOG_DIR bash_env=${{BASH_ENV_RAN:-no}}; exec {CMD}")

    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_one(self):
        self.procs.wait_for("w:env", "dir=" + os.path.dirname(self.procs.log("w:env")) + " bash_env=no")

    def test_own_processes(self):
        procs = Processes()
        procs.start("w:solo", CMD, ready=r"hello_42")  # the class's window w
        assert self.procs.status("w:a").running
        with pytest.raises(ValueError, match="w:a is taken"):
            procs.start("w:a", CMD)

    def test_bad_line(self):
        self.procs.start("w:bad", f"echo BAD; exec {CMD}")
        time.sleep(1)

    def test_after_failure(self):
        pass  # skipped: test_bad_line's failure stopped the class's processes

class TestLong:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls)

    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_long(self):
        self.procs.start("w:dies", CMD + " die")
        for _ in range(20):
            check_health()
            time.sleep(0.2)

class TestSetupFails:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls)
        cls.procs.start("w:x", CMD)
        cls.procs.start("w:y", CMD, ready="never", timeout=0.5)

    def test_x(self):
        pass

def test_after_classes():
    # nothing left running from either class
    import libtmux_easy.testing as t
    assert t._open == [], t._open
"""


@pytest.mark.parametrize("mode", MODES)
def test_setup_class(run, tmp_path, mode):
    (tmp_path / "bash_env").write_text("export BASH_ENV_RAN=yes\n")
    proc, out = run(CLASS_TESTS, mode=mode, env={"BASH_ENV": str(tmp_path / "bash_env")})
    assert "PASSED test_inner.py::TestA::test_one" in out, out
    assert "PASSED test_inner.py::TestA::test_own_processes" in out, out
    assert "FAILED test_inner.py::TestA::test_bad_line" in out, out
    assert "w:bad: saw BAD" in out, out
    assert "processes stopped: test_bad_line failed" in out, out
    assert "FAILED test_inner.py::TestLong::test_long" in out, out
    assert "w:dies exited 3" in out, out
    assert "saw BAD" not in out.split("test_long")[1].split("short test summary")[0], out
    assert "ERROR test_inner.py::TestA" not in out, out  # teardown_class: nothing to check
    assert "ERROR test_inner.py::TestLong" not in out, out
    assert "ERROR test_inner.py::TestSetupFails::test_x" in out, out
    assert "w:y printed no line matching 'never'" in out, out
    assert "PASSED test_inner.py::test_after_classes" in out, out


FEATURE_TESTS = """
import os, sys
import pytest
from libtmux_easy.testing import Processes, arg, parametrize
CMD = f"{sys.executable} proc.py"
RATE = arg("rate", 1)

def test_arg():
    assert RATE == 7 and arg("name", "x") == "x"

def test_owner_is_a_class():
    with pytest.raises(TypeError, match="a test class"):
        Processes(sys.modules[__name__])

def test_exits():
    procs = Processes()
    procs.start("w:job", "echo one; echo two; echo three; exit 4", exits=4)
    assert procs.wait_for_exit("w:job").exit_status.code == 4
    procs.wait_for_in_order("w:job", ["one", "three"])
    procs.wait_for("*", "two")

def test_wrong_exit():
    procs = Processes()
    procs.start("w:job", "echo bye; exit 5", exits=0)
    procs.wait_for_exit("w:job")

def test_out_of_order():
    procs = Processes()
    procs.start("w:job", f"echo three; echo one; exec {CMD}")
    procs.wait_for_in_order("w:job", ["one", "three"], timeout=1)

def test_stop_codes():
    procs = Processes()
    procs.start("w:s", "sleep 60", stop_codes=(0, 130))
    procs.stop("w:s")

def test_close_without_checks():
    procs = Processes()
    procs.start("w:stubborn", "trap '' INT; sleep 60")
    procs.sigint_timeout = 0.5
    procs.close(check=False)
    assert not procs.status("w:stubborn").running

@parametrize("n", [1, 2])
class TestParams:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls, stop_codes={"*": (0, 130)})
        cls.procs.start("w:p", f"echo n={cls.n}; exec {CMD}", ready="hello_42")

    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_n(self):
        self.procs.wait_for("w:p", f"n={self.n}")

@parametrize("n", [1, 2])
class TestParamFails:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls)
        cls.procs.start("w:p", CMD, ready="hello_42")

    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_fails_at_1(self):
        assert self.n == 2

    def test_after(self):
        pass  # skipped at n=1 only

class TestAbort:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls, on_abort=lambda: open("abort_called", "w").close())
        cls.procs.start("w:a", CMD, ready="hello_42")

    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_fails(self):
        assert False

    def test_skipped(self):
        pass

def test_after_abort():
    import libtmux_easy.testing as t
    assert os.path.exists("abort_called")
    assert not TestAbort.procs.status("w:a").running
    assert t._open == [], t._open
"""


@pytest.mark.parametrize("mode", MODES)
def test_features(run, mode):
    proc, out = run(FEATURE_TESTS, "--arg", "rate=7", "--arg", "unused=1", mode=mode)
    for test in ("test_arg", "test_owner_is_a_class", "test_exits", "test_stop_codes",
                 "test_close_without_checks", "TestParams[n=1]::test_n",
                 "TestParams[n=2]::test_n", "TestParamFails[n=2]::test_after",
                 "test_after_abort"):
        assert f"PASSED test_inner.py::{test}" in out, out
    for test in ("test_wrong_exit", "test_out_of_order", "TestAbort::test_fails",
                 "TestParamFails[n=1]::test_fails_at_1"):
        assert f"FAILED test_inner.py::{test}" in out, out
    assert "processes stopped: test_fails failed" in out, out
    assert "processes stopped: test_fails_at_1 failed" in out, out
    assert "w:job exited 5 (expected exit 0)" in out, out
    assert "w:job: no line matching 'three' after 'one'" in out, out
    assert "warning: no test read --arg unused" in out, out
    assert "ERROR" not in out, out


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

    def test_job_finished(self):
        assert self.procs["ctl:job"].wait_for_exit(5).exit_status.code == 4
"""


@pytest.mark.parametrize("mode", MODES)
def test_build(run, mode):
    proc, out = run(BUILD_TESTS, mode=mode, files={"script.py": SCRIPT})
    for test in ("test_names", "test_monitor_sees_a_kill", "test_restart",
                 "test_names_unique_in_the_class", "test_job_finished"):
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

    def test_kill(self):
        with self.procs.expect_exit("sim:a"):
            self.procs["sim:a"].kill()
        assert self.procs["sim:a"].status().exit_status.signal == 9

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
"""


@pytest.mark.parametrize("mode", MODES)
def test_adopt(run, mode):
    proc, out = run(ADOPT_TESTS, mode=mode, files={"script.py": SCRIPT})
    assert "PASSED test_inner.py::TestAdopt::test_names" in out, out
    assert "PASSED test_inner.py::TestAdopt::test_kill" in out, out
    assert "PASSED test_inner.py::TestAdopt::test_shell_pane_stops" in out, out
    assert "PASSED test_inner.py::TestStays::test_script_runs" in out, out
    assert "ERROR test_inner.py::TestStays" not in out, out
    assert "FAILED test_inner.py::TestDies::test_job" in out, out
    assert "ctl:job exited 4" in out, out
    assert "script's session kept: tmux -S" in out, out
