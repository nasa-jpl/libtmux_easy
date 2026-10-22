"""Process discovery: what runs in a pane, and whether it is alive.

Two kinds of function. The stdlib ones read /proc directly, for lte-run and
lte-close: those start once per pane or key press, and importing psutil and
libtmux would cost them ~4x the startup. The psutil ones (process objects,
for the library) import psutil when called. Keep psutil and libtmux out of
the module level: test_wrapper_import_stays_light checks it.
"""

import os
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import psutil
    from libtmux.pane import Pane
    from libtmux.session import Session

# A pane running one of these is a shell: what it runs are its descendants.
SHELLS = frozenset(("bash", "sh", "zsh", "fish", "dash", "ksh", "tcsh", "csh"))


# ---------- stdlib: /proc ----------

def proc_stat(pid):
    """(state, ppid, pgrp) of 'pid', from /proc; None if it is gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            # Field 2 (comm) may contain spaces and parens; split after it.
            fields = f.read().rsplit(")", 1)[1].split()
        return fields[0], int(fields[1]), int(fields[2])
    except (OSError, ValueError, IndexError):
        return None


def proc_table():
    """pid -> proc_stat(pid), for every process; {} without /proc."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return {}
    table = {}
    for name in names:
        if name.isdigit():
            stat = proc_stat(name)
            if stat is not None:  # else it exited mid-scan
                table[int(name)] = stat
    return table


def _children(table):
    """ppid -> [pid, ...] of a proc_table(), each list sorted."""
    children = {}
    for child, (_, ppid, _) in table.items():
        children.setdefault(ppid, []).append(child)
    for pids in children.values():
        pids.sort()
    return children


def descendants(pid, table=None):
    """The pids below 'pid' in a proc_table() (a fresh one by default)."""
    children = _children(proc_table() if table is None else table)
    found, todo = [], list(children.get(pid, ()))
    while todo:
        child = todo.pop()
        found.append(child)
        todo.extend(children.get(child, ()))
    return found


def descendant_tree(pid, table=None):
    """[(depth, pid), ...] below 'pid', depth 1 for its children, in the
    order 'ps axf' draws them: depth first, siblings by pid."""
    children = _children(proc_table() if table is None else table)
    found, todo = [], [(1, child) for child in reversed(children.get(pid, ()))]
    while todo:
        depth, child = todo.pop()
        found.append((depth, child))
        todo.extend((depth + 1, c) for c in reversed(children.get(child, ())))
    return found


def cmdline(pid):
    """The process's command line ('sleep 5'); its '[name]' when it has
    none (a kernel thread, a zombie); '' if it is gone."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            args = f.read().rstrip(b"\0").split(b"\0")
    except OSError:
        return ""
    line = " ".join(a.decode(errors="replace") for a in args if a)
    if line:
        return line
    name = comm(pid)
    return f"[{name}]" if name else ""


def alive(pid):
    """'pid' exists and is not a zombie."""
    stat = proc_stat(pid)
    return stat is not None and stat[0] != "Z"


def comm(pid):
    """The process's name ('bash'), or ''."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return ""


def pane_pids(pane_pid):
    """What a pane runs: a shell's descendants (the shell itself stays), or
    the pane's own process and its descendants."""
    found = descendants(pane_pid)
    return found if comm(pane_pid) in SHELLS else [pane_pid] + found


# ---------- psutil ----------

def proc_alive(proc: "psutil.Process") -> bool:
    """True if 'proc' is still alive and not a zombie (psutil's is_running()
    is True for a zombie)."""
    import psutil
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def pane_process(pane: "Pane") -> Optional["psutil.Process"]:
    """The pane's own process (an exec pane's command), or None."""
    import psutil
    try:
        return psutil.Process(int(pane.pane_pid))
    except (psutil.NoSuchProcess, ValueError, TypeError):
        return None


def get_child_pids(parent_pid: int, recursive: bool = False) -> list:
    """The pids of 'parent_pid''s children (and theirs, if 'recursive');
    [] if it doesn't exist."""
    import psutil
    try:
        parent = psutil.Process(parent_pid)
    except psutil.NoSuchProcess:
        return []
    return [p.pid for p in parent.children(recursive=recursive)]


def pane_child_processes(pane: "Pane") -> list:
    """Running descendants of a pane's process (whatever its shell runs),
    excluding that process itself."""
    root = pane_process(pane)
    if root is None:
        return []
    return [p for p in root.children(recursive=True) if p.is_running()]


def get_session_pids(session: "Session") -> dict:
    """{window name: {pane id: [pid, ...]}} of what runs in each pane of
    'session'. Idle panes, and windows with only idle panes, are left out."""
    result = {}
    for window in session.windows:
        panes = {pane.id: [p.pid for p in pane_child_processes(pane)] for pane in window.panes}
        panes = {pane_id: pids for pane_id, pids in panes.items() if pids}
        if panes:
            result[window.name] = panes
    return result
