"""The smallest test class: one server, up for the class's tests.

    pytest demos/pytest/basic/test_web.py              # subprocesses
    pytest demos/pytest/basic/test_web.py --tmux       # the dashboard, attached
    pytest demos/pytest/basic/test_web.py --keep-open  # leave it running after
"""
import os
import sys
import urllib.error
import urllib.request

from libtmux_easy.testing import Processes

PORT = 8700 + os.getpid() % 500  # two runs at once don't share a port
SERVER = f"{sys.executable} -u -m http.server {PORT} --bind 127.0.0.1"


class TestWeb:
    @classmethod
    def setup_class(cls):
        cls.procs = Processes(cls, fail_on={"*": r"Traceback"})  # a match fails the test
        cls.web = cls.procs.start("servers:web", SERVER, ready=r"Serving HTTP")

    @classmethod
    def teardown_class(cls):
        cls.procs.close()  # fails unless it exits 0 on SIGINT

    def test_get(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/") as reply:
            assert reply.status == 200
        self.web.wait_for(r'"GET / HTTP/1\.\d" 200')

    def test_not_found(self):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/nope")
        except urllib.error.HTTPError as err:
            assert err.code == 404
        self.web.wait_for(r'"GET /nope HTTP/1\.\d" 404')
        print("server log:", self.web.log())
