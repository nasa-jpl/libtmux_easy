"""Method 2: the test runs the script's build() on a session it controls.

    pytest demos/pytest/script_build -v [--tmux]

Each pane build() makes is a process of the group, by the script's names;
the script's own startup order (its wait_for_output) runs as written.
Always tmux. TestMonitoring also tests the script's runtime watches.
"""
import os
import urllib.error
import urllib.request

import launch_build as launch

from libtmux_easy.testing import Processes

PORT = 9700 + os.getpid() % 200
EXITS = {"jobs:migrate": 0}


def get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=2) as reply:
            return reply.status
    except urllib.error.HTTPError as err:
        return err.code


class TestSystem:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls)
        cls.procs.build(launch.build, port=PORT, exits=EXITS)

    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_panes(self):
        assert self.procs.names == ["servers:web", "workers:ticker", "jobs:migrate"]

    def test_web(self):
        assert get("/") == 200
        self.procs["servers:web"].wait_for(r'"GET / HTTP')

    def test_migrate_finished(self):
        assert self.procs["jobs:migrate"].wait_for_exit().exit_status.ok


class TestMonitoring:
    """The script's monitor(), on the same session: make it fire on purpose."""

    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls)
        cls.procs.build(launch.build, port=PORT + 1000, exits=EXITS)
        cls.procs["jobs:migrate"].wait_for_exit()  # done before the tests poke at it
        cls.alerts = []
        launch.monitor(cls.procs.session, alert=cls.alerts.append)

    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_alerts_on_a_crash(self):
        with self.procs.expect_exit("workers:ticker"):  # on purpose: no death
            self.procs["workers:ticker"].kill()
        self.procs.wait_until(lambda: any("ticker" in a for a in self.alerts), timeout=5)
        assert "signal 9" in self.alerts[-1]

    def test_alerts_on_a_404(self):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT + 1000}/nope", timeout=2)
        except urllib.error.HTTPError:
            pass
        self.procs.wait_until(lambda: any("404" in a for a in self.alerts), timeout=5)
