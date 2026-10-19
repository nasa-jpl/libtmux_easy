#!/usr/bin/env python3
"""Pane output: react to a line, wait for one, catch an answer, search a capture"""

import tempfile

from libtmux_easy import TmuxSession

logs = tempfile.mkdtemp(prefix="lte_example_output_")
session = TmuxSession.create("lte_example_output", mode="wrap")
session.new_window("app", capture=logs)  # every pane in it saved to a file

# A callback for each matching line, from the pane's first byte.
server = session.new_pane("server", "python3 -u -m http.server 8767",
                          on_output={r'"GET ': lambda m: print("  request:", m.line)})

# Block until a line matches; what was printed already counts.
m = session.wait_for_output(server, r"port (\d+)", timeout=10)
print("serving on port", m.match.group(1))

# expect(): watch first, act, then wait, so a fast answer isn't missed.
client = session.new_pane("client", "echo ready", mode="shell")
with session.expect(client, r"<title>(.*)</title>") as e:
    session.send_keys("curl -s localhost:8767/", target_pane=client)
print("the client got:", e.match.match.group(1))

# Later: search the saved files (here, once the server has logged the request).
session.wait_for_output(server, r'"GET / HTTP', timeout=5)
print("saved in", logs)
for hit in session.search_output("app", r'"GET / HTTP'):
    print(f"  {hit.name}: {hit.line}")

session.stop_all()
session.kill_session()
