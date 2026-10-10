#!/usr/bin/env python3
"""Demo 1: the basics -- windows, panes, layouts, names and status.

Run with --attach to see the resulting layout for yourself.
"""

from common import check, demo, note, parse_args, settle, step

# Long-lived single processes. Each is one process that keeps running, which
# is what the library records as the pane's target -- see the note in the
# demos README about shell loops ('while true; do ...; done') not having a
# stable process to track.
CLOCK = "sh -c 'exec watch -n 1 date'"
PINGER = "sh -c 'exec ping -i 1 127.0.0.1'"
STATS = "sh -c 'exec vmstat 2'"


def main():
    args = parse_args(__doc__)

    with demo("layout", "Demo 1: windows, panes and layouts", args) as session:

        step("Panes are created in the current window, and named")
        session.new_pane("clock", CLOCK)
        session.new_pane("pinger", PINGER)
        note("the first new_pane() after new_window() reuses the window's")
        note("initial pane; later calls split it")

        demo_window = session.get_window("demo")
        check("panes in 'demo' window", len(demo_window.panes), 3)
        check("lookup by qualified name", session.get_pane("demo:clock") is not None, True)
        check("unqualified name is not a pane key", session.get_pane("clock"), None)

        step("Pane names only have to be unique within their window")
        second = session.new_window("second", layout="main-horizontal")
        session.new_pane("clock", STATS)
        session.new_pane("files", "ls -la")
        check("'clock' resolves differently per window",
              session.get_pane("demo:clock") is not session.get_pane("second:clock"), True)
        check("window count", len(session.windows), 2)

        step("Duplicate names are rejected within a window")
        try:
            session.new_pane("files", "echo nope")
            check("duplicate pane name raises", False, True)
        except ValueError:
            check("duplicate pane name raises", True, True)

        step("Layouts are applied per window")
        note("layout_str lives on the Window object new_window() returned")
        check("second window layout", second.layout_str, "main-horizontal")

        step("send_keys() can target a pane by name after the fact")
        session.send_keys("echo 'sent after the fact'", target_pane="second:files")

        settle(1.5)

        step("session.print() shows the layout and what was run where")
        session.print()

        step("session.print_status() shows what is actually still running")
        note("the three long-lived panes are running; 'files' ran 'ls' and finished")
        session.print_status()

        check("clock still running", session.is_running("demo:clock"), True)
        check("files has finished", session.is_running("second:files"), False)


if __name__ == "__main__":
    main()
