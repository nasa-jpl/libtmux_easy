"""Starter tmux settings, for a tmux server that loaded no config file.

    lte-defaults            print them, as ~/.tmux.conf lines

A server that read a config (~/.tmux.conf, ~/.config/tmux/tmux.conf,
/etc/tmux.conf, -f FILE) is left as its user set it up. One that read none
gets CONF, sourced once, when the library first makes or adopts a session
there (TmuxSession.create() / current(), lte-session). LTE_TMUX_DEFAULTS=0
turns that off.

Stdlib only. See docs/tmux-defaults.md.
"""

import argparse
import os
import sys
import tempfile

from ._tmux import option as _option, tmux as _tmux

ENV = "LTE_TMUX_DEFAULTS"     # "0": never source CONF
DONE_OPTION = "@lte_starter"  # global: "1" sourced, "0" checked and skipped

CONF = """\
# libtmux_easy starter settings (lte-defaults). Applied only to a tmux server
# with no config file; copy them into ~/.tmux.conf to keep or change them.

# Vim keys in copy mode
setw -g mode-keys vi

# Split, and move between panes
bind-key | split-window -h
bind-key - split-window -v
bind-key h select-pane -L
bind-key j select-pane -D
bind-key k select-pane -U
bind-key l select-pane -R
bind-key C-x kill-session

# Mouse: click a pane, drag a border, scroll
set -g mouse on
"""


def has_config(run=_tmux):
    """True if the server loaded a config file (/dev/null counts as none)."""
    out = run("display-message", "-p", "#{config_files}")
    files = [f for f in (out[0].split(",") if out else []) if f.strip()]
    return any(f.strip() != "/dev/null" for f in files)


def apply(run=_tmux):
    """Source CONF into the server once, if it loaded no config and
    $LTE_TMUX_DEFAULTS isn't 0. True if sourced by this call."""
    if os.environ.get(ENV, "").strip() == "0" or _option(None, DONE_OPTION, "-gq", run=run):
        return False
    if has_config(run):
        run("set-option", "-gq", DONE_OPTION, "0")
        return False
    with tempfile.NamedTemporaryFile("w", suffix=".tmux", prefix="lte_starter_",
                                     delete=False) as handle:
        handle.write(CONF)
    try:
        run("source-file", handle.name)
    finally:
        os.unlink(handle.name)
    run("set-option", "-gq", DONE_OPTION, "1")
    return True


def main(argv=None):
    argparse.ArgumentParser(prog="lte-defaults", description=__doc__.splitlines()[0]) \
        .parse_args(argv)
    sys.stdout.write(CONF)
    return 0


if __name__ == "__main__":
    sys.exit(main())
