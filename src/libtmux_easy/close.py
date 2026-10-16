"""lte-close: close a tmux session gracefully, running its close actions.

    lte-close [-t SESSION] [--timeouts INT,TERM,KILL]
    lte-close add [-t SESSION] [--before] -- COMMAND [ARG...]
    lte-close list [-t SESSION]
    lte-close clear [-t SESSION]

Opens a 'close' window in the session and, there, with its progress shown:

  1. runs the 'before' actions;
  2. stops what runs in the panes: the session's script, if it is still
     running, stops its own (and runs its Python on_close callbacks); every
     other pane gets Ctrl-C, then SIGTERM, then SIGKILL;
  3. runs the 'after' actions (the default for 'add');
  4. kills the session.

Ctrl-C in the close window skips to killing the session. Works on any
session, with or without libtmux_easy. Bind it in ~/.tmux.conf:

    bind-key C-x run-shell -b "lte-close -t '#{session_id}'"

Stdlib only. See docs/session-close.md.
"""

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import wrap
from ._tmux import option, tmux as _tmux
from .dash.control import Control, describe, read_status
from .dash.options import (HOME_KIND_OPTION, HOME_OPTION, SIDEBAR_OPTION, STATUS_OPTION,
                           WINDOW_OPTION)

ACTIONS_OPTION = "@lte_on_close"   # session option: JSON list of {"cmd", "cwd", "when"}
STATE_OPTION = "@lte_closing"      # set by the script: "stopping", then "done"
WINDOW_OPTION_CLOSE = "@lte_close_window"
KEY_OPTION = "@lte_close_key"      # session option: set where a borrowed close key applies
WINDOW_NAME = "close"
WHEN = ("before", "after")

TIMEOUTS = (10.0, 5.0, 2.0)        # SIGINT, SIGTERM, SIGKILL: as stop_pane()
ACK_TIMEOUT = 2.0                  # for the script to take the 'close' command
SCRIPT_TIMEOUT = 120.0             # for it to finish
PAUSE = 1.5                        # the summary stays up this long before the kill
SHELLS = frozenset(("bash", "sh", "zsh", "fish", "dash", "ksh", "tcsh", "csh"))


# ---------- actions (session option) ----------

def actions(session, run=_tmux):
    try:
        return json.loads(option(session, ACTIONS_OPTION, run=run) or "[]")
    except ValueError:
        return []


def add_action(session, command, when="after", cwd=None, run=_tmux):
    """Store shell 'command' to run on close, in 'cwd' (default: here)."""
    if when not in WHEN:
        raise ValueError(f"when must be one of {WHEN}, not {when!r}")
    entries = actions(session, run) + [{"cmd": command, "cwd": cwd or os.getcwd(), "when": when}]
    run("set-option", "-q", "-t", session, ACTIONS_OPTION, json.dumps(entries))


def clear_actions(session, run=_tmux):
    run("set-option", "-uq", "-t", session, ACTIONS_OPTION)


def run_actions(entries, when, out=print):
    """Run the shell actions for one phase, in order; output goes to ours."""
    for entry in (e for e in entries if e.get("when", "after") == when):
        out(f"$ {entry['cmd']}")
        try:
            code = subprocess.run(entry["cmd"], shell=True, cwd=entry.get("cwd") or None).returncode
        except OSError as exc:
            out(f"  failed: {exc}")
            continue
        if code:
            out(f"  exited {code}")


# ---------- processes (stdlib, Linux /proc) ----------

def _stat(pid):
    """(state, ppid) of 'pid', or None if it is gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rpartition(")")[2].split()
        return fields[0], int(fields[1])
    except (OSError, IndexError, ValueError):
        return None


def _alive(pid):
    stat = _stat(pid)
    return stat is not None and stat[0] != "Z"


def _comm(pid):
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return ""


def _descendants(pid):
    children = {}
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            stat = _stat(int(entry))
            if stat is not None:
                children.setdefault(stat[1], []).append(int(entry))
    found, todo = [], list(children.get(pid, ()))
    while todo:
        child = todo.pop()
        found.append(child)
        todo.extend(children.get(child, ()))
    return found


def targets(pane_pid):
    """What a pane runs: a shell's descendants (the shell itself stays), or
    the pane's own process and its descendants."""
    found = _descendants(pane_pid)
    return found if _comm(pane_pid) in SHELLS else [pane_pid] + found


# ---------- the close sequence ----------

class Closer:
    def __init__(self, session, timeouts=TIMEOUTS, out=print, run=_tmux):
        self.session = session
        self.timeouts = timeouts
        self.out = out
        self.run = run
        self.name = (run("display-message", "-p", "-t", session, "#{session_name}") or [session])[0]

    def opt(self, name, target=None):
        return option(target or self.session, name, run=self.run)

    def __call__(self):
        out = self.out
        out(f"Closing session {self.name}. Ctrl-C: skip to killing it now.\n")
        entries = actions(self.session, self.run)
        try:
            run_actions(entries, "before", out)
            self.stop_script()
            self.stop_panes()
            run_actions(entries, "after", out)
        except KeyboardInterrupt:
            out("\nSkipped the rest.")
        out("\nKilling the session.")
        time.sleep(PAUSE)
        self.run("kill-session", "-t", self.session)

    # ----- the script -----

    def stop_script(self):
        """Hand the script its own panes: it knows how they end (wrap mode)
        and runs its Python on_close callbacks."""
        ctl_dir = self.opt(wrap.DIR_OPTION)
        control = Control(os.path.join(ctl_dir, wrap.SOCKET_NAME) if ctl_dir else None)
        if not control.online():
            return
        self.run("set-option", "-uq", "-t", self.session, STATE_OPTION)
        control.send("close", None)
        deadline = time.monotonic() + ACK_TIMEOUT
        while not self.opt(STATE_OPTION) and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self.opt(STATE_OPTION):
            self.out("The session's script did not answer: stopping its panes directly.")
            return
        self.out("The session's script is stopping its panes:")
        seen = {}
        deadline = time.monotonic() + SCRIPT_TIMEOUT
        while time.monotonic() < deadline:
            done = self.opt(STATE_OPTION) == "done"
            path = self.opt(STATUS_OPTION)
            for entry in (read_status(path) if path else None) or ():
                label = describe(entry)[0]
                if seen.get(entry["pane_id"]) != label:
                    if entry["pane_id"] in seen or label != "running":
                        self.out(f"  {entry['name']}: {label}")
                    seen[entry["pane_id"]] = label
            if done:
                return
            time.sleep(0.2)
        self.out("  ...gave up waiting for it.")

    # ----- every other pane -----

    def _panes(self):
        """pane id -> label, for the panes to stop: not this one, nor the
        dashboard's sidebar or (unless it runs a command) its home pane."""
        skip = {os.environ.get("TMUX_PANE")}
        dash_window = self.opt(WINDOW_OPTION)
        if dash_window:
            skip.add(self.opt(SIDEBAR_OPTION))
            if option(dash_window, HOME_KIND_OPTION, "-wq", run=self.run) != "command":
                skip.add(option(dash_window, HOME_OPTION, "-wq", run=self.run))
        panes = {}
        for line in self.run("list-panes", "-s", "-t", self.session, "-F",
                             "#{pane_id} #{pane_pid} #{pane_dead} #{window_name}.#{pane_index}"):
            fields = line.split(None, 3)
            if len(fields) == 4 and fields[0] not in skip and fields[2] != "1":
                panes[fields[0]] = (int(fields[1]), fields[3])
        return panes

    def stop_panes(self):
        running = {}
        for pane_id, (pid, label) in self._panes().items():
            pids = targets(pid)
            if pids:
                running[pane_id] = (f"{label} ({_comm(pids[0]) or pids[0]})", pids)
        if not running:
            self.out("Nothing left running in the panes.")
            return
        self.out(f"Stopping {len(running)} pane(s):")
        for step, timeout in zip(("SIGINT", "SIGTERM", "SIGKILL"), self.timeouts):
            if not running:
                break
            for pane_id, (label, pids) in running.items():
                if step == "SIGINT":
                    # A typed Ctrl-C: the foreground job, as the user would.
                    self.run("send-keys", "-t", pane_id, "C-c")
                else:
                    for pid in pids:
                        try:
                            os.kill(pid, getattr(signal, step))
                        except (ProcessLookupError, PermissionError):
                            pass
            self.out(f"  {step} to {', '.join(l for l, _ in running.values())}")
            deadline = time.monotonic() + timeout
            while running and time.monotonic() < deadline:
                for pane_id, (label, pids) in list(running.items()):
                    if not any(_alive(p) for p in pids):
                        self.out(f"  {label}: stopped by {step}")
                        del running[pane_id]
                time.sleep(0.1)
        for label, _ in running.values():
            self.out(f"  {label}: still running")


# ---------- the window ----------

def self_command():
    """Shell text that runs lte-close from this install, PATH or not."""
    src = Path(__file__).resolve().parents[1]
    return f"env PYTHONPATH={shlex.quote(str(src))} {shlex.quote(sys.executable)} -m libtmux_easy.close"


def key_command():
    """tmux command text for a key binding: close the session it's pressed in."""
    return f"run-shell -b \"{self_command()} -t '#{{session_id}}'\""


def _session_of_caller():
    pane = os.environ.get("TMUX_PANE")
    out = _tmux("display-message", "-p", "-t", pane, "#{session_id}") if pane else []
    return out[0].strip() if out else None


def open_close_window(session, timeouts=TIMEOUTS):
    """Run the close sequence in a new window of 'session' (or the one it
    already has), and show it."""
    window = option(session, WINDOW_OPTION_CLOSE)
    if window and _tmux("list-panes", "-t", window, "-F", "#{pane_id}"):
        _tmux("select-window", "-t", window)
        return window
    command = (f"{self_command()} --here -t {shlex.quote(session)} "
               f"--timeouts {','.join(str(t) for t in timeouts)}")
    out = _tmux("new-window", "-t", f"{session}:", "-n", WINDOW_NAME,
                "-P", "-F", "#{window_id}", command)
    if not out:
        return None
    window = out[0].strip()
    _tmux("set-option", "-q", "-t", session, WINDOW_OPTION_CLOSE, window)
    _tmux("select-window", "-t", window)
    return window


def _timeouts(text):
    values = tuple(float(v) for v in text.split(","))
    if len(values) != 3:
        raise argparse.ArgumentTypeError("three values: SIGINT,SIGTERM,SIGKILL seconds")
    return values


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="lte-close", description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 2)[2])
    parser.add_argument("-t", "--target", help="session (default: the one this runs in)")
    parser.add_argument("--timeouts", type=_timeouts, default=TIMEOUTS, metavar="INT,TERM,KILL",
                        help="seconds to wait after each signal (default 10,5,2)")
    parser.add_argument("--here", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("action", nargs="?", default="close",
                        choices=("close", "add", "list", "clear"))
    parser.add_argument("--before", action="store_true",
                        help="add: run before the panes are stopped (default: after)")
    parser.add_argument("command", nargs="*", help="add: the command ('--' before one with options)")
    argv = sys.argv[1:] if argv is None else list(argv)
    # A command after '--' is split off by hand: argparse mishandles '--'
    # next to an optional positional.
    after = argv[argv.index("--") + 1:] if "--" in argv else None
    args = parser.parse_args(argv[:argv.index("--")] if after is not None else argv)
    if after is not None:
        args.command = args.command + after

    session = args.target or _session_of_caller()
    if not session or not _tmux("display-message", "-p", "-t", session, "#{session_id}"):
        sys.stderr.write("lte-close: no such session (run inside tmux, or pass -t)\n")
        return 1
    session = _tmux("display-message", "-p", "-t", session, "#{session_id}")[0].strip()

    if args.action == "add":
        command = args.command
        if not command:
            parser.error("add needs a command")
        text = command[0] if len(command) == 1 else " ".join(shlex.quote(c) for c in command)
        add_action(session, text, "before" if args.before else "after")
        return 0
    if args.action == "list":
        for entry in actions(session):
            print(f"{entry.get('when', 'after'):6}  {entry['cmd']}  (in {entry.get('cwd')})")
        return 0
    if args.action == "clear":
        clear_actions(session)
        return 0
    if args.here:
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        Closer(session, args.timeouts)()
        return 0
    return 0 if open_close_window(session, args.timeouts) else 1


if __name__ == "__main__":
    sys.exit(main())
