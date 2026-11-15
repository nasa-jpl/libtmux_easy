"""A tour of the plugin, no ROS: bring a system up once for the class,
run tests against it, shut it down.

    pytest demos/pytest/basic/test_tour.py -v               # subprocesses, output in logs
    pytest demos/pytest/basic/test_tour.py -v --tmux        # the dashboard; --no-attach: detached
    pytest demos/pytest/basic/test_tour.py -v --keep-open   # leave it all running after
    LTE_DEMO_CRASH=1 pytest demos/pytest/basic/test_tour.py -v   # a worker dies mid-test

What it shows:
  setup_class      start in stages: a server, a gate (any code), then workers
  ready=           wait for a line in a process's output before going on
  fail_on_output   a line that fails the test (a regex, or a function); Only()
                   for some processes
  env=             variables for every process of the group
  every test       checked after it ran: a death fails it, not a later one
  waits            each fails at once if something died:
    wait_for_line()       a line of its output, printed already or later;
                          a LineMatch: .line, m[1]. "*": any process
    mark(), since=        only lines after a mark: mark, act, wait
    since=a LineMatch     lines in order
    wait_for_condition()  any code, e.g. the server answers
  exits=           a process that finishes on its own: wait_for_exit()
  expect_exit()    an exit the test causes on purpose (kill()) is no death
  allowed_exit_codes  a process that exits 2 on SIGINT, stopped cleanly
  test_long_run    a long test calling check_health(), so a death fails it
                   within half a second, not at the end
  Processes()      in a test: a process just that test needs, stopped after it
  on_test_failure  bring things down your own way when a test fails
  teardown_class   close(): stop everything, fail unless each stopped cleanly
  logs             each process's output in a file: 'process logs:' at the end
"""

import os
import sys
import tempfile
import time
import urllib.request

from libtmux_easy.testing import Only, Processes, check_health

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
FAIL_ON = [
    r"Traceback",  # any process
    Only(lambda line: "w1 is sad" if "sad" in line else None, name="workers:w1"),
]


class TestTour:
    # pytest makes a new TestTour for every test: what the tests share goes
    # on the class (cls.procs), set once in setup_class.

    @classmethod
    def setup_class(cls):
        cls.procs = Processes(  # empty; logs in a tmp dir of its own
            cls,
            fail_on_output=FAIL_ON,
            env={"TOUR": "1"},  # for every process of the group
            on_test_failure=lambda: print("a test failed: saving state first"),
        )
        # Stage 1. A name is 'window:pane': in tmux, window 'servers', pane 'web'.
        # start() returns a handle: cls.web.wait_for_line(...), cls.web.stop().
        cls.web = cls.procs.start("servers:web", SERVER, ready=r"Serving HTTP")
        # Gate: any code. wait_for_condition() fails at once if something died.
        cls.procs.wait_for_condition(server_answers, timeout=10)
        # Stage 2.
        cls.w0, cls.w1 = (
            cls.procs.start(f"workers:w{n}", worker(f"w{n}"), ready=r"tick 0") for n in range(2)
        )

    @classmethod
    def teardown_class(cls):
        if os.path.exists(CRASH_FILE):
            os.remove(CRASH_FILE)
        cls.procs.close()

    def test_wait_for_line(self):
        # Any line since its start counts, even one printed before the call.
        m = self.web.wait_for_line(r"Serving HTTP on (\S+) port (\d+)")
        assert m[2] == str(PORT) and m.line.startswith("Serving HTTP")

    def test_after_a_mark(self):
        # Mark, act, wait: only what it printed after the mark counts, so
        # this is the reply to this request, not an earlier one.
        mark = self.web.mark()
        assert server_answers()
        self.web.wait_for_line(r'"GET / HTTP/1\.\d" 200', since=mark)

    def test_lines_in_order(self):
        # since= an earlier match: only later lines, so the order is checked.
        first = self.w0.wait_for_line(r"tick (\d+)")
        later = self.w0.wait_for_line(r"tick (\d+)", since=first)
        assert int(later[1]) > int(first[1])

    def test_wait_for_condition(self):
        # Anything that isn't a line: a reply, a file, a topic. Its result
        # comes back.
        assert self.procs.wait_for_condition(server_answers, timeout=5)

    def test_output_and_logs(self):
        self.w0.wait_for_line(r"tick 5")
        assert "w0 tick 0" in self.w0.output()
        assert "w0 tick 0" in open(self.w0.log()).read()

    def test_any_process(self):
        m = self.procs.wait_for_line("*", r"tick 3")  # a line from any process
        assert m.name in ("workers:w0", "workers:w1")  # which one
        assert self.procs["workers:w0"].name == self.w0.name  # a handle by name

    def test_one_shot(self):
        # exits=: it finishes on its own, so its exit is no death.
        job = self.procs.start("jobs:once", "echo TOUR=$TOUR", exits=0)
        status = job.wait_for_exit()
        assert status.exit_status.code == 0 and "TOUR=1" in job.output()

    def test_kill_on_purpose(self):
        # A kill the test causes: no death inside expect_exit().
        with self.procs.expect_exit("workers:w0"):
            self.w0.kill()
        assert self.w0.status().exit_status.signal == 9
        self.w0.start()

    def test_allowed_exit_code(self):
        # It exits 2 on SIGINT: allowed, so stop() counts it as clean.
        cmd = "trap 'exit 2' INT; echo up; while :; do sleep 0.1; done"
        tool = self.procs.start("tools:quirky", cmd, ready="up", allowed_exit_codes=2)
        assert tool.stop().exit_status.code == 2

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
