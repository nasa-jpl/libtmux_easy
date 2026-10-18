"""The pytest plugin (libtmux_easy.testing), from the repo root:

    PYTHONPATH=src python3 -m pytest demos/interactive/pytest_plugin --lte-mode=shell   # no tmux
    PYTHONPATH=src python3 -m pytest demos/interactive/pytest_plugin --lte-mode=tmux    # detached
    PYTHONPATH=src .venv/bin/python -m libtmux_easy.launch -- \\
        .venv/bin/python -m pytest -s demos/interactive/pytest_plugin                    # dashboard

LTE_DEMO_CRASH=1 makes the ticker die mid-test, which fails the test (and
in tmux mode keeps the session for 'tmux attach'). Under the dashboard,
LTE_PDB=1 drops into pdb in the last test, and LTE_ASK=1 waits for input.
"""
import os
import sys
import time
import urllib.request


PORT = 8765 + os.getpid() % 1000
CRASH = "sys.exit(3) if i == 4 else None; " if os.environ.get("LTE_DEMO_CRASH") else ""
TICKER = (f"{sys.executable} -u -c 'import sys, time\n"
          f"try:\n"
          f"    for i in range(10**6): {CRASH}print(\"tick\", i); time.sleep(0.5)\n"
          f"except KeyboardInterrupt: pass'")

PROCESSES = {
    "servers:web": f"{sys.executable} -m http.server {PORT} --bind 127.0.0.1",
    "workers:ticker": TICKER,
}


def wait_for(predicate, timeout=5.0, processes=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if processes is not None:
            processes.check()  # a dead process fails now, not at the timeout
        if predicate():
            return True
        time.sleep(0.1)
    raise AssertionError("timed out")


def test_web_server_answers(processes):
    def answers():
        try:
            return urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=1).status == 200
        except OSError:
            return False
    wait_for(answers, processes=processes)
    assert '"GET / HTTP' in processes.output("servers:web")


def test_ticker_keeps_ticking(processes):
    wait_for(lambda: "tick 6" in processes.output("workers:ticker"), timeout=10,
             processes=processes)


def test_a_session_of_your_own(tmux_session):
    """'tmux_session': a TmuxSession to fill yourself."""
    tmux_session.new_window("scratch")
    flaky = tmux_session.new_pane("flaky", "sh -c 'sleep 1; exit 3'", mode="wrap")
    if os.environ.get("LTE_ASK"):
        print("you typed:", input("say something: "))
    if os.environ.get("LTE_PDB"):
        breakpoint()  # try: p tmux_session.pane_status(flaky)
    assert tmux_session.wait_for_exit(flaky, timeout=10).code == 3
