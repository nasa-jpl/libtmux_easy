"""Status snapshots: how a pane's command finished, and a pane's state."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ExitStatus:
    """How a pane's command finished.

    When known, exactly one of these is set:
      code    -- the process exited normally with this status
      signal  -- the process was killed by this signal number

    'inferred' is True when 'signal' was derived from a shell's 128+N
    convention (shell-mode panes) rather than reported directly by tmux or
    lte-run. In that case a genuine 'exit 130' cannot be distinguished from
    'killed by SIGINT'. Exec- and wrap-mode panes never have this ambiguity.
    """

    code: Optional[int] = None
    signal: Optional[int] = None
    inferred: bool = False

    @property
    def known(self) -> bool:
        """False when the status could not be determined (still running, or the
        pane/shell could not be probed)."""
        return self.code is not None or self.signal is not None

    @property
    def ok(self) -> bool:
        """True only for a normal exit with status 0."""
        return self.code == 0

    def __str__(self) -> str:
        if self.signal is not None:
            base = f"killed by signal {self.signal}"
            return f"{base} (inferred)" if self.inferred else base
        if self.code is not None:
            return f"exited {self.code}"
        return "unknown"


@dataclass(frozen=True)
class PaneStatus:
    """A snapshot of one pane created via new_pane(). See TmuxSession.status()
    and .pane_status()."""

    name: str                  # 'window:pane', or the raw pane id if unnamed
    pane_id: str                # '%3'
    mode: str                   # PANE_MODE_SHELL | PANE_MODE_EXEC | PANE_MODE_WRAP
    command: str
    target_pids: tuple          # ints, for display -- see new_pane()
    running: bool
    exit_status: ExitStatus
    # Set once stop_pane() (or stop_all()) ended this run: the last signal it
    # had to send, 'SIGINT' / 'SIGTERM' / 'SIGKILL'. None for a run that
    # ended on its own -- including a Ctrl-C typed into the pane.
    stopped: Optional[str] = None
    # Typed by new_pane(wait_start=False); lte-run has not reported yet.
    # 'running' is True meanwhile: the command is on its way.
    starting: bool = False

    @property
    def state(self) -> str:
        """'starting', 'running', 'stopped' (by request), 'ok' (exit 0),
        'failed', or 'unknown'."""
        if self.starting:
            return "starting"
        if self.running:
            return "running"
        if self.stopped:
            return "stopped"
        if self.exit_status.ok:
            return "ok"
        return "failed" if self.exit_status.known else "unknown"

    def __str__(self) -> str:
        if self.starting:
            return "starting"
        if self.running:
            return "running"
        if self.stopped:
            return f"stopped ({self.stopped}): {self.exit_status}"
        return str(self.exit_status)
