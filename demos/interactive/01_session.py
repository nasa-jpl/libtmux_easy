#!/usr/bin/env python3
"""Tour 1: build a tmux session from Python, then attach and look around.

Windows, panes, layouts, names, send_keys(), print() and print_status().
This one runs in your terminal and attaches at the end; the other tours run
inside the dashboard.
"""

import argparse
import os
import subprocess
import sys
import textwrap
import time

import common
from common import BOLD, ECHO_HOW_TO, RESET, Console, Echo, exits_after, runs_forever
from libtmux_easy import session_exists

NAME = "lte_demo_session"

echo = Echo(__file__)
TmuxSession = echo.wrap(common.TmuxSession)  # its calls, and the session's, print as >>> lines


def say(text):
    print(textwrap.indent(textwrap.dedent(text).strip("\n"), "  "))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--auto", action="store_true", help="no prompts, don't attach (smoke test)")
    args = parser.parse_args()
    console = Console(echo)

    def pause(action):
        if not args.auto:
            console.pause(action, sys._getframe(1))  # main's variables at the prompt

    print(f"{BOLD}{__doc__.strip().splitlines()[0]}{RESET}\n")
    say(ECHO_HOW_TO)

    pause("create the session and a 'work' window")
    session = TmuxSession.create(NAME)
    session.new_window("work")                                       # panes go in the current window
    session.new_pane("shell", "echo 'a plain shell: type away'")     # reuses the window's first pane
    session.new_pane("counter", runs_forever("tick"))                # later panes split it
    session.new_pane("short", exits_after(10, code=3), mode="wrap")  # exits 3 in 10 s

    pause("add a 'monitor' window with another layout")
    session.new_window("monitor", layout="main-horizontal")
    session.new_pane("vmstat", "vmstat 2")
    session.new_pane("files", "ls -la", mode="wrap")
    session.send_keys("echo 'sent later, by name'", target_pane="monitor:files")
    session.select_first()

    pause("print the layout and each pane's status")
    time.sleep(1)
    session.print()
    session.print_status()

    if args.auto:
        session.stop_all(sigint_timeout=2)
        session.kill_session()
        return

    say("""

        Panes are named window:pane ("work:counter"); each border shows its status.
        Inside tmux: prefix n / p switch windows, prefix o moves between panes,
        prefix d detaches (prefix is Ctrl-b unless you changed it).
        Watch "short" turn [exited 3] about 10 s in.""")
    pause("attach")
    if os.environ.get("TMUX"):  # already in tmux: switch rather than nest
        subprocess.run(["tmux", "switch-client", "-t", NAME])
        input(f"  Switched to {NAME}. Come back here (prefix s, or prefix L) and press Enter. ")
    else:
        session.attach()

    print("\n  After you left:")
    session.print_status()
    if not session_exists(NAME):
        say("""

            The session is gone (closed from inside: lte-close, prefix C-x, or the
            last pane exited). Above: how each pane ended, as last seen.""")
        return
    try:
        pause("stop everything and kill the session (Ctrl-C keeps it)")
    except KeyboardInterrupt:
        print(f"\n  kept: tmux attach -t {NAME}")
        return
    session.stop_all(sigint_timeout=2)
    session.kill_session()


if __name__ == "__main__":
    main()
