#!/usr/bin/env python3
"""Tour 3: process control -- exit events, stopping, rerunning.

on_exit callbacks, wait_for_exit(), stop_pane()'s SIGINT -> SIGTERM -> SIGKILL
escalation, stop_all() and respawn_pane().
"""

import time

from common import PANE_MODE_WRAP, Tour, exits_after, ignores_sigint, \
    ignores_sigint_and_sigterm, runs_forever


def main():
    with Tour("process_control", __doc__) as tour:
        s = tour.session

        def pane(name, cmd):
            return s.new_pane(name, cmd, mode=PANE_MODE_WRAP, on_exit=tour.event)

        tour.step("Exit events")
        tour.say("""
            new_pane(..., on_exit=callback) calls back the moment the command ends,
            for any reason. Here the callback prints an "event:" line.
        """)
        tour.pause("start a worker that crashes in 5 s")
        s.new_window("events")
        pane("crashy", exits_after(5, code=3))
        tour.say("Waiting for it (wait_for_exit) ...")
        s.wait_for_exit("events:crashy", timeout=30)

        tour.step("A Ctrl-C typed by a person is an exit too")
        victim = pane("interrupt_me", runs_forever())
        tour.say("""
            Select events:interrupt_me in the sidebar, Enter to focus it, and press
            Ctrl-C. Then come back to home: the event is printed here.
        """)
        if tour.auto:
            time.sleep(1)
            victim.send_keys("C-c", enter=False)
        tour.say("Waiting for you to press Ctrl-C there ...")
        s.wait_for_exit(victim)
        tour.pause("continue")

        tour.step("stop_pane() escalates as far as it has to")
        tour.say("""
            SIGINT first (like Ctrl-C), then SIGTERM, then SIGKILL, each after a
            timeout (2 s here). Escalating past SIGINT means the process didn't
            shut down cleanly; its status says how it was stopped (yellow).
        """)
        tour.pause("start three: polite, stubborn (ignores SIGINT), immortal (ignores both)")
        s.new_window("stop")
        workers = [pane("polite", runs_forever()), pane("stubborn", ignores_sigint()),
                   pane("immortal", ignores_sigint_and_sigterm())]
        tour.pause("stop them one by one with stop_pane()")
        for p in workers:
            print(f"\n  stop_pane({s.get_pane_name(p)!r})")
            s.stop_pane(p, sigint_timeout=2, sigterm_timeout=2)
            time.sleep(0.2)
        tour.status()

        tour.step("respawn_pane() reruns in place")
        tour.pause("rerun stop:polite")
        s.respawn_pane(workers[0])
        tour.say("stop:polite is green again: same pane, new run.")

        tour.step("stop_all(): every running pane, in parallel")
        tour.pause("start four more workers")
        s.new_window("fleet")
        for i in range(4):
            pane(f"worker{i}", runs_forever(f"worker{i}"))
        tour.pause("stop_all()")
        start = time.monotonic()
        results = s.stop_all(sigint_timeout=2)
        print(f"\n  stopped {len(results)} panes in {time.monotonic() - start:.1f} s")
        tour.status()


if __name__ == "__main__":
    main()
