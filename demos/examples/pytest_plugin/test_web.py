"""Processes a test module needs, started once and watched by the plugin.

    pytest demos/examples/pytest_plugin/test_web.py              # subprocesses
    pytest demos/examples/pytest_plugin/test_web.py --tmux       # in tmux: attach to watch
    pytest demos/examples/pytest_plugin/test_web.py --keep-open  # leave them running after
    lte-session -- pytest -s demos/examples/pytest_plugin        # with the dashboard
"""
import os
import sys
import urllib.request

PORT = 8700 + os.getpid() % 500  # two runs at once don't share a port
PROCESSES = {"servers:web": f"{sys.executable} -u -m http.server {PORT} --bind 127.0.0.1"}
READY = {"servers:web": r"Serving HTTP"}  # waited for before the first test
FAIL_ON = {"*": r"Traceback"}             # a match anywhere fails the test


def test_get(processes):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/") as reply:
        assert reply.status == 200
    processes.wait_for("servers:web", r'"GET / HTTP/1\.\d" 200')


def test_not_found(processes):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/nope")
    except urllib.error.HTTPError as err:
        assert err.code == 404
    processes.wait_for("servers:web", r'"GET /nope HTTP/1\.\d" 404')
    print("server log:", processes.log("servers:web"))
