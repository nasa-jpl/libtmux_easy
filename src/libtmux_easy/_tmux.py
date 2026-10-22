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


# Set by tmux for each pane, or describing the caller's own terminal or
# session: never copied into a pane's environment. LTE_DIR would point
# lte-run at another session's socket.
# BASH_ENV: a non-interactive bash sources it, and one that sources ROS again
# (e.g. a container's) would drop the overlay the copied environment has.
SKIP_ENV = frozenset(("TMUX", "TMUX_PANE", "TERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION",
                      "COLUMNS", "LINES", "LTE_DIR", "BASH_ENV"))


def pane_env(env):
    """'env' (a mapping, e.g. os.environ) as it may go into a pane: SKIP_ENV
    left out, values as strings. A None value stays None: removed from the
    pane's environment (see TmuxSession.new_pane(env=))."""
    return {k: None if v is None else str(v) for k, v in (env or {}).items()
            if k not in SKIP_ENV}
