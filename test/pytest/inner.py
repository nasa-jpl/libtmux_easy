"""Shared by the plugin tests: the modes an inner pytest runs in, a test process."""
MODES = {"subprocess": [], "tmux": ["--tmux", "--no-attach"]}

# Prints, then runs until SIGINT and exits 0 -- or with 'die', exits 3 first.
PROC = """
import sys, time
try:
    print("hello_" + str(6 * 7), flush=True)
    time.sleep(0.5 if "die" in sys.argv else 60)
except KeyboardInterrupt:
    sys.exit(0)
sys.exit(3)
"""
