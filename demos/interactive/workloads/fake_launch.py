#!/usr/bin/env python3
"""A stand-in for 'ros2 launch', for demos that need its awkward behavior
without needing ROS installed.

Faithfully reproduces the three properties that make a real launch process
hard to monitor (all measured against ros2 jazzy):

  1. When a supervised child dies, it logs an error and *keeps running*.
  2. It logs that death in launch's exact format, which is the only
     machine-readable record of the child's exit code:
         [ERROR] [crasher-1]: process has died [pid 7005, exit code 3, cmd '...'].
  3. On SIGINT it shuts its children down and exits **0** -- the same exit
     code it would give if nothing had gone wrong. So the supervisor's own
     exit status tells you nothing about whether a child failed.

Usage:
    fake_launch.py [--crash-after SECONDS] [--crash-code N] [--children N]

Pass --crash-after 0 to have no child crash at all (the healthy case).
"""

import argparse
import signal
import subprocess
import sys
import time

SHUTTING_DOWN = False


def log(level, name, message):
    print(f"[{level}] [{name}]: {message}", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--crash-after", type=float, default=3.0,
                        help="seconds before one child dies; 0 disables the crash")
    parser.add_argument("--crash-code", type=int, default=3,
                        help="exit code for the dying child")
    parser.add_argument("--children", type=int, default=2,
                        help="number of healthy children to supervise")
    args = parser.parse_args()

    print("[INFO] [launch]: All log files can be found below /tmp/fake_launch", flush=True)
    print("[INFO] [launch]: Default logging verbosity is set to INFO", flush=True)

    children = []  # (name, Popen, cmd string)

    for i in range(args.children):
        cmd = "exec sleep 86400"
        proc = subprocess.Popen(["sh", "-c", cmd])
        name = f"survivor-{i + 1}"
        children.append((name, proc, cmd))
        log("INFO", name, f"process started with pid [{proc.pid}]")

    crasher = None
    if args.crash_after > 0:
        cmd = f"sleep {args.crash_after}; exit {args.crash_code}"
        proc = subprocess.Popen(["sh", "-c", cmd])
        crasher = (f"crasher-{args.children + 1}", proc, cmd)
        children.append(crasher)
        log("INFO", crasher[0], f"process started with pid [{proc.pid}]")

    def shutdown(_signum, _frame):
        # Mirrors launch's SIGINT behavior: tear the children down, then exit
        # 0 regardless of whether any of them had already failed.
        global SHUTTING_DOWN
        SHUTTING_DOWN = True

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    reported = set()
    while not SHUTTING_DOWN:
        for name, proc, cmd in children:
            code = proc.poll()
            if code is None or name in reported:
                continue
            reported.add(name)
            # The line every log scanner keys off. Note it is the ONLY place
            # the child's exit code is recorded -- and that this supervisor
            # deliberately carries on afterwards.
            log("ERROR", name,
                f"process has died [pid {proc.pid}, exit code {code}, cmd '{cmd}'].")
        time.sleep(0.1)

    print("[INFO] [launch]: shutdown requested, exiting", flush=True)
    for _name, proc, _cmd in children:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
    for _name, proc, _cmd in children:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    # Exit 0 even though a child may have failed. This is the behavior that
    # makes the supervisor's own exit status useless as a health signal.
    sys.exit(0)


if __name__ == "__main__":
    main()
