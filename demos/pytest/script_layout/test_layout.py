"""Method 1: the script's system is data; the test starts it.

    pytest demos/pytest/script_layout -v [--tmux]

The test gets everything start() has: ready= per process, its own gates,
restarts, subprocess or tmux. The script's tmux code (main()) is not tested.
"""
import os
import urllib.request

import launch_layout as launch

from libtmux_easy.testing import Processes

PORT = 9700 + os.getpid() % 200
READY = {"servers:web": r"Serving HTTP", "workers:ticker": r"tick 0"}
EXITS = {"jobs:migrate": 0}  # a one-shot job: finishing is no death


class TestLayout:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls)
        for name, cmd in launch.panes(PORT):
            cls.procs.start(name, cmd, ready=READY.get(name), exits=EXITS.get(name))

    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_web(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2) as reply:
            assert reply.status == 200

    def test_migrate_finished(self):
        self.procs["jobs:migrate"].wait_for_exit()
        assert "migrated" in self.procs["jobs:migrate"].output()

    def test_ticker_restart(self):
        ticker = self.procs["workers:ticker"]
        ticker.stop()
        ticker.start()  # same command, ready= too
        ticker.wait_for(r"tick 2")
