"""A tour of the plugin, no ROS: bring a system up once for the class,
run tests against it, shut it down.

    pytest demos/pytest/basic/test_tour.py -v               # subprocesses, output in logs
    pytest demos/pytest/basic/test_tour.py -v --tmux        # the dashboard; --no-attach: detached
    pytest demos/pytest/basic/test_tour.py -v --keep-open   # leave it all running after
    LTE_DEMO_CRASH=1 pytest demos/pytest/basic/test_tour.py -v   # a worker dies mid-test

What it shows:
  setup_class      start in stages: a server, a gate (any code), then workers
  ready=           wait for a line in a process's output before going on
  fail_on_output   a line that fails the test (a regex, or a function);
                   Only(..., name="workers:*"): every worker, even later ones
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
  stop("workers:*")  a glob: stop a set at once, then start() each again
  Processes()      in a test: a process just that test needs, stopped after it
  a failure        skips the rest of the class; the processes stay up
  teardown_class   optional: procs.failed says why a test failed; stop in
                   your own order. The plugin then stops the rest, and fails
                   unless each stopped cleanly (not after a failure)
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
CRASH_FILE = os.path.join(tempfile.gettempdir(), f"lte_tour_crash_{os.getpid()}")


HERE = os.path.dirname(os.path.abspath(__file__))


def worker(name):
    """Create a worker process with the given name"""
    return f"{PY} -u {HERE}/worker.py {name} {CRASH_FILE}"


def server_answers():
    try:
        return urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=1).status == 200
    except OSError:
        return False


def worker_error(line):
    """An output check: a line -> a problem message, or None: fine."""
    return f"a worker reported an error: {line}" if "ERROR" in line else None


class TestTour:
    # pytest makes a new TestTour for every test. What the tests share goes
    # on the class (e.g. cls.procs). Set once in setup_class.
    @classmethod
    def setup_class(cls):

        # Create the Processes group object. This is used for creating and
        # tracking processes we want to test against. We can set global test
        # settings for all processes created in this group. E.g.
        # fail_on_output rules that can be applied to all processes, or a
        # selected subset.
        cls.procs = Processes(
            cls,
            fail_on_output=[
                r"Traceback",  # any process
                # every worker, now or later
                Only(worker_error, name="workers:*"),
            ],
            env={"TOUR": "1"},  # env variables for every process of the group
        )

        # Create a process. A name is 'window:pane' in tmux. start() returns a
        # handle that can be used directly (e.g. cls.web.stop()) or we can
        # look up a process by name (e.g. cls.procs.stop('servers:web'))
        cls.web = cls.procs.start(
            name="servers:web",
            cmd=f"{PY} -u -m http.server {PORT} --bind 127.0.0.1",
            ready=r"Serving HTTP",
        )

        # Stop and wait for a condition to be true. Fails at once if
        # something died.
        cls.procs.wait_for_condition(server_answers, timeout=10)

        # Create worker processes
        cls.w0, cls.w1 = (
            cls.procs.start(name=f"workers:w{n}", cmd=worker(f"w{n}"), ready=r"tick 0")
            for n in range(2)
        )

    # Note - Nominally in pytest, tests run in the order they appear

    def test_wait_for_line(self):
        # Wait for a line to appear, and then check contents of that line.
        # NOTE - In this approach, any line since process start counts, even
        # one printed before the call.
        m = self.web.wait_for_line(r"Serving HTTP on (\S+) port (\d+)", timeout=5)
        assert m[2] == str(PORT) and m.line.startswith("Serving HTTP")

    def test_after_a_mark(self):
        # Wait for a line again. Except this time we only look for lines
        # that occur after our mark. So this is the reply to this request,
        # not an earlier one.
        mark = self.web.mark()
        assert server_answers()
        self.web.wait_for_line(r'"GET / HTTP/1\.\d" 200', since=mark)

    def test_lines_in_order(self):
        # Another example. We wait for a line (first), which could have
        # occurred anytime. Then we wait for the same line again (later),
        # but it has to be after the first.
        first = self.w0.wait_for_line(r"tick (\d+)")
        later = self.w0.wait_for_line(r"tick (\d+)", since=first)
        assert int(later[1]) > int(first[1])

    def test_wait_for_condition(self):
        # Waiting on a condition again. Assert the condition was met in the
        # required time. Use for checking things besides process
        # stdout/stderr. E.g. a reply, a file to exist, a topic.
        assert self.procs.wait_for_condition(server_answers, timeout=5)

    def test_output_and_logs(self):
        # Read everything a process printed. output() is what it printed
        # since its latest start, as one string. log() is the path of its log
        # file on disk. Wait for a few ticks first, so there is output.
        self.w0.wait_for_line(r"tick 5")
        assert "w0 tick 0" in self.w0.output()
        assert "w0 tick 0" in open(self.w0.log()).read()

    def test_any_process(self):
        # Wait for a line from any process in the group. The first argument
        # is a glob on the process name, so "*" is every process. The match
        # says which process printed it. We can also look up a handle by name
        # (e.g. self.procs["workers:w0"]).
        m = self.procs.wait_for_line("*", r"tick 3")
        assert m.name in ("workers:w0", "workers:w1")
        assert self.procs["workers:w0"].name == self.w0.name

    def test_one_shot(self):
        # Start a process that finishes on its own. exits=0 says it may exit
        # with code 0 without that counting as a death. wait_for_exit() waits
        # for it to end and returns its status. Its output also shows the
        # group's env= reached it (TOUR=1).
        job = self.procs.start("jobs:once", "echo TOUR=$TOUR", exits=0)
        status = job.wait_for_exit()
        assert status.exit_status.code == 0 and "TOUR=1" in job.output()

    def test_kill_on_purpose(self):
        # Kill a process on purpose. Inside expect_exit(), its exit is not a
        # death, so the test doesn't fail. Then check how it ended (SIGKILL
        # is signal 9), and start it again with the same command, so the
        # tests after this one still have it.
        with self.procs.expect_exit("workers:w0"):
            self.w0.kill()
        assert self.w0.status().exit_status.signal == 9
        self.w0.start()

    def test_allowed_exit_code(self):
        # By default, stop() sends Ctrl-C (SIGINT) and fails unless the
        # process exits 0. This one exits 2 on SIGINT. allowed_exit_codes=2
        # tells the plugin that is a clean stop too.
        cmd = "trap 'exit 2' INT; echo up; while :; do sleep 0.1; done"
        tool = self.procs.start("tools:quirky", cmd, ready="up", allowed_exit_codes=2)
        assert tool.stop().exit_status.code == 2

    def test_stop_and_restart(self):
        # Stop a process, then start it again. stop() fails unless it exits
        # cleanly on SIGINT, and returns its status. start() with no
        # arguments reruns the same command (start(cmd=...) takes a new one).
        # Its output starts fresh.
        status = self.w1.stop()
        assert status.exit_status.ok
        self.w1.start()

    def test_restart_a_set(self):
        # Stop a set of processes at once with a glob on their names. stop()
        # returns {name: status} for every match, so we can start each one
        # again. Then wait for a worker to be up before moving on.
        for name in self.procs.stop("workers:*"):
            self.procs[name].start()
        self.w0.wait_for_line(r"tick 0")

    def test_a_process_of_its_own(self):
        # Create a second Processes group, for this test only. Its processes
        # are stopped and checked at the end of the 'with'. The class's
        # processes keep running. NOTE - Names are unique within the class,
        # so "workers:w0" here would be an error.
        with Processes() as procs:
            procs.start("tools:helper", worker("helper"), ready=r"tick 0")

    def test_long_run(self):
        # A long test. Without help, a process that dies mid-test is only
        # noticed when the test ends. Calling check_health() in the loop
        # fails the test within half a second of a death. With
        # LTE_DEMO_CRASH=1, the workers die at 3 s to show this.
        start = time.monotonic()
        while time.monotonic() - start < 10:
            if os.environ.get("LTE_DEMO_CRASH") and time.monotonic() - start > 3:
                open(CRASH_FILE, "w").close()
            check_health()  # or self.procs.check()
            time.sleep(0.5)

    @classmethod
    def teardown_class(cls):
        # teardown_class is optional. The pytest plugin automatically calls
        # cls.procs.stop_all() after this function, which will stop all
        # processes and check their exit codes. Use teardown_class to inspect
        # after failure, do a controlled bringdown, etc.

        # After a failure everything is still up. We can look around.
        if cls.procs.failed:
            up = [n for n in cls.procs.names if cls.procs.status(n).running]
            print(f"{cls.procs.failed}; still running: {', '.join(up)}")
        if os.path.exists(CRASH_FILE):
            os.remove(CRASH_FILE)
        cls.procs.stop("workers:*")  # bringdown order: the workers first
        # The plugin stops the rest (the server) after this, checked.
