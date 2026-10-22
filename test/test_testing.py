"""The pytest plugin (libtmux_easy.testing): 'processes' in shell and tmux
mode, run in an inner pytest."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parents[1] / "src")

# Prints, then runs until SIGINT and exits 0 -- or with 'die', exits 3 first.
PROC = """
import sys, time
print("hello_" + str(6 * 7), flush=True)
try:
    time.sleep(0.5 if "die" in sys.argv else 60)
except KeyboardInterrupt:
    sys.exit(0)
sys.exit(3)
"""

TESTS = """
import sys, time
PROCESSES = {"w:ok": f"{sys.executable} proc.py", "w:other": f"{sys.executable} proc.py %s"}

def test_output(processes):
    deadline = time.time() + 5
    while "hello_42" not in processes.output("w:ok") and time.time() < deadline:
        time.sleep(0.1)
    assert "hello_42" in processes.output("w:ok")
    time.sleep(%s)
"""


def _pytest(tmp_path, mode, die, *args):
    (tmp_path / "conftest.py").write_text('pytest_plugins = ["libtmux_easy.testing"]\n')
    (tmp_path / "proc.py").write_text(PROC)
    (tmp_path / "test_inner.py").write_text(TESTS % ("die" if die else "", 2 if die else 0))
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["PYTHONPATH"] = SRC
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                           "-p", "no:libtmux", f"--lte-mode={mode}", *args, "test_inner.py"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("mode", ["shell", "tmux"])
def test_passes_and_shuts_down_cleanly(tmp_path, mode):
    proc = _pytest(tmp_path, mode, die=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "processes kept" not in proc.stdout


@pytest.mark.parametrize("mode", ["shell", "tmux"])
def test_death_fails_the_test(tmp_path, mode):
    proc = _pytest(tmp_path, mode, die=True)
    session = re.search(r"tmux attach -t ([\w-]+)", proc.stdout)
    try:
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "FAILED test_inner.py::test_output" in proc.stdout
        assert "w:other exited 3" in proc.stdout
        if mode == "tmux":
            assert "processes kept" in proc.stdout  # kept on failure
            assert subprocess.run(["tmux", "has-session", "-t", session[1]]).returncode == 0
        else:
            assert "[w:ok] hello_42" in proc.stdout  # output to the terminal
    finally:
        if session:
            subprocess.run(["tmux", "kill-session", "-t", session[1]], capture_output=True)


def test_default_is_shell(tmp_path):
    proc = _pytest(tmp_path, "auto", False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "lte mode: shell" in proc.stdout
    assert "[w:ok] hello_42" in proc.stdout
    log_dir = re.search(r"process logs: (\S+)", proc.stdout)[1]
    assert open(os.path.join(log_dir, "test_inner0", "w.ok.log")).read() == "hello_42\n"


def test_tmux_logs(tmp_path):
    proc = _pytest(tmp_path, "tmux", False)
    log_dir = re.search(r"process logs: (\S+)", proc.stdout)[1]
    assert "hello_42" in open(os.path.join(log_dir, "test_inner0", "w.ok.log")).read()


def test_tmux_flag(tmp_path):
    proc = _pytest(tmp_path, "auto", False, "--tmux")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "lte mode: tmux" in proc.stdout


def test_keep_open_leaves_processes_running(tmp_path):
    proc = _pytest(tmp_path, "auto", False, "--keep-open")
    session = re.search(r"tmux attach -t ([\w-]+)", proc.stdout)
    try:
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "processes still running" in proc.stdout
        panes = subprocess.run(["tmux", "list-panes", "-s", "-t", session[1], "-F",
                                "#{pane_dead} #{pane_current_command}"],
                               capture_output=True, text=True).stdout
        assert panes.count("0 ") >= 2 and "python" in panes, panes
    finally:
        if session:
            subprocess.run(["tmux", "kill-session", "-t", session[1]], capture_output=True)


def test_keep_open_needs_tmux(tmp_path):
    proc = _pytest(tmp_path, "shell", False, "--keep-open")
    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert "--keep-open needs tmux" in proc.stderr


# Ready once it says so; later prints a line FAIL_ON catches.
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
PROCESSES = {"w:srv": f"{sys.executable} srv.py"}
READY = {"w:srv": %r}
READY_TIMEOUT = 2
FAIL_ON = {"*": "BOOM"}

def test_ready(processes):
    assert "listening on 8080" in processes.output("w:srv")
    assert processes.wait_for("w:srv", r"tick (\\d)").group(1) == "1"

def test_fail_on(processes):
    processes.wait_for("w:srv", "BOOM")
"""


def _pytest_output(tmp_path, mode, ready):
    (tmp_path / "conftest.py").write_text('pytest_plugins = ["libtmux_easy.testing"]\n')
    (tmp_path / "srv.py").write_text(SERVER)
    (tmp_path / "test_inner.py").write_text(OUTPUT_TESTS % ready)
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["PYTHONPATH"] = SRC
    proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                           "-p", "no:libtmux", f"--lte-mode={mode}", "--lte-keep=never",
                           "test_inner.py"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    return proc, proc.stdout + proc.stderr


@pytest.mark.parametrize("mode", ["shell", "tmux"])
def test_ready_wait_for_and_fail_on(tmp_path, mode):
    proc, out = _pytest_output(tmp_path, mode, r"listening on \d+")
    assert proc.returncode == 1, out
    assert "1 failed, 1 passed in" in out  # and no teardown error
    assert "FAILED test_inner.py::test_fail_on" in out
    assert "w:srv printed 'BOOM' (FAIL_ON 'BOOM')" in out


@pytest.mark.parametrize("mode", ["shell", "tmux"])
def test_not_ready(tmp_path, mode):
    proc, out = _pytest_output(tmp_path, mode, "never printed")
    assert proc.returncode == 1, out
    assert "not ready: w:srv printed no line matching 'never printed' within 2s" in out


# A test of its own processes: start, stop, restart; then a death.
LAUNCHER_TESTS = """
import sys
CMD = f"{sys.executable} proc.py"

def test_start_stop_restart(launcher):
    launcher.start("w:a", CMD, ready=r"hello_42")
    launcher.start("w:b", CMD, ready=r"hello_42")   # same window
    launcher.start("other:c", CMD, ready=r"hello_42")
    assert launcher.stop("w:a").exit_status.ok      # a clean stop is not a death
    launcher.start("w:a", CMD, ready=r"hello_42")   # and the name can start again
    assert launcher.output("w:a").count("hello_42") == 1
    assert "hello_42" in open(launcher.log("w:a")).read()

def test_with_module_processes(processes, launcher):
    launcher.start("solo", CMD, ready=r"hello_42")
    assert "hello_42" in processes.output("m:ok") or processes.wait_for("m:ok", "hello_42")

def test_death(launcher):
    launcher.start("w:dies", CMD + " die", ready=r"hello_42")
    import time; time.sleep(2)

def test_unclean_stop(launcher):
    launcher.start("w:a", CMD, ready=r"hello_42")
    launcher.start("w:stubborn", "trap '' INT; sleep 60")
    launcher.stop("w:stubborn", sigint_timeout=0.5)

PROCESSES = {"m:ok": CMD}
"""


@pytest.mark.parametrize("mode", ["shell", "tmux"])
def test_launcher(tmp_path, mode):
    (tmp_path / "conftest.py").write_text('pytest_plugins = ["libtmux_easy.testing"]\n')
    (tmp_path / "proc.py").write_text(PROC)
    (tmp_path / "test_inner.py").write_text(LAUNCHER_TESTS)
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["PYTHONPATH"] = SRC
    proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                           "-p", "no:libtmux", f"--lte-mode={mode}", "--lte-keep=never",
                           "-rA", "test_inner.py"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    out = proc.stdout + proc.stderr
    assert "PASSED test_inner.py::test_start_stop_restart" in out, out
    assert "PASSED test_inner.py::test_with_module_processes" in out, out
    assert "FAILED test_inner.py::test_death" in out, out
    assert "w:dies exited 3" in out, out
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
    for run in runs:
        out = run.communicate(timeout=60)[0]
        assert run.returncode == 0, out


class _FakeSession:
    def __init__(self, *names):
        self.windows = [type("W", (), {"name": n})() for n in names]


def test_unique_prefix():
    from libtmux_easy.testing import _unique_prefix
    assert _unique_prefix(_FakeSession("dash", "other/w"), "mod") == "mod"
    assert _unique_prefix(_FakeSession("mod/w", "mod#2/w"), "mod") == "mod#3"
    assert _unique_prefix(_FakeSession("module/w"), "mod") == "mod"


# setup_class / teardown_class with new_processes(); FAIL_ON functions;
# wait_until; LTE_LOG_DIR.
CLASS_TESTS = """
import os, sys, time
from libtmux_easy.testing import check_health, new_processes
CMD = f"{sys.executable} proc.py"

class TestA:
    FAIL_ON = {"*": lambda line: "saw BAD" if "BAD" in line else None}

    @classmethod
    def setup_class(cls):
        cls.procs = new_processes(cls)
        cls.procs.start("w:a", CMD, ready=r"hello_42")
        cls.procs.wait_until(lambda: "hello_42" in cls.procs.output("w:a"), timeout=5)
        cls.procs.start("w:env", f"echo dir=$LTE_LOG_DIR; exec {CMD}")

    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_one(self):
        self.procs.wait_for("w:env", "dir=" + os.path.dirname(self.procs.log("w:env")))

    def test_bad_line(self):
        self.procs.start("w:bad", f"echo BAD; exec {CMD}")
        time.sleep(1)

    def test_after_failure(self):
        pass  # skipped: test_bad_line's failure stopped the class's processes

class TestLong:
    @classmethod
    def setup_class(cls):
        cls.procs = new_processes(cls)

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
        cls.procs = new_processes(cls)
        cls.procs.start("w:x", CMD)
        cls.procs.start("w:y", CMD, ready="never", timeout=0.5)

    def test_x(self):
        pass

def test_after_classes():
    # nothing left running from either class
    import libtmux_easy.testing as t
    assert t._open == [], t._open
"""


@pytest.mark.parametrize("mode", ["shell", "tmux"])
def test_setup_class(tmp_path, mode):
    (tmp_path / "conftest.py").write_text('pytest_plugins = ["libtmux_easy.testing"]\n')
    (tmp_path / "proc.py").write_text(PROC)
    (tmp_path / "test_inner.py").write_text(CLASS_TESTS)
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["PYTHONPATH"] = SRC
    proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                           "-p", "no:libtmux", f"--lte-mode={mode}", "--lte-keep=never",
                           "-rA", "test_inner.py"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    out = proc.stdout + proc.stderr
    assert "PASSED test_inner.py::TestA::test_one" in out, out
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
from libtmux_easy.testing import arg, new_processes, parametrize
CMD = f"{sys.executable} proc.py"
RATE = arg("rate", 1)

def test_arg():
    assert RATE == 7 and arg("name", "x") == "x"

def test_exits(launcher):
    launcher.start("w:job", "echo one; echo two; echo three; exit 4", exits=4)
    assert launcher.wait_for_exit("w:job").exit_status.code == 4
    launcher.wait_for_in_order("w:job", ["one", "three"])
    launcher.wait_for("*", "two")

def test_wrong_exit(launcher):
    launcher.start("w:job", "echo bye; exit 5", exits=0)
    launcher.wait_for_exit("w:job")

def test_out_of_order(launcher):
    launcher.start("w:job", f"echo three; echo one; exec {CMD}")
    launcher.wait_for_in_order("w:job", ["one", "three"], timeout=1)

def test_stop_codes(launcher):
    launcher.start("w:s", "sleep 60", stop_codes=(0, 130))
    launcher.stop("w:s")

@parametrize("n", [1, 2])
class TestParams:
    @classmethod
    def setup_class(cls):
        cls.procs = new_processes(cls)
        cls.procs.start("w:p", f"echo n={cls.n}; exec {CMD}", ready="hello_42")

    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_n(self):
        self.procs.wait_for("w:p", f"n={self.n}")

class TestAbort:
    @classmethod
    def setup_class(cls):
        cls.procs = new_processes(cls)
        cls.procs.start("w:a", CMD, ready="hello_42")

    @classmethod
    def abort(cls):
        open("abort_called", "w").close()

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


@pytest.mark.parametrize("mode", ["shell", "tmux"])
def test_features(tmp_path, mode):
    (tmp_path / "conftest.py").write_text('pytest_plugins = ["libtmux_easy.testing"]\n')
    (tmp_path / "proc.py").write_text(PROC)
    (tmp_path / "test_inner.py").write_text(FEATURE_TESTS)
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["PYTHONPATH"] = SRC
    proc = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                           "-p", "no:libtmux", f"--lte-mode={mode}", "--lte-keep=never",
                           "--arg", "rate=7", "--arg", "unused=1", "-rA", "test_inner.py"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    out = proc.stdout + proc.stderr
    for test in ("test_arg", "test_exits", "test_stop_codes", "TestParams[n=1]::test_n",
                 "TestParams[n=2]::test_n", "test_after_abort"):
        assert f"PASSED test_inner.py::{test}" in out, out
    for test in ("test_wrong_exit", "test_out_of_order", "TestAbort::test_fails"):
        assert f"FAILED test_inner.py::{test}" in out, out
    assert "w:job exited 5 (expected exit 0)" in out, out
    assert "w:job: no line matching 'three' after 'one'" in out, out
    assert "processes stopped: test_fails failed" in out, out
    assert "warning: no test read --arg unused" in out, out
    assert "ERROR" not in out, out
