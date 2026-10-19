"""Tell the sidebar the moment its session's current window changes.

A global 'session-window-changed' hook, guarded to one session, sends the
sidebar SIGUSR1. It goes in the global hook array at an index of its own: a
session-scoped hook would shadow the user's global one for that session.
An entry left by a sidebar that died is harmless (its kill fails) and is
swept by the next install().

Stdlib only.
"""

import os
import re

from .._tmux import tmux as _tmux

HOOK = "session-window-changed"
BASE = 100           # first index used: low ones are left to the user
MARK = "lte-dash-hook"

_ENTRY = re.compile(rf"^{HOOK}\[(\d+)\]\s+(.*)$")
_PID = re.compile(rf"kill -USR1 (\d+) .*{MARK}")


def _entries(run):
    """index -> command, for the global hook array."""
    entries = {}
    for line in run("show-hooks", "-g"):
        m = _ENTRY.match(line.strip())
        if m:
            entries[int(m.group(1))] = m.group(2)
    return entries


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def install(session_id, pid, run=_tmux):
    """Hook 'session_id''s window changes to SIGUSR1 for 'pid'. Returns the
    hook index for remove(), or None if tmux did not take it."""
    entries = _entries(run)
    for index, command in entries.items():
        m = _PID.search(command)
        if m and not _alive(int(m.group(1))):
            remove(index, run)
    index = max(BASE, max(entries, default=0) + 1)
    # Single quotes: tmux must not expand the '$' of a session id.
    command = (f"if-shell -F '#{{==:#{{session_id}},{session_id}}}' "
               f"{{ run-shell -b 'kill -USR1 {pid} 2>/dev/null || : {MARK}' }}")
    run("set-hook", "-g", f"{HOOK}[{index}]", command)
    return index if index in _entries(run) else None


def remove(index, run=_tmux):
    run("set-hook", "-gu", f"{HOOK}[{index}]")
