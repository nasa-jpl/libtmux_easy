#!/usr/bin/env python3
"""Tour 1: build a tmux session from Python, then attach and look around.

Windows, panes, layouts, names, send_keys(), print() and print_status().
This one runs in your terminal and attaches at the end; the other tours run
inside the dashboard.
"""

import argparse
import os
import subprocess
import time

from common import BOLD, RESET, TmuxSession, exits_after, runs_forever

NAME = "lte_demo_session"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--auto", action="store_true", help="don't attach (smoke test)")
    args = parser.parse_args()

    print(f"{BOLD}{__doc__.strip().splitlines()[0]}{RESET}\n")
    print("  session = TmuxSession.create(NAME)")
    session = TmuxSession.create(NAME)

    print('  session.new_window("work")                        # panes go in the current window')
    session.new_window("work")
    print('  session.new_pane("shell", "echo a plain shell")    # reuses the first pane')
    session.new_pane("shell", "echo 'a plain shell: type away'")
    print('  session.new_pane("counter", ...)                   # later panes split it')
    session.new_pane("counter", runs_forever("tick"))
    print('  session.new_pane("short", ..., mode="wrap")        # exits 3 in 10 s')
    session.new_pane("short", exits_after(10, code=3), mode="wrap")

    print('  session.new_window("monitor", layout="main-horizontal")')
    session.new_window("monitor", layout="main-horizontal")
    print('  session.new_pane("vmstat", "vmstat 2"); session.new_pane("files", "ls -la")')
    session.new_pane("vmstat", "vmstat 2")
    session.new_pane("files", "ls -la", mode="wrap")
    print('  session.send_keys("echo sent later", target_pane="monitor:files")')
    session.send_keys("echo 'sent later, by name'", target_pane="monitor:files")
    session.select_first()
    time.sleep(1)

    print("\n  session.print():")
    session.print()
    print("\n  session.print_status():")
    session.print_status()

    if args.auto:
        session.stop_all(sigint_timeout=2)
        session.kill_session()
        return

    print("""
  Panes are named window:pane ("work:counter"); each border shows its status.
  Inside tmux: prefix n / p switch windows, prefix o moves between panes,
  prefix d detaches (prefix is Ctrl-b unless you changed it).
  Watch "short" turn [exited 3] about 10 s in.""")
    input(f"\n  {BOLD}[Enter]{RESET} attach ")
    if os.environ.get("TMUX"):  # already in tmux: switch rather than nest
        subprocess.run(["tmux", "switch-client", "-t", NAME])
        input(f"  Switched to {NAME}. Come back here (prefix s, or prefix L) and press Enter. ")
    else:
        session.attach()

    print("\n  After you left, session.print_status():")
    session.print_status()
    try:
        input(f"\n  {BOLD}[Enter]{RESET} stop everything and kill the session (Ctrl-C keeps it) ")
    except KeyboardInterrupt:
        print(f"\n  kept: tmux attach -t {NAME}")
        return
    session.stop_all(sigint_timeout=2)
    session.kill_session()


if __name__ == "__main__":
    main()
