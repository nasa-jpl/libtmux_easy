"""lte-session: run a command as the home pane of a new dashboard session.

    lte-session [--name NAME] [--keep] [--width N|N%|auto] [--no-dash] -- COMMAND [ARG...]

The command (e.g. pytest) runs in a real terminal -- the dashboard's home
pane -- so print(), input(), pdb and Ctrl-C all work, while the panes it
starts through TmuxSession.current() show up in the sidebar. When it ends
(or you detach, or with no terminal, as in CI), its output is printed here,
and lte-session exits with the command's exit status. --keep leaves the
session open after the command ends, until you detach.

Stdlib only. See docs/lte-session.md.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import close, dash, starter, wrap
from .dash import keys
from ._tmux import option, tmux as _tmux

ENV_FILE = "env.json"      # the launching shell's environment and cwd
EXIT_FILE = "exit.json"    # the command's 'exited' message, from the runner
TRANSCRIPT = "transcript"  # everything the home pane printed (pipe-pane)

HISTORY_LIMIT = 100000     # scrollback lines kept for the copy printed at the end

# Kept from the pane rather than the launching shell: they describe the
# terminal the command now runs in.
_PANE_ENV = ("TMUX", "TMUX_PANE", "TERM")
# BASH_ENV: a 'bash -c' command would source it, and one that sources ROS
# again (e.g. a container's) drops the workspace overlay (see _tmux.SKIP_ENV).
_DROP_ENV = ("COLUMNS", "LINES", "BASH_ENV")

_DEAD_LINE = re.compile(r"^(\x1b\[[0-9;]*m)*Pane is dead \(")


# ---------- runner: inside the home pane ----------

class _ExitFile:
    """A wrap.Channel stand-in that keeps only the 'exited' message."""

    def __init__(self, path):
        self.path = path

    def send(self, **msg):
        if msg.get("event") != "exited":
            return
        tmp = f"{self.path}.tmp"
        with open(tmp, "w") as f:
            json.dump(msg, f)
        os.replace(tmp, self.path)

    def messages(self):
        return iter(())


def run(run_dir, argv):
    """Run 'argv' as the launching shell would have, under lte-run's
    supervision (Ctrl-C reaches it; the exact status is recorded)."""
    with open(os.path.join(run_dir, ENV_FILE)) as f:
        saved = json.load(f)
    env = {k: v for k, v in saved["env"].items() if k not in _PANE_ENV + _DROP_ENV}
    env.update({k: os.environ[k] for k in _PANE_ENV if k in os.environ})
    os.environ.clear()
    os.environ.update(env)
    try:
        os.chdir(saved["cwd"])
    except OSError as exc:
        sys.stderr.write(f"lte-session: {saved['cwd']}: {exc.strerror}\n")
    return wrap.main(argv, channel=_ExitFile(os.path.join(run_dir, EXIT_FILE)))


# ---------- launcher: in your terminal ----------

class Launch:
    def __init__(self, argv, name=None, width=dash.AUTO, dash_on=True):
        self.argv = argv
        self.label = Path(argv[0]).name
        self.run_dir = tempfile.mkdtemp(prefix="lte_session_")
        self.exit_file = os.path.join(self.run_dir, EXIT_FILE)
        self.transcript = os.path.join(self.run_dir, TRANSCRIPT)
        with open(os.path.join(self.run_dir, ENV_FILE), "w") as f:
            json.dump({"env": dict(os.environ), "cwd": os.getcwd()}, f)
        Path(self.transcript).touch()

        base = name or f"lte_{Path(argv[0]).name}_{os.getpid()}"
        self.name = re.sub(r"[.:]", "_", base)  # tmux forbids both in session names
        cols, rows = shutil.get_terminal_size((200, 50))
        out = _tmux("new-session", "-d", "-s", self.name, "-x", str(cols), "-y", str(rows),
                    "-P", "-F", "#{session_id} #{window_id}", "cat", check=True)
        self.session, scratch = out[0].split()
        # history-limit applies to panes created from here on, hence the
        # throwaway first window.
        _tmux("set-option", "-t", self.session, "history-limit", str(HISTORY_LIMIT))
        starter.apply()
        self.close_key = keys.chosen(close.KEY_ROLE, close.KEY)
        if self.close_key and keys.borrow(self.close_key, close.KEY_OPTION, close.key_command(),
                                          "lte-close: not a libtmux_easy session",
                                          if_free=True):
            _tmux("set-option", "-q", "-t", self.session, close.KEY_OPTION, "1")
        else:
            self.close_key = ""

        self.window = self.home = None
        self.return_to = None  # (client, session) to switch back to, inside tmux
        if dash_on:
            try:
                self.window, _ = dash.open_window(self.session, None, None, None,
                                                  width=width, home_cmd="cat")
                self.home = option(self.window, dash.HOME_OPTION, "-wq")
            except RuntimeError as exc:
                sys.stderr.write(f"lte-session: no dashboard. {exc}")
        if self.window is None:
            out = _tmux("new-window", "-d", "-t", f"{self.session}:", "-n", "home",
                        "-P", "-F", "#{window_id} #{pane_id}", "cat", check=True)
            self.window, self.home = out[0].split()
        _tmux("kill-window", "-t", scratch)

        # Both must be in place before the command can print or exit.
        _tmux("set-option", "-p", "-t", self.home, "remain-on-exit", "on")
        _tmux("pipe-pane", "-o", "-t", self.home, f"cat >> {shlex.quote(self.transcript)}")
        src = Path(__file__).resolve().parents[1]
        runner = (f"env PYTHONPATH={shlex.quote(str(src))} {shlex.quote(sys.executable)} "
                  f"-m libtmux_easy.launch --run {shlex.quote(self.run_dir)} -- "
                  + " ".join(shlex.quote(a) for a in argv))
        _tmux("set-option", "-q", "-t", self.session, dash.RUN_DIR_OPTION, self.run_dir)
        _tmux("set-option", "-q", "-t", self.session, dash.HOME_STATUS_OPTION, "running")
        _tmux("respawn-pane", "-k", "-t", self.home, runner, check=True)
        _tmux("select-pane", "-t", self.home, "-T", "home")
        _tmux("select-window", "-t", self.window)
        _tmux("select-pane", "-t", self.home)  # typing goes to the command

    # ----- state -----

    def alive(self):
        return subprocess.run(["tmux", "has-session", "-t", self.session],
                              capture_output=True).returncode == 0

    def exit_msg(self):
        try:
            with open(self.exit_file) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def finished(self):
        """The command has exited, or can no longer report that it did."""
        if self.exit_msg() is not None or not self.alive():
            return True
        return not _tmux("list-panes", "-t", self.home, "-F", "#{pane_id}")

    def exit_code(self):
        msg = self.exit_msg()
        if msg is None:
            sys.stderr.write("lte-session: the command's exit status is unknown "
                             "(its pane was closed?)\n")
            return 1
        return 128 + msg["signal"] if msg.get("signal") is not None else msg["code"]

    def describe_exit(self):
        msg = self.exit_msg() or {}
        if msg.get("signal") is not None:
            try:
                return f"killed by {signal.Signals(msg['signal']).name}"
            except ValueError:
                return f"killed by signal {msg['signal']}"
        return f"exited {msg.get('code')}"

    # ----- output -----

    def capture(self):
        """The home pane's whole scrollback, colors included, or None."""
        out = _tmux("capture-pane", "-p", "-e", "-J", "-S", "-", "-t", self.home)
        if not out:
            return None
        while out and (not out[-1].strip() or _DEAD_LINE.match(out[-1])):
            out.pop()
        return "\n".join(out) + "\x1b[0m\n"

    def follow(self, offset=0):
        """Copy the transcript from 'offset' to stdout until the command ends."""
        out = sys.stdout.buffer
        with open(self.transcript, "rb") as f:
            f.seek(offset)
            while True:
                done = self.finished()
                chunk = f.read()
                if chunk:
                    out.write(chunk)
                    out.flush()
                elif done:
                    time.sleep(0.2)  # pipe-pane's cat may still be flushing
                    out.write(f.read())
                    out.flush()
                    return
                else:
                    time.sleep(0.1)

    def watch_exit(self, notify, keep):
        """When the command ends: publish its state for the sidebar's home
        entry. Then send everyone attached back out, so lte-session finishes;
        with 'keep', stay, and with 'notify' (someone is attached) say so."""
        def watch():
            while not self.finished():
                time.sleep(0.2)
            if not self.alive():
                return
            state = self.describe_exit() if self.exit_msg() else "unknown"
            _tmux("set-option", "-q", "-t", self.session, dash.HOME_STATUS_OPTION, state)
            if not keep:
                self.leave()
            elif notify:
                _tmux("display-message", "-d", "10000", "-t", self.home,
                      f"{self.label} {state}. Detach (prefix d) to finish.")
        threading.Thread(target=watch, daemon=True).start()

    def leave(self):
        """Every client out of the session: the one that came from another
        session goes back there, the rest detach. Killing the session instead
        would detach them all (detach-on-destroy)."""
        for client in _tmux("list-clients", "-t", self.session, "-F", "#{client_name}"):
            client = client.strip()
            if self.return_to and client == self.return_to[0]:
                try:
                    _tmux("switch-client", "-c", client, "-t", self.return_to[1], check=True)
                    continue
                except RuntimeError:
                    pass  # that session is gone
            _tmux("detach-client", "-t", client)

    def attached(self):
        out = _tmux("display-message", "-p", "-t", self.session, "#{session_attached}")
        return bool(out and out[0].strip() not in ("", "0"))

    def kill(self):
        _tmux("kill-session", "-t", self.session)
        self.give_back_key()

    def give_back_key(self):
        if self.close_key:
            keys.give_back(self.close_key)
            self.close_key = ""


def _interrupt(launch):
    """Ctrl-C here (when following, not attached): pass it to the command,
    the way a Ctrl-C typed in its pane would. A second one gives up."""
    def handler(signum, frame):
        signal.signal(signal.SIGINT, signal.default_int_handler)
        _tmux("send-keys", "-t", launch.home, "C-c")
        sys.stderr.write("\nlte-session: sent Ctrl-C to the command; again to kill the session\n")
    signal.signal(signal.SIGINT, handler)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["--run"]:
        if len(argv) < 4 or argv[2] != "--":
            sys.stderr.write("usage: lte-session --run DIR -- COMMAND [ARG...]\n")
            return 2
        return run(argv[1], argv[3:])

    parser = argparse.ArgumentParser(
        prog="lte-session", description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 2)[2])
    parser.add_argument("--name", help="session name (default: lte_<command>_<pid>)")
    parser.add_argument("--keep", action="store_true",
                        help="stay in the session after the command ends, and leave "
                             "it running at the end")
    parser.add_argument("--width", default=dash.AUTO,
                        help="sidebar columns, N%%, or auto (fit the panes listed at start)")
    parser.add_argument("--no-dash", action="store_true", help="no sidebar, just the home pane")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no command given")

    try:
        launch = Launch(command, name=args.name, width=args.width, dash_on=not args.no_dash)
    except RuntimeError as exc:
        sys.stderr.write(f"lte-session: {exc}\n")
        return 1
    signal.signal(signal.SIGTERM, lambda *_: (launch.kill(), sys.exit(143)))

    try:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        if interactive and os.environ.get("TMUX"):
            out = _tmux("display-message", "-p", "-t", os.environ["TMUX_PANE"],
                        "#{client_name} #{session_id}")
            if out and len(out[0].split()) == 2:
                launch.return_to = tuple(out[0].split())
        launch.watch_exit(notify=interactive, keep=args.keep)
        if interactive and not os.environ.get("TMUX"):
            subprocess.run(["tmux", "attach-session", "-t", launch.session])
            text = launch.capture() if launch.alive() else None
            if text is None:
                launch.follow(0)  # pane gone: the transcript is all there is
            else:
                sys.stdout.write(text)
                sys.stdout.flush()
                if not launch.finished():
                    sys.stderr.write(f"lte-session: detached, {launch.label} still running. "
                                     f"Following its output; tmux attach -t {launch.name} "
                                     f"to interact.\n")
                    _interrupt(launch)
                    launch.follow(os.path.getsize(launch.transcript))
        else:
            if interactive:  # inside tmux: no nesting, switch this client instead
                client = launch.return_to[0] if launch.return_to else None
                _tmux("switch-client", *(["-c", client] if client else []),
                      "-t", launch.session)
                sys.stderr.write(f"lte-session: switched to {launch.name}. Its output "
                                 f"follows here too.\n")
            _interrupt(launch)
            launch.follow(0)
            if interactive and launch.alive() and launch.attached():
                # Killing it now would detach that client (detach-on-destroy).
                if args.keep:
                    sys.stderr.write(f"lte-session: {launch.label} {launch.describe_exit()}. "
                                     f"Finishing once you leave {launch.name}.\n")
                while launch.alive() and launch.attached():
                    time.sleep(0.3)
    except KeyboardInterrupt:
        launch.kill()
        sys.stderr.write("lte-session: killed the session\n")
        return 130

    code = launch.exit_code()
    if args.keep:
        # The kept session's sidebar still reads the status files in it.
        sys.stderr.write(f"lte-session: kept session {launch.name} "
                         f"(and {launch.run_dir})\n")
    else:
        launch.kill()
        shutil.rmtree(launch.run_dir, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
