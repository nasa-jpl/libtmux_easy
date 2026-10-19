"""Processes a test needs, started and watched by the plugin.

    pytest demos/examples/pytest_plugin                    # tmux; --lte-mode=shell without it
    lte-session -- pytest -s demos/examples/pytest_plugin  # with the dashboard
"""
import sys
import urllib.request

PORT = 8768
PROCESSES = {"servers:web": f"{sys.executable} -u -m http.server {PORT} --bind 127.0.0.1"}
READY = {"servers:web": r"Serving HTTP"}  # waited for before the first test
FAIL_ON = {"*": r"Traceback"}             # a match anywhere fails the test


def test_get(processes):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/") as reply:
        assert reply.status == 200
    processes.wait_for("servers:web", r'"GET / HTTP/1\.\d" 200')
