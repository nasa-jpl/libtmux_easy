"""Shared scaffolding for the libtmux_easy demos.

Each demo is a standalone script. Run one with no arguments to see its
printed results; add --attach to drop into the tmux session afterwards and
poke at the panes yourself, or --keep to leave the session running without
attaching.

Nothing here requires libtmux_easy to be installed -- the import below falls
back to ../src so the demos work from a bare checkout.
"""

import argparse
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

try:
    from libtmux_easy import TmuxSession
except ImportError:  # not installed -- run straight from the checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from libtmux_easy import TmuxSession

from libtmux_easy import (  # noqa: E402  (re-exported for the demos' convenience)
    ExitStatus,
    PANE_MODE_EXEC,
    PANE_MODE_SHELL,
    PANE_MODE_WRAP,
    PaneStatus,
)

__all__ = [
    "TmuxSession",
    "ExitStatus",
    "PaneStatus",
    "PANE_MODE_EXEC",
    "PANE_MODE_SHELL",
    "PANE_MODE_WRAP",
    "demo",
    "parse_args",
    "step",
    "note",
    "check",
    "results",
    "exits_after",
    "runs_forever",
    "ignores_sigint",
    "ignores_sigint_and_sigterm",
    "graceful_sigint_exit",
    "fake_launch",
]


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

_WIDTH = 74
_checks = []


def step(message):
    """Announce a phase of the demo."""
    print(f"\n-- {message}")


def note(message):
    """An aside: something worth explaining but not asserted on."""
    print(f"   note: {message}")


def check(label, actual, expected=None):
    """Print a labelled result. When 'expected' is given, compare against it
    (as strings, so ExitStatus and friends read naturally) and record a
    pass/fail for the summary at the end."""
    actual_s = str(actual)
    if expected is None:
        print(f"   {label:.<44} {actual_s}")
        return True

    ok = actual_s == str(expected)
    _checks.append((label, ok))
    marker = "ok  " if ok else "FAIL"
    suffix = "" if ok else f"   (expected: {expected})"
    print(f"   [{marker}] {label:.<44} {actual_s}{suffix}")
    return ok


def results():
    """Print the pass/fail summary. Returns a process exit code."""
    if not _checks:
        return 0
    failed = [label for label, ok in _checks if not ok]
    print("\n" + "=" * _WIDTH)
    if failed:
        print(f"{len(failed)} of {len(_checks)} checks FAILED:")
        for label in failed:
            print(f"  - {label}")
        return 1
    print(f"all {len(_checks)} checks passed")
    return 0


# --------------------------------------------------------------------------- #
# Session lifecycle
# --------------------------------------------------------------------------- #

def parse_args(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--attach", action="store_true",
                        help="attach to the session when the demo finishes")
    parser.add_argument("--keep", action="store_true",
                        help="leave the session running without attaching")
    parser.add_argument("--status-file", default=None,
                        help="write pane status as JSON here on every transition")
    return parser.parse_args()


def _resize(session, width, height):
    """Give a detached session a usable canvas. 'window-size manual' is
    needed for resize-window to stick on a session with no client attached;
    both are tmux >= 2.9, so failures are non-fatal."""
    try:
        session.set_option("window-size", "manual")
        session.cmd("resize-window", "-x", str(width), "-y", str(height))
    except Exception:  # noqa: BLE001 -- older tmux; demos with few panes still work
        pass


@contextmanager
def demo(name, title, args, window="demo"):
    """Create a session for one demo, yield it, and clean up afterwards.

    An 'info' pane is created first. It is purely cosmetic -- it labels the
    session when you attach -- but it also guarantees the window always has
    one live pane, so a fast-exiting demo pane can never empty the window and
    take the tmux server down with it.
    """
    session_name = f"lte_demo_{name}"
    print("=" * _WIDTH)
    print(f"{title}")
    print("=" * _WIDTH)
    print(f"   tmux session: {session_name}")

    session = TmuxSession.create(session_name, status_file=args.status_file)
    # A detached session inherits an 80x24 default, which runs out of room
    # ('no space for new pane') after about four splits. Demos that create
    # more panes than that need a bigger canvas.
    _resize(session, 220, 60)
    session.new_window(window)
    session.new_pane("info", f"clear; echo '=== libtmux_easy demo: {name} ==='")

    try:
        yield session
    except Exception:  # noqa: BLE001 -- report it, then still tear the session down
        print("\n!! the demo raised an exception:\n")
        traceback.print_exc()
        _checks.append(("demo ran without raising", False))
    finally:
        code = results()
        if args.attach:
            print(f"\nAttaching to '{session_name}'. Detach with Ctrl-b d.")
            print("Kill it afterwards with: tmux kill-session -t " + session_name)
            session.attach()
        elif args.keep:
            print(f"\nLeaving '{session_name}' running. Inspect with:")
            print(f"  tmux attach -t {session_name}")
            print(f"  tmux kill-session -t {session_name}")
        else:
            session.stop_watching()
            session.kill_session()
        sys.exit(code)


def settle(seconds=0.5):
    """Give tmux/psutil a moment to catch up. Demos are timing-sensitive by
    nature -- they are watching real processes start and stop."""
    time.sleep(seconds)


# --------------------------------------------------------------------------- #
# Workloads
#
# All single-line: in shell mode the command is typed into a shell with
# send_keys, so an embedded newline would submit the line early.
# --------------------------------------------------------------------------- #

def exits_after(seconds, code=0):
    """Runs for 'seconds', then exits with 'code' of its own accord."""
    return f"sh -c 'sleep {seconds}; exit {code}'"


def runs_forever():
    """Runs until signalled.

    'exec' matters: it makes the sleep itself the immediate child of the
    shell (or the pane process, in exec mode), rather than leaving an 'sh'
    wrapper in between. That is what the library records as the target pid.
    """
    return "sh -c 'exec sleep 86400'"


def ignores_sigint():
    """Ignores SIGINT, so stop_pane() has to escalate to SIGTERM.

    An ignored (SIG_IGN) disposition survives exec, so the sleep that
    replaces this shell still ignores SIGINT.
    """
    return "sh -c 'trap \"\" INT; exec sleep 86400'"


def ignores_sigint_and_sigterm():
    """Ignores both, forcing stop_pane() all the way to SIGKILL."""
    return "sh -c 'trap \"\" INT TERM; exec sleep 86400'"


def graceful_sigint_exit(code=7):
    """Catches SIGINT and exits with 'code' -- the shape 'ros2 launch' has,
    and the reason a non-zero exit after Ctrl-C is worth distinguishing from
    a signal death."""
    return (
        'python3 -u -c "import signal, sys, time; '
        f"signal.signal(signal.SIGINT, lambda *a: sys.exit({code})); "
        "print('up; waiting for SIGINT', flush=True); time.sleep(86400)\""
    )


def fake_launch(*extra):
    """A stand-in for 'ros2 launch': supervises children, survives one of
    them dying, and exits 0 on SIGINT. See workloads/fake_launch.py."""
    script = Path(__file__).resolve().parent / "workloads" / "fake_launch.py"
    return " ".join(["python3", "-u", str(script), *(str(a) for a in extra)])
