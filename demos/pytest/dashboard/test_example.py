"""The dashboard: watch the processes while the tests run, and talk to the
test itself (pdb, input()) in the home pane.

    pytest -s demos/pytest/dashboard --tmux             # the dashboard, attached
    pytest demos/pytest/dashboard --tmux --no-attach    # detached tmux session
    pytest demos/pytest/dashboard                       # subprocesses

LTE_DEMO_CRASH=1 makes the ticker die mid-test, which fails the test (and
with --no-attach keeps the session for 'tmux attach'). With --tmux -s,
LTE_PDB=1 drops into pdb in the last test, and LTE_ASK=1 waits for input.
"""
import os
import sys
import urllib.request

from libtmux_easy.testing import Processes

PORT = 8765 + os.getpid() % 1000
CRASH = "sys.exit(3) if i == 4 else None; " if os.environ.get("LTE_DEMO_CRASH") else ""
TICKER = (f"{sys.executable} -u -c 'import sys, time\n"
          f"try:\n"
          f"    for i in range(10**6): {CRASH}print(\"tick\", i); time.sleep(0.5)\n"
          f"except KeyboardInterrupt: pass'")


def answers():
    try:
        return urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=1).status == 200
    except OSError:
        return False


class TestDashboard:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls)
        cls.web = cls.procs.start("servers:web", f"{sys.executable} -m http.server {PORT} --bind 127.0.0.1")
        cls.ticker = cls.procs.start("workers:ticker", TICKER)

    @classmethod
    def teardown_class(cls):
        cls.procs.close()

    def test_web_server_answers(self):
        self.procs.wait_until(answers, timeout=5)  # a dead process fails it now
        assert '"GET / HTTP' in self.web.output()

    def test_ticker_keeps_ticking(self):
        self.ticker.wait_for(r"tick 6", timeout=10)

    def test_inspect(self):
        """A process of this test's own, to poke at in pdb."""
        with Processes() as procs:
            flaky = procs.start("scratch:flaky", "sleep 1; exit 3", exits=3)
            if os.environ.get("LTE_ASK"):
                print("you typed:", input("say something: "))
            if os.environ.get("LTE_PDB"):
                breakpoint()  # try: p flaky.status()
            assert flaky.wait_for_exit().exit_status.code == 3
