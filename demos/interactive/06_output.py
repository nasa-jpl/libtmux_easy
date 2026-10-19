#!/usr/bin/env python3
"""Tour 6: output -- capture panes to files, search them, react to lines.

tmux pipes a pane's output to a file (pipe-pane); everything reads the file.
Watch a pane for a pattern, wait for a ready line, capture a window and
grep it later, catch the answer to something you send, let lte-run --tee
start the pipe, and watch the whole session, panes made by hand included.
"""

import os
import tempfile
import time

from common import BOLD, HERE, RESET, Tour
from common import lte_run, runs_forever


def server(*args):
    return " ".join(["python3", "-u", str(HERE / "workloads" / "fake_server.py"), *map(str, args)])


class Matches:
    """An on_output callback: each match goes to the 'matches' pane under
    home (a file it tails), or is printed here without a dashboard."""

    def __init__(self):
        self.path = None

    def __call__(self, match):
        line = f"{match.name or match.pane_id}: {match.line}"
        if self.path is None:
            print(f"\n  {BOLD}match:{RESET} {line}")
            return
        with open(self.path, "a") as f:
            print(line, file=f)


def files(directory):
    for name in sorted(os.listdir(directory)):
        size = os.path.getsize(os.path.join(directory, name))
        print(f"    {name:<24} {size:>7} bytes")


def main():
    with Tour("output", __doc__, echo=True) as tour:
        s = tour.session
        keep = tempfile.mkdtemp(prefix="lte_demo_output_")
        show = Matches()
        if tour.has_dash:
            show.path = tempfile.mkstemp(prefix="lte_demo_matches_", suffix=".log")[1]
            s.dashboard_pane(f"tail -n +1 -f {show.path}", title="matches", size="30%")

        tour.step("React to a line")
        tour.say("""
            new_pane(on_output={pattern: callback}) pipes the pane from its first
            byte and calls back on each matching line. Matching is on plain
            text: the server's colors don't get in the way. Then
            wait_for_output() blocks for the ready line and hands back the
            regex match, here the port.
        """)
        tour.pause("start a server that fails every 7th request")
        s.new_window("web")
        web = s.new_pane("server", server("--port", 8080), mode="exec",
                         on_output={r"\[ERROR\]": show})
        m = s.wait_for_output(web, r"Serving HTTP on \S+ port (\d+)", timeout=10)
        print(f"  ready: port {m.match.group(1)}  ({m.line!r})")
        tour.say("""
            The callback writes each match to the matches pane, under this one
            (dashboard_pane(): it stays put while you browse). Wait for three
            new errors (since="now": earlier lines don't count), then
            unwatch_output() stops the watch; the server keeps running.
        """)
        for _ in range(3):
            s.wait_for_output(web, r"\[ERROR\]", timeout=30, since="now")
        s.unwatch_output(web)
        tour.say("""
            That pane is only watched: its file is a temp one, deleted with
            the session.
        """)
        tour.pause("go on")

        tour.step("Capture now, search later")
        tour.say(f"""
            capture() saves output without watching it: no events, no reading,
            just a file per pane. new_window(capture=DIR) covers every pane made
            in the window, now or later. These files are kept:

                {keep}
        """)
        tour.pause("start two workers in a captured window")
        s.new_window("workers", capture=keep)
        s.new_pane("alpha", runs_forever("alpha"), mode="wrap")
        s.new_pane("beta", runs_forever("beta"), mode="wrap")
        time.sleep(3.5)
        files(keep)
        tour.pause("search them")
        for hit in s.search_output("workers", r" 2$"):
            print(f"  {hit.name}: {hit.line!r} at byte {hit.offset}")
        tour.say("search_output() scans a pane's, window's or the session's files once.")

        tour.step("Catch the answer to something you send")
        tour.say("""
            expect() arms a watch on entry and waits at exit, so a fast answer
            can't slip past. mark() and since= search only what came after a
            point: earlier matches don't count.
        """)
        tour.pause("send a command to a shell pane and catch its answer")
        s.new_window("shell")
        sh = s.new_pane("bash", "bash --norc", mode="exec")
        cap = s.capture(sh, os.path.join(keep, "shell.log"))
        s.send_keys("echo pong 1", target_pane=sh)
        time.sleep(0.5)
        mark = cap.mark()
        with s.expect(sh, r"pong (\d+)", timeout=5) as e:
            s.send_keys("echo pong 2", target_pane=sh)
        print(f"  expect: {e.match.line!r}")
        since = cap.search("pong", since=mark)
        every = cap.search("pong")
        print(f"  since the mark: {[h.line for h in since]}")
        print(f"  whole capture:  {[h.line for h in every]}")

        tour.step("Let lte-run start the pipe")
        tee = os.path.join(keep, "job.log")
        tour.say(f"""
            A pane with no capture of its own. Typed into it:

                lte-run --tee {tee} -- <job>

            lte-run pipes the pane for the run (plain text) and closes the pipe
            when the job ends. The library finds the file by itself: a watch
            or search later just works. A rerun overwrites the file; -a appends.
            expect() arms its watch before the job is typed, and lte-run takes
            that watch's pipe over: no line slips past.
        """)
        tour.pause("run a job twice: once plain, once with -a")
        s.new_window("jobs")
        job = s.new_pane("job", "bash --norc", mode="shell")
        with s.expect(job, "job done", timeout=10):
            s.send_keys(f"{lte_run()} --tee {tee} -- sh -c 'echo job run 1; sleep 1; echo job done'",
                        target_pane=job)
        path = s.output_path(job)
        print(f"  file: {os.path.basename(path)}")
        with s.expect(job, "job run 2", timeout=10):
            s.send_keys(f"{lte_run()} --tee {tee} -a -- sh -c 'echo job run 2'", target_pane=job)
        time.sleep(0.5)
        text = s.output_text(job)
        print("  the file now:", text.splitlines())

        tour.step("Watch the whole session")
        tour.say("""
            watch_output(None, pattern) covers every pane in the session, the
            ones made later too, by the library or by hand: tmux's own hooks
            pipe a new pane from its first byte.
        """)
        tour.pause("watch for 'hello' everywhere, then make a pane by hand")
        hello = s.watch_output(None, r"\bhello\b", show)
        if not tour.auto:
            tour.say("""
                Make one yourself: select jobs:job, Enter, then prefix % (or
                prefix ") and type 'echo hello'. Or wait: one is made for you in 15 s.
            """)
        if hello.wait(timeout=1 if tour.auto else 15) is None:
            s.server.cmd("split-window", "-d", "-t", f"{s.id}:jobs", "echo hello, made for you; sleep 30")
            hello.wait(timeout=5)
        s.unwatch_output(None)
        print(f"  {hello.count} match, then unwatch_output(None): the session watch is off.")
        tour.say(f"""
            Kept after the tour (you chose to capture them): {keep}
            Deleted with the session (only watched): the server's file.
            Set session.keep_output = True to keep those too.
        """)


if __name__ == "__main__":
    main()
