"""The pytest plugin under lte-session (docs/lte-session.md): processes join
its session; build() and adopt() work there too."""
import sys
import textwrap

from helpers import lte_session as _launch


def test_plugin_under_lte_session(tmp_path):
    (tmp_path / "conftest.py").write_text('pytest_plugins = ["libtmux_easy.testing"]\n')
    (tmp_path / "test_inner.py").write_text(textwrap.dedent("""
        import os
        from libtmux_easy.testing import Processes

        class TestAdopted:
            @classmethod
            def setup_class(cls):
                cls.procs = Processes(cls, env={"LTE_T_GROUP": "g"})

            @classmethod
            def teardown_class(cls):
                cls.procs.close(check=False)

            def test_adopted(self):
                assert os.environ.get("TMUX_PANE")
                self.procs.start("inner:p", "sleep 30")
                assert self.procs.status("inner:p").running
                self.procs.start("inner:e", 'echo "G=$LTE_T_GROUP"; sleep 30')
                self.procs.wait_for("inner:e", "G=g")
                assert "LTE_T_GROUP" not in os.environ
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
