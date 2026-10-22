"""A tour of the plugin, no ROS: bring a system up once for the class,
run tests against it, shut it down.

    pytest demos/pytest/basic/test_tour.py -v               # subprocesses, output here
    pytest demos/pytest/basic/test_tour.py -v --tmux        # the dashboard; --no-attach: detached
    pytest demos/pytest/basic/test_tour.py -v --keep-open   # leave it all running after
    LTE_DEMO_CRASH=1 pytest demos/pytest/basic/test_tour.py -v   # a worker dies mid-test

What it shows:
  setup_class      start in stages: a server, a gate (any code), then workers
  READY / ready=   wait for a line in a process's output before going on
  FAIL_ON          a line that fails the test (a regex, or a function)
  every test       checked after it ran: a death fails it, not a later one
  test_long_run    a long test calling check_health(), so a death fails it
                   within half a second, not at the end
  launcher         a process just one test needs, stopped after it
  teardown_class   close(): stop everything, fail unless each exits 0 on SIGINT
  logs             each process's output in a file: 'process logs:' at the end
"""
import os
import sys
import tempfile
import time
import urllib.request

from libtmux_easy.testing import check_health, new_processes

PY = sys.executable
PORT = 8700 + os.getpid() % 500  # two runs at once don't share a port
SERVER = f"{PY} -u -m http.server {PORT} --bind 127.0.0.1"
CRASH_FILE = os.path.join(tempfile.gettempdir(), f"lte_tour_crash_{os.getpid()}")


HERE = os.path.dirname(os.path.abspath(__file__))


def worker(name):
    return f"{PY} -u {HERE}/worker.py {name} {CRASH_FILE}"


def server_answers():
    try:
        return urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=1).status == 200
    except OSError:
        return False


class TestTour:
    # Read by new_processes() (a class attribute, else the module's).
    FAIL_ON = {
        "*": r"Traceback", # any process
        "workers:w1": lambda line: "w1 is sad" if "sad" in line else None
    }

    @classmethod
    def setup_class(cls):
        cls.procs = new_processes(cls)  # empty; logs in a tmp dir of its own
        # Stage 1. A name is 'window:pane': in tmux, window 'servers', pane 'web'.
        cls.procs.start("servers:web", SERVER, ready=r"Serving HTTP")
        # Gate: any code. wait_until() fails at once if something died.
        cls.procs.wait_until(server_answers, timeout=10)
        # Stage 2.
        for n in range(2):
            cls.procs.start(f"workers:w{n}", worker(f"w{n}"), ready=r"tick 0")

    @classmethod
    def teardown_class(cls):
        if os.path.exists(CRASH_FILE):
            os.remove(CRASH_FILE)
        cls.procs.close()

    def test_server(self):
        assert server_answers()
        self.procs.wait_for("servers:web", r'"GET / HTTP/1\.\d" 200')  # its output

    def test_output_and_logs(self):
        self.procs.wait_for("workers:w0", r"tick 5")
        assert "w0 tick 0" in self.procs.output("workers:w0")
        assert "w0 tick 0" in open(self.procs.log("workers:w0")).read()

    def test_stop_and_restart(self):
        status = self.procs.stop("workers:w1")  # fails unless it exits 0 on SIGINT
        assert status.exit_status.ok
        self.procs.start("workers:w1", worker("w1"), ready=r"tick 0")  # fresh output

    def test_a_process_of_its_own(self, launcher):
        launcher.start("tools:helper", worker("helper"), ready=r"tick 0")  # stopped after this test

    def test_long_run(self):
        """10 s of work. With LTE_DEMO_CRASH=1, the workers die at 3 s."""
        start = time.monotonic()
        while time.monotonic() - start < 10:
            if os.environ.get("LTE_DEMO_CRASH") and time.monotonic() - start > 3:
                open(CRASH_FILE, "w").close()
            check_health()  # or self.procs.check()
            time.sleep(0.5)
