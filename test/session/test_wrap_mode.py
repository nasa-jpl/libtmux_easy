"""
Tests for wrap mode: panes whose command runs under lte-run
(src/lte_run/wrap.py), which reports the exact pid and exit status over
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

from helpers import pane_text as _pane_text, title as _pane_title, wait_until as _wait_until
from libtmux_easy import PANE_MODE_WRAP, PANE_MODE_SHELL
from libtmux_easy import lte_dash
from lte_run import wrap


def test_wrapper_import_stays_light():
    """lte-run starts once per pane: the lte_run package stays out of
    libtmux_easy, whose import loads libtmux and psutil (about 3x the
    startup, +8 MB per pane)."""
    probe = "import sys, lte_run.wrap; print('libtmux' in sys.modules, 'psutil' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
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
        env = dict(env, PYTHONPATH=os.pathsep.join(sys.path))
        return subprocess.run(
            [sys.executable, "-m", "lte_run", *argv],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    @pytest.mark.parametrize(
        "argv, code, stderr",
        [
            (("sh", "-c", "exit 42"), 42, ""),
            (("sh", "-c", "kill -INT $$"), -signal.SIGINT, ""),  # the signal, re-raised
            (("no-such-command-xyz",), 127, "lte-run: no-such-command-xyz"),
            (("--bogus", "true"), 2, ""),
        ],
    )
    def test_wrap_standalone_status(self, env, argv, code, stderr):
        proc = self._run(env, *argv)
        assert proc.returncode == code
        assert stderr in proc.stderr

    def test_restart(self, env):
        """--restart N: a failure reruns up to N times, then its code stands;
        a success isn't rerun; a bad count is a usage error."""
        proc = self._run(env, "--restart", "2", "--restart-delay=0", "sh", "-c", "echo run; exit 3")
        assert proc.returncode == 3 and proc.stdout.split() == ["run"] * 3
        assert proc.stderr.count("lte-run: ") == 2 and "exited 3; restart 2/2" in proc.stderr
        ok = self._run(env, "--restart", "-1", "true")
        assert (ok.returncode, ok.stderr) == (0, "")
        assert self._run(env, "--restart", "-2", "true").returncode == 2

    def test_restart_not_after_a_stop(self, env):
        """A signal to lte-run stops it for good: running, or between runs."""
        env = dict(env, PYTHONPATH=os.pathsep.join(sys.path))
        for command, sig in (("sleep 30", -signal.SIGTERM), ("exit 4", 4)):
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "lte_run",
                    "--restart",
                    "-1",
                    "--restart-delay",
                    "30",
                    "sh",
                    "-c",
                    f"echo up; {command}",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert proc.stdout.readline() == "up\n"
            time.sleep(0.3)  # the exit 4: into the delay
            proc.send_signal(signal.SIGTERM)
            out, _ = proc.communicate(timeout=10)
            assert proc.returncode == sig and "up" not in out

    def test_tee(self, env, tmp_path):
        """Outside tmux, through a pipe: shown and copied as plain text; -a
        appends, --raw keeps escapes; LTE_RUN_TEE is like --tee and not passed
        on; an unwritable file still runs the command."""
        log = tmp_path / "out.log"
        cmd = r"echo out; echo err >&2; printf '\033[31mred\033[0m\n'; printf '1\r2\r3\n'; exit 3"
        proc = self._run(env, "--tee", str(log), "sh", "-c", cmd)
        assert proc.returncode == 3
        assert "out" in proc.stdout and "err" in proc.stdout
        assert log.read_text() == "out\nerr\nred\n3\n"

        self._run(env, "--tee", str(log), "-a", "--raw", "printf", r"\033[1mx\033[0m\n")
        assert log.read_text() == "out\nerr\nred\n3\n\x1b[1mx\x1b[0m\n"

        proc = self._run(
            dict(env, **{wrap.TEE_ENV: str(log)}), "sh", "-c", 'echo "seen=[$LTE_RUN_TEE]"'
        )
        assert proc.returncode == 0 and log.read_text() == "seen=[]\n"

        proc = self._run(env, "--tee", str(tmp_path / "no" / "dir.log"), "sh", "-c", "exit 4")
        assert proc.returncode == 4 and "--tee" in proc.stderr


class TestWrapMode:
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
        assert session.pane_status(exited).mode == PANE_MODE_WRAP

    def test_wrap_status_survives_user_commands(self, session):
        """The pane stays an interactive shell; what is typed there that isn't
        lte-run leaves the status alone."""
        pane = session.new_pane("x", "sh -c 'sleep 0.3; exit 42'", mode=PANE_MODE_WRAP)
        assert session.wait_for_exit(pane, timeout=5.0).code == 42

        session.send_keys("false", target_pane=pane)
        session.send_keys("echo hi_$((6*7))", target_pane=pane)

        assert _wait_until(lambda: "hi_42" in _pane_text(pane))
        time.sleep(0.3)
        assert str(session.exit_status(pane)) == "exited 42"

    def test_wrap_target_pid_respawn_and_teardown(self, session):
        """The target pid is exact (bash -> lte-run -> command); respawn is a
        new target; kill() removes the control socket's dir."""
        pane = session.new_pane("x", "sleep 30", mode=PANE_MODE_WRAP)

        pids = session.pane_status(pane).target_pids
        assert len(pids) == 1
        target = psutil.Process(pids[0])
        assert target.name() == "sleep"
        wrapper = target.parent()
        assert "lte_run" in " ".join(wrapper.cmdline()) or "lte-run" in wrapper.name()
        assert wrapper.pid == session._wrap_runs[pane.id].wrapper_pid
        assert wrapper.parent().pid == int(pane.pane_pid)  # bash, the pane process

        session.respawn_pane(pane, "sh -c 'sleep 0.3; exit 6'")
        assert session.pane_status(pane).target_pids != pids
        assert session.wait_for_exit(pane, timeout=5.0).code == 6

        directory = session._wrap_dir
        assert os.path.exists(os.path.join(directory, wrap.SOCKET_NAME))
        session.kill()
        assert not os.path.exists(directory)

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
        assert _wait_until(
            lambda: not any(p.is_running() and p.status() != psutil.STATUS_ZOMBIE for p in sleeps)
        )

    def test_wrap_rerun_typed_is_a_new_run(self, make_session, tmp_path):
        """An lte-run typed into the pane is a new run, and its argv the pane's
        command: in the status, the status file, and for respawn_pane().
        Via send_keys(), then straight into tmux."""
        path = str(tmp_path / "status.json")
        session = make_session(status_file=path)
        pane = session.new_pane("x", "sh -c 'sleep 0.3; exit 4'", mode=PANE_MODE_WRAP)
        assert session.wait_for_exit(pane, timeout=5.0).code == 4
        old_pids = session.pane_status(pane).target_pids

        session.send_keys("lte-run sh -c 'sleep 0.5; exit 8'", target_pane=pane)

        assert _wait_until(lambda: session.pane_status(pane).target_pids != old_pids)
        assert session.is_running(pane) is True
        assert not session.exit_status(pane).known
        assert session.wait_for_exit(pane, timeout=5.0).code == 8
        assert session.pane_status(pane).command == "sh -c 'sleep 0.5; exit 8'"

        session.server.cmd("send-keys", "-t", pane.id, "-l", "lte-run sh -c 'sleep 0.3; exit 6'")
        session.server.cmd("send-keys", "-t", pane.id, "Enter")

        assert _wait_until(lambda: session.pane_status(pane).command == "sh -c 'sleep 0.3; exit 6'")
        assert session.wait_for_exit(pane, timeout=5.0).code == 6
        assert _wait_until(
            lambda: lte_dash.read_status(path)[0]["command"] == "sh -c 'sleep 0.3; exit 6'"
        )
        session.respawn_pane(pane)
        assert session.wait_for_exit(pane, timeout=5.0).code == 6

    def test_wrap_upgrades_shell_pane(self, session):
        pane = session.new_pane("x", "true", mode=PANE_MODE_SHELL)
        session.wait_for_pane_idle(pane, timeout=5.0)
        assert session.exit_status(pane).code == 0

        session.send_keys("lte-run sh -c 'sleep 0.3; exit 5'", target_pane=pane)

        assert _wait_until(lambda: session.pane_status(pane).mode == PANE_MODE_WRAP)
        status = session.wait_for_exit(pane, timeout=5.0)
        assert (status.code, status.inferred) == (5, False)
        assert session.pane_status(pane).command == "sh -c 'sleep 0.3; exit 5'"

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

    def test_wrap_tee(self, session, tmp_path):
        """In a pane: the command keeps its terminal, tmux copies its output,
        and the file is complete when the exit is reported."""
        log = tmp_path / "pane.log"
        pane = session.new_pane(
            "x",
            "sh -c 'test -t 1 && echo tty; echo done; exit 5'",
            mode=PANE_MODE_WRAP,
            cmd_capture=str(log),
        )
        assert session.wait_for_exit(pane, timeout=5.0).code == 5
        assert log.read_text() == "tty\ndone\n"
        assert _wait_until(lambda: session._pane_fmt(pane, "#{pane_pipe}") == "0")

        session.respawn_pane(pane, "sh -c 'echo rerun; exit 0'")
        session.wait_for_exit(pane, timeout=5.0)
        assert log.read_text() == "rerun\n"
        with pytest.raises(ValueError, match="cmd_capture"):
            session.new_pane("y", "true", mode=PANE_MODE_SHELL, cmd_capture=str(log))

    def test_wrap_enter_false_waits_for_enter(self, session):
        pane = session.new_pane("x", "sh -c 'exit 2'", mode=PANE_MODE_WRAP, enter=False)
        assert session.pane_status(pane).target_pids == ()

        pane.send_keys("", enter=True)

        assert _wait_until(lambda: session.pane_status(pane).target_pids != ())
        assert session.wait_for_exit(pane, timeout=5.0).code == 2

    def test_wait_start_false(self, make_session, tmp_path):
        """Typed before the shell is up: a slow rc file of each pane runs at
        the same time, and each command once it is done. A pane still
        starting can be stopped."""
        rc = tmp_path / "rc"
        rc.write_text("sleep 1\n")
        session = make_session(mode=PANE_MODE_WRAP, rc=str(rc), wait_start=False)
        exits = []

        start = time.monotonic()
        quick = session.new_pane("quick", "sh -c 'exit 3'", on_exit=exits.append)
        slow = [session.new_pane(f"p{i}", "sleep 60") for i in range(3)]
        stopped = session.new_pane("stopped", "sleep 60")
        assert time.monotonic() - start < 1.0
        assert session.pane_status(slow[0]).state == "starting"
        assert session.is_running(slow[0])  # on its way
        assert "starting" in _pane_title(slow[0])
        assert session.stop_pane(stopped, sigint_timeout=5.0) is True
        assert session.exit_status(stopped).signal == signal.SIGINT

        assert session.wait_started(timeout=10) == []
        assert all(session.pane_status(p).state == "running" for p in slow)
        assert _wait_until(lambda: exits)
        assert exits[0].exit_status.code == 3
        assert session.exit_status(quick).code == 3

    def test_wait_start_false_echo_is_not_output(self, make_session, tmp_path):
        """Typed early, the command echoes twice: neither is output."""
        rc = tmp_path / "rc"
        rc.write_text("sleep 0.5\necho from-rc\n")
        session = make_session(mode=PANE_MODE_WRAP, rc=str(rc), wait_start=False)
        lines = []
        pane = session.new_pane(
            "x", "echo hello; sleep 60", on_output={"": lambda m: lines.append(m.line)}
        )

        session.wait_for_output(pane, "hello", timeout=10)
        assert _wait_until(lambda: "hello" in lines)
        assert [line for line in lines if "lte-run" in line] == []

    def test_wait_started_reports_late_panes(self, make_session, tmp_path, caplog):
        rc = tmp_path / "rc"
        rc.write_text("exec sleep 60\n")  # never reads what is typed
        session = make_session(mode=PANE_MODE_WRAP, rc=str(rc), wait_start=False)
        session.new_pane("x", "true")

        assert session.wait_started(timeout=0.5) == ["main:x"]
        assert "lte-run never reported from main:x" in caplog.text

    def test_wrap_shell_operators_and_dash_t(self, session):
        """Shell operators run under bash -c. And a second pane whose command
        contains '-t' (here '--topic'): its keys used to land in the first
        pane. See _pane_cmd()."""
        session.new_pane("first", "sleep 30", mode=PANE_MODE_WRAP)
        pane = session.new_pane(
            "second", "sleep 0.1 && sh -c 'exit 7' --topic x", mode=PANE_MODE_WRAP
        )

        assert session.wait_for_exit(pane, timeout=5.0).code == 7
