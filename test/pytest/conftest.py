"""The 'run' fixture: an inner pytest with the plugin (see inner.py)."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import SRC
from inner import MODES, PROC


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
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-p",
                "no:libtmux",
                "-rA",
                *MODES[mode],
                *args,
                "test_inner.py",
            ],
            cwd=tmp_path,
            env=full_env,
            capture_output=True,
            text=True,
            timeout=120,
        )
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
