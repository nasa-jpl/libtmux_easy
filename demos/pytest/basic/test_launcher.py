"""Tests that bring their own processes up and down: the 'launcher' fixture.

    pytest demos/pytest/basic/test_launcher.py
    pytest demos/pytest/basic/test_launcher.py --tmux
    LTE_DEMO_CRASH=1 pytest demos/pytest/basic/test_launcher.py  # a worker dies

Each test's processes are stopped after it (a clean SIGINT exit, or the
test errors), and their logs go to the test's tmp_path.
"""
import os
import sys
import time
import urllib.error
import urllib.request

import pytest

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


def test_server_restart(launcher):
    launcher.start("servers:web", WEB, ready=r"Serving HTTP")
    assert get() == 200
    launcher.stop("servers:web")              # fails unless it exits 0 on SIGINT
    with pytest.raises(urllib.error.URLError):
        get()
    launcher.start("servers:web", WEB, ready=r"Serving HTTP")  # same name, fresh output
    assert get() == 200
    assert launcher.output("servers:web").count("Serving HTTP") == 1


def test_workers(launcher):
    for n in range(3):
        launcher.start(f"workers:w{n}", WORKER, ready=r"tick 0")
    launcher.wait_for("workers:w2", r"tick 5")
    launcher.stop("workers:w0")               # the others keep going
    launcher.wait_for("workers:w1", r"tick 8")
    time.sleep(0.5)                           # a death here fails the test afterwards
