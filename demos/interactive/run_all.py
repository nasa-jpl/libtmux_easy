#!/usr/bin/env python3
"""Run every tour unattended (--auto) and report which ones finished.

    ./run_all.py                 # all tours
    ./run_all.py --only exit     # tours whose filename contains 'exit'
    ./run_all.py --verbose       # show each tour's output

A smoke test, not a test suite: the tours print, they don't assert. Don't run
it while a tour is open (they share session names).
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOURS = sorted(p.name for p in HERE.glob("[0-9][0-9]_*.py"))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="only tours whose filename contains this")
    parser.add_argument("--verbose", action="store_true", help="show each tour's output")
    args = parser.parse_args()

    tours = [t for t in TOURS if not args.only or args.only in t]
    if not tours:
        sys.exit(f"no tours match {args.only!r}; available: {', '.join(TOURS)}")

    failed = []
    for name in tours:
        start = time.monotonic()
        proc = subprocess.run([sys.executable, str(HERE / name), "--auto"], cwd=HERE,
                              capture_output=not args.verbose, text=True, timeout=300)
        ok = proc.returncode == 0
        print(f"  {'ok  ' if ok else 'FAIL'}  {name:<26} {time.monotonic() - start:4.0f}s",
              flush=True)
        if not ok:
            failed.append(name)
            if not args.verbose:
                print("\n".join(f"        {line}" for line in
                                (proc.stdout + proc.stderr).splitlines()[-30:]))
    print(f"{len(tours) - len(failed)}/{len(tours)} tours finished")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
