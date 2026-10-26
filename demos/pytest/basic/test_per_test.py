"""Tests that bring their own processes up and down: Processes() in a test.

    pytest demos/pytest/basic/test_per_test.py
    pytest demos/pytest/basic/test_per_test.py --tmux
    LTE_DEMO_CRASH=1 pytest demos/pytest/basic/test_per_test.py  # a worker dies

The processes are stopped at the end of the 'with' (each must exit 0 on
SIGINT, or the test fails); a failed test's at once.
"""
import os
import sys
import time
import urllib.error
import urllib.request

import pytest

from libtmux_easy.testing import Processes

PORT = 9200 + os.getpid() % 500
WEB = f"{sys.executable} -u -m http.server {PORT} --bind 127.0.0.1"
CRASH = "sys.exit(3) if i == 3 else None; " if os.environ.get("LTE_DEMO_CRASH") else ""
WORKER = (f"{sys.executable} -u -c 'import sys, time\n"
          f"try:\n"
          f"    for i in range(10**6): {CRASH}print(\"tick\", i); time.sleep(0.3)\n"
          f"except KeyboardInterrupt: pass'")


def get(path="/"):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=2) as reply:
        return reply.status


def test_server_restart():
    with Processes() as procs:
        web = procs.start("servers:web", WEB, ready=r"Serving HTTP")  # a Process handle
        assert get() == 200
        web.stop()                            # fails unless it exits 0 on SIGINT
        with pytest.raises(urllib.error.URLError):
            get()
        web.start()                           # same command and ready=; fresh output
        assert get() == 200
        assert web.output().count("Serving HTTP") == 1


def test_workers():
    with Processes() as procs:
        w0, w1, w2 = (procs.start(f"workers:w{n}", WORKER, ready=r"tick 0") for n in range(3))
        w2.wait_for(r"tick 5")
        w0.stop()                             # the others keep going
        w1.wait_for(r"tick 8")
        time.sleep(0.5)                       # a death here fails the test at the 'with' end
