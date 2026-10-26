"""Method 3: run the script as it is; adopt the panes it made.

    pytest demos/pytest/script_adopt -v

The script runs on a tmux server of its own, so its session name can't
clash with yours. Its attach() does nothing. It must exit 0 (or pass
ready= for one that keeps running). Panes can't be started again; exit
statuses are exact for wrap and exec panes.
"""
import os
import sys
import urllib.request
from pathlib import Path

from libtmux_easy.testing import Processes

PORT = 9700 + os.getpid() % 200
SCRIPT = Path(__file__).with_name("launch_adopt.py")


class TestAdopt:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls)
        cls.procs.adopt(f"{sys.executable} {SCRIPT} --port {PORT}", exits={"jobs:migrate": 0})

    @classmethod
    def teardown_class(cls):
        cls.procs.close()  # Ctrl-C each pane; fail unless each exits 0

    def test_panes(self):
        assert self.procs.names == ["script:script", "servers:web", "workers:ticker",
                                    "jobs:migrate"]

    def test_web(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2) as reply:
            assert reply.status == 200
        self.procs["servers:web"].wait_for(r'"GET / HTTP')

    def test_ticker(self):
        self.procs["workers:ticker"].wait_for(r"tick 3")

    def test_migrate_finished(self):
        assert self.procs["jobs:migrate"].wait_for_exit().exit_status.ok
