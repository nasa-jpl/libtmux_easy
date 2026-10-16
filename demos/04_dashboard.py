#!/usr/bin/env python3
"""Tour 4: the dashboard -- browse, focus, stop and rerun panes from the sidebar.

Needs Textual: in a venv, 'pip install textual', or pipx install
'libtmux-easy[dash]'. Without it, the tour says so and ends.
"""

import importlib.util

from common import PANE_MODE_WRAP, Tour, exits_after, runs_forever

KEYS = """
    Up / Down  browse; the viewport follows
    Enter      focus the selected pane (type into it); prefix g or a click returns
    g          go to the pane in its own window; prefix g comes back
    x / r      stop / rerun the selected pane
    X / Q      stop all / stop all and close the tour's windows (press twice)
    [ ]        resize the sidebar
    ?          help
"""


def main():
    with Tour("dashboard", __doc__) as tour:
        s = tour.session
        if tour.auto and importlib.util.find_spec("textual"):
            s.dashboard()  # under lte-session it is already open
        elif not tour.has_dash:
            tour.say(__doc__.split("\n\n", 1)[1])
            return

        def pane(name, cmd):
            return s.new_pane(name, cmd, mode=PANE_MODE_WRAP, on_exit=tour.event)

        tour.step("What you're looking at")
        tour.say("""
            The dashboard is a tmux window: the sidebar (lte-dash) on the left, and on
            the right whichever pane you select -- the real pane, swapped in with
            'tmux swap-pane', so it is fully interactive. Every other window keeps
            its layout. A script opens one with session.dashboard(); this tour runs
            under lte-session, which opened it for you.
        """)
        tour.pause("start some panes in two windows")
        s.new_window("sim")
        pane("vmstat", "vmstat 2")
        pane("planner", runs_forever("planning"))
        pane("crashy", exits_after(15, code=3))
        s.new_window("tools")
        pane("shell", "echo 'an ordinary shell: try typing here'")
        pane("build", exits_after(5, code=0))

        tour.step("Browse")
        tour.say(KEYS)
        tour.say("""
            Try: arrows through the panes, Enter on tools:shell and type something,
            g to see a pane in its own window (prefix g back), prefix z to zoom.
            The summary at the top counts running, stopped and failed panes.
        """)
        tour.pause("continue (select home first)")

        tour.step("Actions")
        tour.say("""
            sim:crashy has failed by now (red). Select it and press r to rerun it.
            Select sim:planner and press x to stop it (yellow: stopped on purpose).
            Actions go to this script over its control socket, so they work while
            it runs. The events print here.
        """)
        tour.pause("continue")

        tour.step("A pane of your own beside the viewport")
        tour.pause("add one with dashboard_pane()")
        s.dashboard_pane("watch -t -n 2 uptime", title="load", size=4)
        tour.say('dashboard_pane("watch -t -n 2 uptime", title="load", size=4): under the viewport.')

        tour.step("Many panes")
        tour.pause("add 4 windows of 6 panes, more than the sidebar shows at once")
        for w in range(4):
            s.new_window(f"bulk{w}")
            for p in range(6):
                s.new_pane(f"p{p}", runs_forever(f"bulk{w}.p{p}"), mode=PANE_MODE_WRAP)
        tour.say("""
            Scroll with the arrows. X stops them all (press twice). The tour's end stops
            them too.
        """)


if __name__ == "__main__":
    main()
