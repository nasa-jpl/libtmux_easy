"""Raw tmux commands, without libtmux: for the modules that only send tmux
commands and need none of its Session/Window/Pane objects (dash, keys,
lte_session, close, lte_defaults), so they stay stdlib only. It talks to the server of
the calling process ($TMUX), or the default one."""

import subprocess
from typing import List, Optional


def tmux(*args, check: bool = False) -> List[str]:
    """Run 'tmux ARGS'.

    :param check: raise if tmux fails
    :return: its stdout lines
    :raises RuntimeError: tmux failed, with 'check'
    """
    proc = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if check and proc.returncode:
        raise RuntimeError(f"tmux {args[0]}: {proc.stderr.strip()}")
    return proc.stdout.splitlines()


def option(target: Optional[str], name: str, scope: str = "-q", run=tmux) -> str:
    """An option's value.

    :param target: the session or window; None: the current one
    :param scope: '-q' session, '-wq' window, '-gq' global
    :param run: runs tmux (a stub, in tests)
    :return: the value, or '' if unset
    """
    out = (
        run("show-options", f"{scope}v", "-t", target, name)
        if target
        else run("show-options", f"{scope}v", name)
    )
    return out[0].strip() if out else ""
