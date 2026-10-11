#!/usr/bin/env python3
"""Run every demo in order and summarize which ones passed.

    ./run_all.py                 # all demos
    ./run_all.py --only modes    # just demos whose filename contains 'modes'
    ./run_all.py --verbose       # show each demo's own output

Each demo creates and tears down its own tmux session, so they can be run in
any order, but not concurrently with each other (they would fight over
session names).
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

DEMOS = [
    "demo_layout.py",
    "demo_modes.py",
    "demo_exit.py",
    "demo_interference.py",
    "demo_launch_like.py",
    "demo_monitor.py",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default=None,
                        help="only run demos whose filename contains this substring")
    parser.add_argument("--verbose", action="store_true",
                        help="show each demo's output instead of just the verdict")
    args = parser.parse_args()

    demos = [d for d in DEMOS if args.only is None or args.only in d]
    if not demos:
        sys.exit(f"no demos match {args.only!r}; available: {', '.join(DEMOS)}")

    results = []
    for name in demos:
        print(f"\n{'=' * 74}\nRUN  {name}\n{'=' * 74}", flush=True)
        start = time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(HERE / name)],
            cwd=HERE,
            capture_output=not args.verbose,
            text=True,
        )
        elapsed = time.monotonic() - start
        ok = proc.returncode == 0
        results.append((name, ok, elapsed))

        if args.verbose:
            pass  # already streamed
        elif ok:
            print(f"     passed ({elapsed:.0f}s)")
        else:
            print(f"     FAILED ({elapsed:.0f}s), exit code {proc.returncode}")
            print("     ---- output ----")
            for line in (proc.stdout or "").splitlines()[-40:]:
                print(f"     {line}")
            if proc.stderr:
                print("     ---- stderr ----")
                for line in proc.stderr.splitlines()[-20:]:
                    print(f"     {line}")

    print(f"\n{'=' * 74}")
    failed = [name for name, ok, _ in results if not ok]
    for name, ok, elapsed in results:
        print(f"  {'pass' if ok else 'FAIL'}  {name:<28} {elapsed:>5.0f}s")
    print(f"{'=' * 74}")
    print(f"{len(results) - len(failed)}/{len(results)} demos passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
