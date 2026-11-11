"""Running the tmux binary, for the stdlib-only modules (dash, keys, launch).
It talks to the server of the calling process ($TMUX), or the default one."""

import subprocess
from typing import Dict, List, Mapping, Optional


def tmux(*args, check: bool = False) -> List[str]:
    """Run 'tmux ARGS'.

    @param check: raise if tmux fails
    @return: its stdout lines
    @raise RuntimeError: tmux failed, with 'check'
    """
    proc = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if check and proc.returncode:
        raise RuntimeError(f"tmux {args[0]}: {proc.stderr.strip()}")
    return proc.stdout.splitlines()


def option(target: Optional[str], name: str, scope: str = "-q", run=tmux) -> str:
    """An option's value.

    @param target: the session or window; None: the current one
    @param scope: '-q' session, '-wq' window, '-gq' global
    @param run: runs tmux (a stub, in tests)
    @return: the value, or '' if unset
    """
    out = (
        run("show-options", f"{scope}v", "-t", target, name)
        if target
        else run("show-options", f"{scope}v", name)
    )
    return out[0].strip() if out else ""


# Set by tmux for each pane, or describing the caller's own terminal or
# session: never copied into a pane's environment. LTE_DIR would point
# lte-run at another session's socket.
# BASH_ENV: a non-interactive bash sources it, and one that sources ROS again
# (e.g. a container's) would drop the overlay the copied environment has.
SKIP_ENV = frozenset(
    (
        "TMUX",
        "TMUX_PANE",
        "TERM",
        "TERM_PROGRAM",
        "TERM_PROGRAM_VERSION",
        "COLUMNS",
        "LINES",
        "LTE_DIR",
        "BASH_ENV",
    )
)


def pane_env(env: Optional[Mapping[str, object]]) -> Dict[str, Optional[str]]:
    """'env' as it may go into a pane: SKIP_ENV left out, values as strings.

    @param env: a mapping, e.g. os.environ, or None. A None value stays None:
        removed from the pane's environment (see TmuxSession.new_pane(env=))
    @return: the variables, name -> str or None
    """
    return {k: None if v is None else str(v) for k, v in (env or {}).items() if k not in SKIP_ENV}
