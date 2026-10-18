# The names below are loaded on first use, not at import. The lte-run console
# script imports libtmux_easy.wrap, which runs this file first; importing
# tmux_lib here would drag libtmux and psutil into every wrapped pane (about
# 4x the startup time and +7 MB each) for a script that uses neither.
_EXPORTS = (
    "TmuxSession",
    "session_exists",
    "set_server",
    "tmux_command",
    "DeprecatedError",
    "libtmux_version",
    "ExitStatus",
    "PaneStatus",
    "PANE_MODE_SHELL",
    "PANE_MODE_EXEC",
    "PANE_MODE_WRAP",
    "Capture",
    "OutputExpect",
    "OutputMatch",
    "OutputWatch",
    "PaneExited",
)

__all__ = list(_EXPORTS)


def __getattr__(name):
    if name in _EXPORTS:
        from . import tmux_lib

        return getattr(tmux_lib, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
