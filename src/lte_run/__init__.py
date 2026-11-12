"""lte-run, and the stdlib helpers it shares with libtmux_easy.

A package of its own, outside libtmux_easy: every wrap-mode pane runs one
lte-run for as long as its command runs, and importing libtmux_easy would
load libtmux and psutil into each (about 3x the startup, +8 MB). Stdlib
only; never imports libtmux_easy.

    wrap    lte-run itself; tmux pipe guards; plain-text conversion
    proc    finding and inspecting processes (/proc; psutil on demand)
"""
