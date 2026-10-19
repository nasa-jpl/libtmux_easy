#!/usr/bin/env python3
"""Exit status in each pane mode: 'exit 130' and a SIGINT death look the same to a shell"""

from libtmux_easy import TmuxSession

session = TmuxSession.create("lte_example_exit_status")
session.new_window("modes")

cases = [
    ("shell", "sh -c 'exit 130'"),
    ("shell", "sh -c 'kill -INT $$'"),
    ("wrap", "sh -c 'exit 130'"),       # lte-run reports the real status
    ("wrap", "sh -c 'kill -INT $$'"),
    ("exec", "sh -c 'exit 3'"),         # the command is the pane's process: tmux records it
]
panes = [(mode, cmd, session.new_pane(f"p{i}", cmd, mode=mode))
         for i, (mode, cmd) in enumerate(cases)]

for mode, cmd, pane in panes:
    status = session.wait_for_exit(pane, timeout=10)  # an ExitStatus: .code, .signal, .inferred
    print(f"{mode:5}  {cmd:22}  {status}")

session.kill_session()
