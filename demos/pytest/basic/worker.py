"""A stand-in for a long-running process: prints '<name> tick N' every
0.2 s, exits 0 on Ctrl-C (SIGINT), and exits 3 once CRASH_FILE exists.

    worker.py NAME [CRASH_FILE]
"""
import os
import sys
import time

name = sys.argv[1]
crash_file = sys.argv[2] if len(sys.argv) > 2 else None
try:
    for i in range(10**6):
        if crash_file and os.path.exists(crash_file):
            print(f"{name}: crashing", flush=True)
            sys.exit(3)
        print(f"{name} tick {i}", flush=True)
        time.sleep(0.2)
except KeyboardInterrupt:
    pass
