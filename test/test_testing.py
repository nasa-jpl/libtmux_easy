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


def _pytest(tmp_path, mode, die):
    (tmp_path / "conftest.py").write_text('pytest_plugins = ["libtmux_easy.testing"]\n')
    (tmp_path / "proc.py").write_text(PROC)
    (tmp_path / "test_inner.py").write_text(TESTS % ("die" if die else "", 2 if die else 0))
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    env["PYTHONPATH"] = SRC
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                           f"--lte-mode={mode}", "test_inner.py"],
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
