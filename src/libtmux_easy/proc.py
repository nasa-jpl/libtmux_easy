"""
Process discovery/inspection utilities, built on psutil.

Includes plain-PID helpers (get_child_pids) as well as tmux-aware helpers
(pane_child_processes, get_session_pids) used by TmuxSession's graceful
teardown logic (see tmux_lib.stop_pane).
"""

import psutil

from libtmux.pane import Pane
from libtmux.session import Session


def get_child_pids(parent_pid: int, recursive: bool = False) -> list:
    """
    Find the PIDs of all child processes of a given parent PID.

    :param parent_pid: The PID of the parent process.
    :param recursive: If True, includes children of children, and so on.
    :return: A list of child PIDs (integers). Empty if the parent doesn't exist
        or has no (matching) children.
    """
    try:
        parent = psutil.Process(parent_pid)
    except psutil.NoSuchProcess:
        return []

    return [p.pid for p in parent.children(recursive=recursive)]


def pane_child_processes(pane: Pane) -> list:
    """Running descendant processes of a pane's shell (i.e. whatever command is
    currently executing in the pane), excluding the shell itself."""
    try:
        root = psutil.Process(int(pane.pane_pid))
    except psutil.NoSuchProcess:
        return []
    return [p for p in root.children(recursive=True) if p.is_running()]


def get_session_pids(session: Session) -> dict:
    """Return the currently running child process PIDs for every pane in a tmux
    session, keyed by window name then pane id. Idle panes (no running child
    process) are omitted, as are windows with no such panes.

        {
            "<window_name>": {
                "<pane_id>": [pid, pid, ...],
                ...
            },
            ...
        }
    """
    result = {}
    for window in session.windows:
        panes_with_children = {
            pane.id: [p.pid for p in pane_child_processes(pane)]
            for pane in window.panes
        }
        panes_with_children = {
            pane_id: pids for pane_id, pids in panes_with_children.items() if pids
        }
        if panes_with_children:
            result[window.name] = panes_with_children
    return result
