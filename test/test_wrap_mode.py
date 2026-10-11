"""
Tests for wrap mode: panes whose command runs under lte-run
(src/libtmux_easy/wrap.py), which reports the exact pid and exit status over
the session's control socket. See docs/wrap-mode.md.
"""
import logging
import os
import signal
import subprocess
import sys
import time

import psutil
import pytest

from helpers import pane_text as _pane_text, wait_until as _wait_until
from libtmux_easy import PANE_MODE_WRAP, PANE_MODE_SHELL
from libtmux_easy import wrap
from libtmux_easy.tmux_lib import _wrap_prefix


def test_wrapper_import_stays_light():
    """lte-run starts once per pane, and its console script imports the package
    __init__ first. Pulling libtmux and psutil in there cost ~4x the startup
    time and +7 MB per pane."""
    probe = "import sys, libtmux_easy.wrap; print('libtmux' in sys.modules, 'psutil' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert out.stdout.split() == ["False", "False"], out.stderr


class TestWrapStandalone:
    """lte-run outside any session: a transparent passthrough."""

    @pytest.fixture
    def env(self):
        env = dict(os.environ)
        for name in ("LTE_DIR", "TMUX", "TMUX_PANE"):
            env.pop(name, None)
        return env

    def _run(self, env, *argv):
        # A subprocess, not wrap.main(): main() changes signal dispositions
        # for the rest of the process it runs in.
        return subprocess.run([sys.executable, wrap.__file__, *argv], env=env,
                              capture_output=True, text=True, timeout=10)

    def test_wrap_standalone_passthrough(self, env):
        assert self._run(env, "sh", "-c", "exit 42").returncode == 42

    def test_wrap_standalone_reraises_signal(self, env):
        assert self._run(env, "sh", "-c", "kill -INT $$").returncode == -signal.SIGINT

    def test_wrap_standalone_exec_failure(self, env):
        proc = self._run(env, "no-such-command-xyz")
        assert proc.returncode == 127
        assert "lte-run: no-such-command-xyz" in proc.stderr


class TestWrapMode:
    def test_wrap_exact_exit_code(self, session):
        pane = session.new_pane("x", "sh -c 'sleep 0.5; exit 42'", mode=PANE_MODE_WRAP)

        status = session.wait_for_exit(pane, timeout=5.0)

        assert status.code == 42
        assert status.inferred is False
        assert session.pane_status(pane).mode == PANE_MODE_WRAP

    def test_wrap_distinguishes_exit_130_from_sigint(self, session):
        """The regression test for the feature: shell mode reads both of these
        as 128+2; exec mode can lose either to the tmux reaping bug."""
        exited = session.new_pane("exited", "sh -c 'sleep 0.3; exit 130'", mode=PANE_MODE_WRAP)
        killed = session.new_pane("killed", "sleep 60", mode=PANE_MODE_WRAP)

        assert session.stop_pane(killed, sigint_timeout=5.0) is True
        exited_status = session.wait_for_exit(exited, timeout=5.0)
        killed_status = session.exit_status(killed)

        assert (exited_status.code, exited_status.signal) == (130, None)
        assert (killed_status.code, killed_status.signal) == (None, 2)
        assert not exited_status.inferred and not killed_status.inferred

    def test_wrap_status_survives_user_commands(self, session):
        pane = session.new_pane("x", "sh -c 'sleep 0.3; exit 42'", mode=PANE_MODE_WRAP)
        assert session.wait_for_exit(pane, timeout=5.0).code == 42

        session.send_keys("false", target_pane=pane)
        session.send_keys("ls", target_pane=pane)
        time.sleep(0.5)

        assert str(session.exit_status(pane)) == "exited 42"

    def test_wrap_target_pid_is_exact(self, session):
        pane = session.new_pane("x", "sleep 30", mode=PANE_MODE_WRAP)

        pids = session.pane_status(pane).target_pids
        assert len(pids) == 1
        target = psutil.Process(pids[0])
        assert target.name() == "sleep"
        wrapper = target.parent()
        assert "wrap.py" in " ".join(wrapper.cmdline()) or "lte-run" in wrapper.name()
        assert wrapper.pid == session._wrap_runs[pane.id].wrapper_pid
        assert wrapper.parent().pid == int(pane.pane_pid)  # bash, the pane process

    def test_wrap_pane_stays_interactive(self, session):
        pane = session.new_pane("x", "true", mode=PANE_MODE_WRAP)
        session.wait_for_exit(pane, timeout=5.0)

        session.send_keys("echo hi_$((6*7))", target_pane=pane)

        assert _wait_until(lambda: "hi_42" in _pane_text(pane))

    def test_wrap_stop_escalates(self, session):
        pane = session.new_pane("x", "sh -c 'trap \"\" INT; exec sleep 60'", mode=PANE_MODE_WRAP)

        assert session.stop_pane(pane, sigint_timeout=1.0) is True
        status = session.exit_status(pane)

        assert (status.code, status.signal) == (None, 15)

    def test_wrap_kills_target_children(self, session):
        """SIGKILL goes to the whole target group, so nothing is orphaned.
        The ignored dispositions are inherited by both sleeps."""
        cmd = "sh -c 'trap \"\" INT TERM; sleep 60 & sleep 60; wait'"
        pane = session.new_pane("x", cmd, mode=PANE_MODE_WRAP)
        target = psutil.Process(session.pane_status(pane).target_pids[0])
        assert _wait_until(lambda: len(target.children()) == 2)
        sleeps = target.children()

        assert session.stop_pane(pane, sigint_timeout=1.0, sigterm_timeout=1.0) is True

        assert session.exit_status(pane).signal == 9
        assert _wait_until(lambda: not any(p.is_running() and p.status() != psutil.STATUS_ZOMBIE
                                           for p in sleeps))

    def test_wrap_manual_rerun_tracked(self, session):
        """A rerun typed into the pane (up-arrow, Enter) is a new run."""
        pane = session.new_pane("x", "sh -c 'sleep 0.3; exit 4'", mode=PANE_MODE_WRAP)
        assert session.wait_for_exit(pane, timeout=5.0).code == 4
        old_pids = session.pane_status(pane).target_pids

        session.send_keys(f"{_wrap_prefix()} sh -c 'sleep 0.5; exit 8'", target_pane=pane)

        assert _wait_until(lambda: session.pane_status(pane).target_pids != old_pids)
        assert session.is_running(pane) is True
        assert not session.exit_status(pane).known
        assert session.wait_for_exit(pane, timeout=5.0).code == 8

    def test_wrap_upgrades_shell_pane(self, session):
        pane = session.new_pane("x", "true", mode=PANE_MODE_SHELL)
        session.wait_for_pane_idle(pane, timeout=5.0)
        assert session.exit_status(pane).code == 0

        session.send_keys(f"{_wrap_prefix()} sh -c 'sleep 0.3; exit 5'", target_pane=pane)

        assert _wait_until(lambda: session.pane_status(pane).mode == PANE_MODE_WRAP)
        status = session.wait_for_exit(pane, timeout=5.0)
        assert (status.code, status.inferred) == (5, False)

    def test_wrap_on_exit_fires_once(self, session):
        calls = []
        pane = session.new_pane("x", "sh -c 'sleep 0.5; exit 3'", mode=PANE_MODE_WRAP,
                                on_exit=calls.append)
        assert _wait_until(lambda: calls)
        time.sleep(0.5)

        assert len(calls) == 1
        assert calls[0].exit_status.code == 3

        start = time.time()
        assert session.wait_for_exit(pane, timeout=5.0).code == 3
        assert time.time() - start < 0.5
        assert len(calls) == 1

    def test_wrap_lost_wrapper(self, session, caplog):
        pane = session.new_pane("x", "sleep 2", mode=PANE_MODE_WRAP)
        run = session._wrap_runs[pane.id]
        target = psutil.Process(run.target_pid)

        with caplog.at_level(logging.WARNING, logger="libtmux_easy.tmux_lib"):
            os.kill(run.wrapper_pid, signal.SIGKILL)
            assert _wait_until(lambda: run.lost)
        assert any("without reporting" in r.getMessage() for r in caplog.records)

        assert target.is_running()
        assert session.is_running(pane) is True  # follows the orphaned target

        assert session.wait_for_exit(pane, timeout=5.0).known is False
        assert session.is_running(pane) is False

    def test_wrap_shell_operators(self, session):
        pane = session.new_pane("x", "sleep 0.1 && exit 9", mode=PANE_MODE_WRAP)

        assert session.wait_for_exit(pane, timeout=5.0).code == 9

    def test_wrap_respawn_pane(self, session):
        pane = session.new_pane("x", "sleep 60", mode=PANE_MODE_WRAP)
        old_pids = session.pane_status(pane).target_pids

        session.respawn_pane(pane, "sh -c 'sleep 0.3; exit 6'")

        assert session.pane_status(pane).target_pids != old_pids
        assert session.wait_for_exit(pane, timeout=5.0).code == 6

    def test_wrap_enter_false_waits_for_enter(self, session):
        pane = session.new_pane("x", "sh -c 'exit 2'", mode=PANE_MODE_WRAP, enter=False)
        assert session.pane_status(pane).target_pids == ()

        pane.send_keys("", enter=True)

        assert _wait_until(lambda: session.pane_status(pane).target_pids != ())
        assert session.wait_for_exit(pane, timeout=5.0).code == 2

    def test_wrap_teardown(self, session):
        directory = session._wrap_dir
        assert os.path.exists(os.path.join(directory, wrap.SOCKET_NAME))

        session.kill_session()

        assert not os.path.exists(directory)

    def test_wrap_command_with_dash_t(self, session):
        """A second pane whose command contains '-t' (here '--topic'): its
        keys used to land in the first pane. See _pane_cmd()."""
        session.new_pane("first", "sleep 30", mode=PANE_MODE_WRAP)
        pane = session.new_pane("second", "sh -c 'exit 7' --topic x", mode=PANE_MODE_WRAP)

        assert session.wait_for_exit(pane, timeout=5.0).code == 7
