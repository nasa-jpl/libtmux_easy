"""Running the tmux binary, for the stdlib-only modules (dash, keys, launch).
It talks to the server of the calling process ($TMUX), or the default one."""

import subprocess


def tmux(*args, check=False):
    """Run 'tmux ARGS'; return its stdout lines. 'check' raises on failure."""
    proc = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if check and proc.returncode:
        raise RuntimeError(f"tmux {args[0]}: {proc.stderr.strip()}")
    return proc.stdout.splitlines()


def option(target, name, scope="-q", run=tmux):
    """An option's value, or ''. 'scope': '-q' session, '-wq' window, '-gq' global."""
    out = run("show-options", f"{scope}v", "-t", target, name) if target else \
        run("show-options", f"{scope}v", name)
    return out[0].strip() if out else ""
