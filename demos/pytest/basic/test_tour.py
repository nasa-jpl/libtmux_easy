"""A tour of the plugin, no ROS: bring a system up once for the class,
run tests against it, shut it down.

    pytest demos/pytest/basic/test_tour.py -v               # subprocesses, output in logs
    pytest demos/pytest/basic/test_tour.py -v --tmux        # the dashboard; --no-attach: detached
    pytest demos/pytest/basic/test_tour.py -v --keep-open   # leave it all running after
    LTE_DEMO_CRASH=1 pytest demos/pytest/basic/test_tour.py -v   # a worker dies mid-test

What it shows:
  setup_class      start in stages: a server, a gate (any code), then workers
  ready=           wait for a line in a process's output before going on
  fail_on          a line that fails the test (a regex, or a function)
  every test       checked after it ran: a death fails it, not a later one
  test_long_run    a long test calling check_health(), so a death fails it
                   within half a second, not at the end
  Processes()      in a test: a process just that test needs, stopped after it
  teardown_class   close(): stop everything, fail unless each exits 0 on SIGINT
  logs             each process's output in a file: 'process logs:' at the end
"""
import os
import sys
import tempfile
import time
import urllib.request

from libtmux_easy.testing import Processes, check_health

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


# A line that fails the test: a regex, or a function line -> problem or None.
FAIL_ON = {
    "*": r"Traceback",  # any process
    "workers:w1": lambda line: "w1 is sad" if "sad" in line else None,
}


class TestTour:
    # pytest makes a new TestTour for every test: what the tests share goes
    # on the class (cls.procs), set once in setup_class.

    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls, fail_on=FAIL_ON)  # empty; logs in a tmp dir of its own
        # Stage 1. A name is 'window:pane': in tmux, window 'servers', pane 'web'.
        # start() returns a handle: cls.web.wait_for(...), cls.web.stop().
        cls.web = cls.procs.start("servers:web", SERVER, ready=r"Serving HTTP")
        # Gate: any code. wait_until() fails at once if something died.
        cls.procs.wait_until(server_answers, timeout=10)
        # Stage 2.
        cls.w0, cls.w1 = (cls.procs.start(f"workers:w{n}", worker(f"w{n}"), ready=r"tick 0")
                          for n in range(2))

    @classmethod
    def teardown_class(cls):
        if os.path.exists(CRASH_FILE):
            os.remove(CRASH_FILE)
        cls.procs.close()

    def test_server(self):
        assert server_answers()
        self.web.wait_for(r'"GET / HTTP/1\.\d" 200')  # its output

    def test_output_and_logs(self):
        self.w0.wait_for(r"tick 5")
        assert "w0 tick 0" in self.w0.output()
        assert "w0 tick 0" in open(self.w0.log()).read()

    def test_stop_and_restart(self):
        status = self.w1.stop()  # fails unless it exits 0 on SIGINT
        assert status.exit_status.ok
        self.w1.start()  # the same command again; fresh output

    def test_a_process_of_its_own(self):
        # A second group, for this test only: stopped and checked at the end
        # of the 'with'. The class's processes keep running. Names are
        # unique within the class: "workers:w0" here would be an error.
        with Processes() as procs:
            procs.start("tools:helper", worker("helper"), ready=r"tick 0")

    def test_long_run(self):
        """10 s of work. With LTE_DEMO_CRASH=1, the workers die at 3 s."""
        start = time.monotonic()
        while time.monotonic() - start < 10:
            if os.environ.get("LTE_DEMO_CRASH") and time.monotonic() - start > 3:
                open(CRASH_FILE, "w").close()
            check_health()  # or self.procs.check()
            time.sleep(0.5)
