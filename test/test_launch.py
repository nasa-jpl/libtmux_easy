"""
Tests for lte-session (src/libtmux_easy/launch.py) and TmuxSession.current().
See docs/lte-session.md. The launcher runs headless here (no
TTY), which is its CI path: it follows the command's output and exits with
its status.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from libtmux_easy import TmuxSession, PANE_MODE_WRAP
from libtmux_easy import dash

from helpers import wait_until

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _launch(*argv, env=None, cwd=None, timeout=60):
    env = dict(os.environ if env is None else env)
    for name in ("TMUX", "TMUX_PANE"):
        env.pop(name, None)
    env["PYTHONPATH"] = SRC
    return subprocess.run([sys.executable, "-m", "libtmux_easy.launch", *argv],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          env=env, cwd=cwd, timeout=timeout)


def _has_session(name):
    return subprocess.run(["tmux", "has-session", "-t", name], capture_output=True).returncode == 0


class TestLauncher:
    def test_exit_code_output_env_and_cwd(self, tmp_path):
        """The command's status and output; it runs with the launching env and cwd."""
        env = dict(os.environ, LTE_TEST_VAR="from_outside")
        proc = _launch("--name", "lte-test-launch-a", "--", "sh", "-c",
                       "echo hello var=$LTE_TEST_VAR; pwd; exit 3", env=env, cwd=tmp_path)

        assert proc.returncode == 3, proc.stderr
        assert "hello var=from_outside" in proc.stdout
        assert str(tmp_path) in proc.stdout
        assert not _has_session("lte-test-launch-a")

    def test_signal_death_is_128_plus_n(self):
        proc = _launch("--name", "lte-test-launch-b", "--", "sh", "-c", "kill -INT $$")
        assert proc.returncode == 130, proc.stderr

    def test_keep(self):
        proc = _launch("--name", "lte-test-launch-d", "--keep", "--", "true")
        try:
            assert proc.returncode == 0
            assert _has_session("lte-test-launch-d")
        finally:
            subprocess.run(["tmux", "kill-session", "-t", "lte-test-launch-d"])

    def test_pytest_plugin_adopts_the_session(self, tmp_path):
        (tmp_path / "conftest.py").write_text('pytest_plugins = ["libtmux_easy.testing"]\n')
        (tmp_path / "test_inner.py").write_text(textwrap.dedent("""
            import os
            from libtmux_easy.testing import Processes

            class TestAdopted:
                @classmethod
                def setup_class(cls):
                    cls.procs = Processes(cls)

                @classmethod
                def teardown_class(cls):
                    cls.procs.close(check=False)

                def test_adopted(self):
                    assert os.environ.get("TMUX_PANE")
                    self.procs.start("inner:p", "sleep 30")
                    assert self.procs.status("inner:p").running
                    print("INNER_OK")

            def build(session):
                session.new_window("w")
                session.new_pane("x", "sleep 30")

            class TestBuilt:
                def test_build(self):
                    with Processes() as procs:
                        procs.build(build)
                        assert procs["w:x"].status().running
                        assert procs.session.get_window("w").name.endswith("/w")
                        procs.close(check=False)
                    print("BUILD_OK")

            class TestScript:
                def test_adopt(self):
                    import sys
                    with Processes() as procs:
                        [x] = procs.adopt(f"{sys.executable} script.py")
                        assert x.name == "w:x" and x.status().running
                        procs.close(check=False)
                    print("ADOPT_OK")
        """))
        (tmp_path / "script.py").write_text(textwrap.dedent("""
            from libtmux_easy import TmuxSession
            s = TmuxSession.create("scripted", mode="wrap")
            s.new_window("w")
            s.new_pane("x", "sleep 30")
            s.attach()
        """))
        proc = _launch("--name", "lte-test-launch-e", "--", sys.executable, "-m", "pytest",
                       "-q", "-s", "-p", "no:cacheprovider", "-p", "no:libtmux",
                       "test_inner.py", cwd=tmp_path)

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "INNER_OK" in proc.stdout
        assert "BUILD_OK" in proc.stdout and "ADOPT_OK" in proc.stdout, proc.stdout


@pytest.fixture
def host(make_session):
    """A session to adopt, with this process posing as one of its panes."""
    return make_session("lte-test-current", window="host")


@pytest.fixture
def inside(host, monkeypatch):
    pane = host.windows[0].panes[0]
    socket_path = pane.cmd("display-message", "-p", "#{socket_path}").stdout[0]
    monkeypatch.setenv("TMUX", f"{socket_path},{os.getpid()},0")
    monkeypatch.setenv("TMUX_PANE", pane.id)
    return host


class TestCurrent:
    def test_outside_tmux_raises(self, monkeypatch):
        monkeypatch.delenv("TMUX_PANE", raising=False)
        with pytest.raises(RuntimeError, match="not running inside tmux"):
            TmuxSession.current()

    def test_adopt_and_release(self, inside):
        adopted = TmuxSession.current()
        assert adopted.id == inside.id
        with pytest.raises(ValueError, match="new_window"):
            adopted.new_pane("p", "true")

        adopted.new_window("mine")
        pane = adopted.new_pane("p", "sleep 30", mode=PANE_MODE_WRAP)
        assert adopted.is_running(pane)
        assert inside._session_option("@lte_dir") == adopted._wrap_dir

        adopted.kill_session()

        assert _has_session("lte-test-current")
        assert [w.name for w in inside.windows] == ["host"]
        assert inside._session_option("@lte_dir") == ""

    def test_env_leaves_session_alone(self, inside, tmp_path):
        """current() writes nothing to the session's tmux environment, even
        for an environment past tmux's 16 KB command limit."""
        before = inside.server.cmd("show-environment", "-t", inside.id).stdout
        adopted = TmuxSession.current(env={"LTE_ADOPT": "x" * 20000})
        adopted.new_window("mine")
        out = tmp_path / "v"
        adopted.new_pane("p", f"echo $LTE_ADOPT > {out}")
        assert wait_until(lambda: out.exists() and len(out.read_text()) == 20001)
        assert inside.server.cmd("show-environment", "-t", inside.id).stdout == before
        adopted.kill_session()

    def test_reuses_dashboard_and_publishes_status(self, inside):
        window, _ = dash.open_window(inside.id, None, None, None, home_cmd="cat",
                                     sidebar_cmd="cat")

        adopted = TmuxSession.current()
        assert adopted._dash_window == window
        assert adopted.status_file is not None
        assert inside._session_option(dash.STATUS_OPTION) == os.path.abspath(adopted.status_file)
        adopted.new_window("mine")
        adopted.new_pane("p", "sleep 30", mode=PANE_MODE_WRAP)
        assert [e["name"] for e in dash.read_status(adopted.status_file)] == ["mine:p"]

        adopted.close_dashboard()  # not ours: left open
        adopted.kill_session()
        assert window in [w.id for w in inside.windows]
        assert inside._session_option(dash.STATUS_OPTION) == ""

    def test_release_puts_a_viewed_pane_back(self, inside):
        window, sidebar = dash.open_window(inside.id, None, None, None, home_cmd="cat",
                                           sidebar_cmd="cat")
        adopted = TmuxSession.current()
        adopted.new_window("mine")
        pane = adopted.new_pane("p", "sleep 30", mode=PANE_MODE_WRAP)
        viewport = dash.Viewport(window, run=inside._tmux_run, sidebar=sidebar)
        home = viewport.home
        viewport.show(pane.id)

        adopted.stop_all()
        adopted.kill_session()

        assert viewport.panes().get(home) == window  # home survived, back in place
        assert pane.id not in viewport.panes()

    def test_release_keeping_windows(self, inside, tmp_path):
        adopted = TmuxSession.current(status_file=str(tmp_path / "status.json"))
        adopted.new_window("nodes")
        pane = adopted.new_pane("p", "sleep 30", mode=PANE_MODE_WRAP)
        adopted.stop_all()

        adopted.release(keep_windows=True, label="test_x")

        assert [w.name for w in inside.windows] == ["host", "test_x/nodes"]
        assert pane.id in [p.id for w in inside.windows for p in w.panes]
        assert [e["name"] for e in dash.read_status(adopted.status_file)] == ["test_x/nodes:p"]
        nxt = TmuxSession.current()
        nxt.new_window("nodes")  # the name is free again
        nxt.kill_session()

    def test_release_own_session_keeps_it(self, session):
        session.new_pane("p", "true", mode=PANE_MODE_WRAP)
        session.release(keep_windows=True)
        assert _has_session("lte-test")
        session.release()  # already released: a no-op
        assert _has_session("lte-test")
        subprocess.run(["tmux", "kill-session", "-t", "lte-test"])
