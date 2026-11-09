"""Processes: start a group of processes, watch them, stop them -- no pytest.

The engine under the pytest plugin (testing.py), usable by any program:

    from libtmux_easy.processes import Processes

    procs = Processes("bringup", fail_on={"*": r"Traceback"})
    procs.start("sim:server", "./server", ready=r"listening")
    procs.start("sim:client", "./client")
    procs.wait_until(server_answers)
    ...
    procs.close()          # SIGINT all at once, then SIGTERM, SIGKILL

A death (an exit nobody asked for, or a fail_on line) is recorded; check()
raises ProcessFailure for it, and every wait calls check(). on_exit=
hears each exit as it happens. Where processes run: subprocesses
(default), or panes of a TmuxSession (tmux=session).

Reference: docs/processes.md.
"""

import contextlib
import fnmatch
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

from .output import PaneExited, file_size
from .status import ExitStatus, PaneStatus
from .tmux_lib import _SNAPSHOT_SEP, HEADLESS_ENV, PANE_MODE_WRAP, _pane_cmd
from .proc import SHELLS, alive, cmdline, comm, proc_table
from .wrap import (PANE_MODE_OPTION, PANE_NAME_OPTION, PIPE_FILE_OPTION, PIPE_FROM_OPTION,
                   PIPE_TEE_OPTION, PIPE_WHOLE_OPTION,
                   STATUS_DIR_ENV,
                   plain_line, plain_text)


class ProcessFailure(AssertionError):
    """A process died, printed a fail_on line, or did not stop cleanly. An
    AssertionError, so a test that gets one fails as for an assert."""


# ---------- backends ----------

def _unique_prefix(session, prefix):
    """'prefix', or 'prefix#N' if a window '<prefix>/...' exists already:
    another run in the same session, or windows a past run kept. "" stays
    "": bare window names."""
    if not prefix:
        return ""
    names = [w.name for w in session.windows]
    candidate, n = prefix, 1
    while any(name.startswith(candidate + "/") for name in names):
        n += 1
        candidate = f"{prefix}#{n}"
    return candidate


class _TmuxBackend:
    """Each process in a wrap-mode pane; one window per 'window' group, named
    '<prefix>/<window>' so classes sharing the session don't clash. All
    groups of a class share one. A restart gets a new pane, '<name>~N',
    next to the old one's output."""

    def __init__(self, session, prefix):
        self.session = session
        self.prefix = _unique_prefix(session, prefix)
        self.panes = {}  # name -> Pane
        self.runs = {}  # name -> how many times started
        self.logs = {}  # name -> its log, which each start rewrites (cmd_capture)
        self.windows = set()

    def window(self, window, setup, **kwargs):
        """Make the window '<prefix>/<window>', or go to it: new_pane() goes in
        it. Returns it."""
        full_window = self.full(window)
        if window not in self.windows:  # this class's: its groups share this backend
            w = self.session.new_window(full_window, **kwargs)
            self.session.send_command_before(setup, window=full_window)
            self.windows.add(window)
        else:  # don't move the viewer
            w = self.session.current_window = self.session.resolve_target_window(full_window)
            self.session.current_pane = w.panes[-1]
        return w

    def start(self, name, command, setup, on_exit, on_line=None, log=None, env=None):
        window, _, pane_name = name.partition(":")
        self.window(window, setup)
        self.runs[name] = self.runs.get(name, 0) + 1
        if self.runs[name] > 1:
            pane_name = f"{pane_name}~{self.runs[name]}"
        on_output = {_EVERY_LINE: lambda m: on_line(m.line)} if on_line else None
        env = dict(env or {}, **({"LTE_LOG_DIR": os.path.dirname(log)} if log else {})) or None
        if log:
            self.logs[name] = log
        self.panes[name] = self.session.new_pane(pane_name, command, mode=PANE_MODE_WRAP,
                                                 on_exit=lambda s: on_exit(name, s),
                                                 on_output=on_output, cmd_capture=log,
                                                 env=env)

    def full(self, window):
        """The tmux window name of group window 'window'."""
        return f"{self.prefix}/{window}" if self.prefix else window

    def short(self, window):
        """full()'s inverse."""
        return window.partition("/")[2] if self.prefix else window

    def register(self, name, pane, log):
        """A pane build() made, as 'name'."""
        self.panes[name] = pane
        self.runs[name] = 1
        self.logs[name] = log

    def wait_for(self, name, regex, timeout):
        """The first re.Match in the output; TimeoutError, or PaneExited."""
        return self.session.wait_for_output(self.panes[name], regex, timeout).match

    def output(self, name):
        return "\n".join(self.lines(name))

    def lines(self, name):
        """What the latest start printed: from its log (a cmd_capture, or a
        pane_capture), else the pane, which also shows the shell's prompt and the
        command."""
        if name not in self.logs:
            return _pane_cmd(self.panes[name], "capture-pane", "-p", "-J", "-S", "-").stdout
        return _read_plain(self.logs[name])

    def kill(self, name):
        self.session.wait_started(self.panes[name], timeout=5.0)  # its pid, once known
        _kill(self.status(name).target_pids)

    def stop(self, name, sigint_timeout, sigterm_timeout=5.0):
        self.session.stop_pane(self.panes[name], sigint_timeout=sigint_timeout,
                               sigterm_timeout=sigterm_timeout)

    def stop_all(self, names, timeouts):
        _parallel(self.stop, [n for n in names if self.session.is_running(self.panes[n])],
                  timeouts)

    def status(self, name):
        return self.session.pane_status(self.panes[name])


_EVERY_LINE = re.compile("")


def _read_plain(path):
    """A log's lines as plain text (a capture keeps terminal escapes)."""
    try:
        with open(path, "rb") as f:
            return plain_text(f.read()).decode(errors="replace").splitlines()
    except FileNotFoundError:
        return []


def _kill(pids):
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def _parallel(fn, names, timeouts):
    """fn(name, *timeouts[name]) for every name at once: timeouts[name] is
    (sigint_timeout, sigterm_timeout), each process on its own deadlines."""
    threads = [threading.Thread(target=fn, args=(name, *timeouts[name])) for name in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


class _Subprocess:
    def __init__(self, name, command, on_exit, tty, lock, on_line=None, log=None, env=None):
        self.name = name
        self.command = command
        self.lines = []
        self.plain = []  # the lines as plain text, for matching
        self.cond = threading.Condition()
        self.done_reading = False
        self.stopped = None
        # stdin is a pty, as in a terminal. Some programs act on that: 'ros2
        # launch' with a non-tty stdin re-sends SIGINT to children that
        # already got the group's, and Python children die of the second.
        tty_in, stdin = os.openpty()
        # Without BASH_ENV, as for panes (see _tmux.SKIP_ENV).
        env = {k: v for k, v in dict(os.environ, **(env or {})).items()
               if k != "BASH_ENV" and v is not None}
        self.proc = subprocess.Popen(
            ["bash", "-c", command], stdin=stdin, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace",
            start_new_session=True,  # a process group of its own, for SIGINT
            env=dict(env, PYTHONUNBUFFERED="1",
                     **({"LTE_LOG_DIR": os.path.dirname(log)} if log else {})))
        os.close(stdin)
        log_file = open(log, "a", buffering=1) if log else None  # line-buffered: tail -f

        def read():
            for line in self.proc.stdout:
                plain = plain_line(line.rstrip("\n").encode()).decode()
                if log_file:
                    log_file.write(plain + "\n")
                with self.cond:
                    self.lines.append(line)
                    self.plain.append(plain)
                    self.cond.notify_all()
                if tty:
                    with lock:
                        tty.write(f"[{name}] {line}")
                if on_line:
                    on_line(plain)
            if log_file:
                log_file.close()
            with self.cond:
                self.done_reading = True
                self.cond.notify_all()

        def wait():
            self.proc.wait()
            os.close(tty_in)
            on_exit(name, self.status())

        threading.Thread(target=read, daemon=True).start()
        self.waiter = threading.Thread(target=wait, daemon=True)
        self.waiter.start()

    def status(self):
        rc = self.proc.poll()
        exit_status = (ExitStatus() if rc is None else
                       ExitStatus(signal=-rc) if rc < 0 else ExitStatus(code=rc))
        return PaneStatus(name=self.name, pane_id="", mode="subprocess", command=self.command,
                          target_pids=(self.proc.pid,), running=rc is None,
                          exit_status=exit_status, stopped=self.stopped)

    def wait_for(self, regex, timeout):
        """The first re.Match in the output; TimeoutError, or PaneExited."""
        seen = 0
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for line in self.plain[seen:]:
                    m = regex.search(line)
                    if m:
                        return m
                seen = len(self.plain)
                if self.done_reading:
                    self.proc.wait()
                    raise PaneExited(self.status())
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"{self.name}: no line matching {regex.pattern!r}")
                self.cond.wait(left)

    def stop(self, sigint_timeout, sigterm_timeout=5.0):
        """SIGINT the group (a Ctrl-C), escalating like stop_pane()."""
        for sig, timeout in ((signal.SIGINT, sigint_timeout), (signal.SIGTERM, sigterm_timeout),
                             (signal.SIGKILL, 2)):
            if self.proc.poll() is not None:
                break
            self.stopped = sig.name
            try:
                os.killpg(self.proc.pid, sig)
                self.proc.wait(timeout)
            except ProcessLookupError:
                break
            except subprocess.TimeoutExpired:
                pass
        self.waiter.join(1)  # on_exit has fired

    def kill(self):
        with contextlib.suppress(OSError):
            os.killpg(self.proc.pid, signal.SIGKILL)


class _SubprocessBackend:
    """Plain subprocesses, their output in their logs; also on 'tty' for
    names matching an 'echo' pattern. A restart replaces the old one."""

    def __init__(self, tty, echo):
        self.tty = tty
        self.echo = echo
        self.lock = threading.Lock()
        self.procs = {}

    def start(self, name, command, setup, on_exit, on_line=None, log=None, env=None):
        if setup:
            command = f"{setup} && {command}"
        tty = self.tty if any(fnmatch.fnmatchcase(name, p) for p in self.echo) else None
        self.procs[name] = _Subprocess(name, command, on_exit, tty, self.lock, on_line, log, env)

    def wait_for(self, name, regex, timeout):
        return self.procs[name].wait_for(regex, timeout)

    def output(self, name):
        return "".join(self.procs[name].lines)

    def lines(self, name):
        return list(self.procs[name].plain)

    def stop(self, name, sigint_timeout, sigterm_timeout=5.0):
        self.procs[name].stop(sigint_timeout, sigterm_timeout)

    def stop_all(self, names, timeouts):
        _parallel(self.stop, names, timeouts)

    def status(self, name):
        return self.procs[name].status()

    def kill(self, name):
        self.procs[name].kill()


class _AdoptedBackend:
    """adopt(): a session script, run as it is, and the panes it made, on a
    tmux server of their own; watched from outside. How a pane's run ended:
    lte-run's status file (wrap mode), tmux's dead-pane status (exec mode),
    else only that it ended (shell mode, or plain tmux). Output: what the
    pane shows already, then a pipe; or the file the script pipes it to."""

    POLL = 0.2

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="lte_tmux_", dir="/tmp")  # short: a socket path
        self.socket = os.path.join(self.dir, f"tmux-{os.getuid()}", "default")
        self.runs = os.path.join(self.dir, "runs")  # lte-run's <pane id>.json
        os.mkdir(self.runs)
        self.script = {}  # name -> _Subprocess: the script
        self.panes = {}  # name -> _AdoptedPane
        self.cond = threading.Condition()
        self.done = threading.Event()
        self.thread = None

    def env(self):
        """For the script: this server, not the one it may run under; no
        attach() or dashboard()."""
        return {"TMUX": None, "TMUX_PANE": None, "TMUX_TMPDIR": self.dir, HEADLESS_ENV: "1",
                STATUS_DIR_ENV: self.runs}

    def _tmux(self, *args):
        return subprocess.run(["tmux", "-S", self.socket, *args], capture_output=True,
                              text=True, errors="replace").stdout.splitlines()

    def run_script(self, name, command, on_exit, on_line, log):
        self.script[name] = _Subprocess(name, command, on_exit, None, threading.Lock(),
                                        on_line, log, env=self.env())

    def discover(self):
        """[(name, pane id, mode)] of every pane on the server: 'window:pane'
        by new_pane()'s name, else by index; 'session.window' if several.
        A pane not made by new_pane(): 'shell' if it runs an interactive
        shell, else 'exec' (tmux ran the command: the pane's process is it);
        an idle shell (a prompt, nothing running) is left out."""
        fmt = "\t".join(("#{pane_id}", "#{session_name}", "#{window_name}", "#{pane_index}",
                         f"#{{{PANE_NAME_OPTION}}}", f"#{{{PANE_MODE_OPTION}}}", "#{pane_pid}"))
        rows = [line.split("\t") for line in self._tmux("list-panes", "-a", "-F", fmt)]
        several = len({r[1] for r in rows}) > 1
        table = proc_table()
        found = []
        for pane_id, session, window, index, name, mode, pid in rows:
            full = f"{session}.{window}:{name or index}" if several else f"{window}:{name or index}"
            while full in [f[0] for f in found]:
                full += "+"
            if not mode:
                shell = comm(pid) in SHELLS and "-c" not in cmdline(pid).split()
                if shell and not any(ppid == int(pid) for _, ppid, _ in table.values()):
                    continue
                mode = "shell" if shell else "exec"
            found.append((full, pane_id, mode))
        return found

    def watch(self, name, pane_id, mode, on_exit, on_line, log):
        piped = self._tmux("display-message", "-p", "-t", pane_id,
                           "\t".join(("#{pane_pipe}", f"#{{{PIPE_FILE_OPTION}}}",
                                      f"#{{{PIPE_FROM_OPTION}}}", f"#{{{PIPE_WHOLE_OPTION}}}",
                                      f"#{{{PIPE_TEE_OPTION}}}")))
        piped, theirs, start, whole, tee = ((piped[0] if piped else "") + "\t" * 4).split("\t")[:5]
        history = []
        if piped == "1" and theirs and (whole or tee):
            # The script's capture from the pane's first byte, or lte-run's
            # file for the run: read that, from where this pipe began (it appends).
            source, offset = theirs, int(start or 0)
        elif piped == "1" and theirs:
            # Piped late (a watch of the script's): the scrollback, then the
            # file from here. A line in flight to it may come twice, not never.
            history = self._tmux("capture-pane", "-p", "-J", "-S", "-", "-t", pane_id)
            source, offset = theirs, file_size(theirs)
        else:
            # The scrollback and the pipe in one tmux call: tmux reads no pane
            # output between them, so no line is in neither. The pipe holds
            # off (tmux buffers) until the log, starting with the scrollback,
            # exists.
            with contextlib.suppress(FileNotFoundError):
                os.remove(log)
            q = shlex.quote(log)
            out = self._tmux("capture-pane", "-p", "-J", "-S", "-", "-t", pane_id, ";",
                             "display", "-p", "-t", pane_id, _SNAPSHOT_SEP, ";",
                             "pipe-pane", "-o", "-t", pane_id,
                             f"until [ -e {q} ]; do sleep 0.01; done; exec cat >> {q}")
            end = len(out) - 1 - out[::-1].index(_SNAPSHOT_SEP) if _SNAPSHOT_SEP in out else len(out)
            with open(log + ".tmp", "w") as f:
                f.write("".join(line + "\n" for line in _strip_blank_tail(out[:end])))
            os.replace(log + ".tmp", log)
            source, offset = log, 0
        self.panes[name] = _AdoptedPane(name, pane_id, mode, source, on_exit, on_line, offset,
                                        copy=None if source == log else log,
                                        history=_strip_blank_tail(history))
        if self.thread is None:
            self.thread = threading.Thread(target=self._poll, daemon=True)
            self.thread.start()

    def _poll(self):
        while not self.done.wait(self.POLL):
            self.poll()

    def poll(self):
        """Read new output; notice exits."""
        fmt = "\t".join(("#{pane_id}", "#{pane_pid}", "#{pane_dead}", "#{pane_dead_status}",
                         "#{pane_dead_signal}"))
        rows = {r[0]: r for r in (line.split("\t") for line in self._tmux("list-panes", "-a", "-F", fmt))}
        table = proc_table()
        for pane in list(self.panes.values()):
            pane.read()
            pane.update(rows.get(pane.pane_id), self._run(pane.pane_id), table)
        with self.cond:
            self.cond.notify_all()

    def _run(self, pane_id):
        """lte-run's record of the pane's latest run, or None."""
        try:
            with open(os.path.join(self.runs, pane_id + ".json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _get(self, name):
        return self.script.get(name) or self.panes[name]

    def wait_for(self, name, regex, timeout):
        if name in self.script:
            return self.script[name].wait_for(regex, timeout)
        pane = self.panes[name]
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                m = next((m for line in pane.lines for m in [regex.search(line)] if m), None)
                if m:
                    return m
                if pane.exited and pane.read_after_exit:
                    raise PaneExited(pane.status())
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"{name}: no line matching {regex.pattern!r}")
                self.cond.wait(left)

    def output(self, name):
        return "\n".join(self.lines(name))

    def lines(self, name):
        if name in self.script:
            return list(self.script[name].plain)
        return list(self.panes[name].lines)

    def status(self, name):
        return self._get(name).status()

    def stop(self, name, sigint_timeout, sigterm_timeout=5.0):
        if name in self.script:
            return self.script[name].stop(sigint_timeout, sigterm_timeout)
        pane = self.panes[name]
        self._tmux("send-keys", "-t", pane.pane_id, "C-c")  # as a person would
        pane.stopped = "SIGINT"
        for sig, timeout in ((None, sigint_timeout), (signal.SIGTERM, sigterm_timeout),
                             (signal.SIGKILL, 2)):
            if sig is not None:
                pane.stopped = sig.name
                for pid in pane.targets():
                    with contextlib.suppress(OSError):
                        os.kill(pid, sig)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not pane.exited:
                time.sleep(0.05)
            if pane.exited:
                return

    def stop_all(self, names, timeouts):
        _parallel(self.stop, [n for n in names if self.status(n).running], timeouts)

    def kill(self, name):
        if name in self.script:
            return self.script[name].kill()
        _kill(self.panes[name].targets())

    def start(self, *args):
        raise ValueError("start(): these are the script's panes; it can't be started again")

    def close(self, keep):
        """Stop watching; kill the server, unless 'keep' (to attach and look)."""
        self.done.set()
        if not keep:
            self._tmux("kill-server")
            shutil.rmtree(self.dir, ignore_errors=True)


def _strip_blank_tail(lines):
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    return lines


class _AdoptedPane:
    """One adopted pane: its output so far, and how its run ended."""

    def __init__(self, name, pane_id, mode, source, on_exit, on_line, offset=0, copy=None,
                 history=()):
        self.name = name
        self.pane_id = pane_id
        self.mode = mode
        self.source = source
        self.on_exit = on_exit
        self.on_line = on_line
        self.lines = []
        self.offset = offset
        self.copy = copy  # the group's log, when 'source' is the script's file
        if copy is not None:
            open(copy, "w").close()
        self.partial = b""
        self.exited = False
        self.read_after_exit = False
        self.exit_status = ExitStatus()
        self.stopped = None
        self.pids = ()  # what runs it: lte-run's target, the pane's process, or the shell's children
        self.unset = 0  # polls that saw it dead, with no status yet
        self._take([plain_line(line.encode()).decode(errors="replace") for line in history])

    def read(self):
        try:
            with open(self.source, "rb") as f:
                f.seek(self.offset)
                data = f.read()
        except FileNotFoundError:
            return
        self.offset += len(data)
        *complete, self.partial = (self.partial + data).split(b"\n")
        self._take([plain_line(raw).decode(errors="replace") for raw in complete])
        if self.exited:
            self.read_after_exit = True

    def _take(self, lines):
        if self.copy is not None and lines:
            with open(self.copy, "a") as f:
                f.write("".join(line + "\n" for line in lines))
        for line in lines:
            self.lines.append(line)
            if self.on_line:
                self.on_line(line)

    def update(self, row, run, table):
        if self.exited:
            return
        if run is not None:  # lte-run's record: exact
            self.pids = (run["pid"],)
            ended = "code" in run
            running = not ended and alive(run["pid"])
            status = ExitStatus(code=run.get("code"), signal=run.get("signal"))
        elif row is None:  # the pane is gone
            running, status = False, ExitStatus()
        else:
            _, pane_pid, dead, dead_status, dead_signal = row
            if dead == "1" or self.mode == "exec":
                self.pids = (int(pane_pid),)
                if dead == "1" and not (dead_status or dead_signal) and self.unset < 10:
                    self.unset += 1  # tmux marks it dead a moment before the status
                    return
                running = dead != "1"
                status = (ExitStatus(signal=int(dead_signal)) if dead_signal else
                          ExitStatus(code=int(dead_status)) if dead_status else ExitStatus())
            else:  # a shell: running while it has children; how it ended is not known
                children = [p for p, (state, ppid, _) in table.items()
                            if ppid == int(pane_pid) and state != "Z"]
                self.pids = tuple(children)
                running, status = bool(children), ExitStatus()
        if not running:
            self.exit_status = status
            self.exited = True
            self.read()
            self.on_exit(self.name, self.status())

    def targets(self):
        return self.pids

    def status(self):
        return PaneStatus(name=self.name, pane_id=self.pane_id, mode=self.mode, command="",
                          target_pids=self.pids, running=not self.exited,
                          exit_status=self.exit_status, stopped=self.stopped)


# ---------- processes ----------

class _Run:
    """One start of a process: whether its exit is expected, and when it came."""

    def __init__(self, exits=None, stop_codes=(0,)):
        self.exits = exits  # codes it may finish with on its own; None: it must not
        self.stop_codes = stop_codes  # codes it may exit with when stopped
        self.stopping = False
        self.died = False
        self.finished = False  # exited on its own, with a code in 'exits'
        self.exited = threading.Event()


def _codes(value):
    """An exit code, or several, as a tuple; None stays None."""
    if value is None:
        return None
    return (value,) if isinstance(value, int) else tuple(value)


def _exit_code(status):
    """The exit code, a signal as the shell's 128+N (SIGINT: 130)."""
    e = status.exit_status
    return e.code if e.code is not None else None if e.signal is None else 128 + e.signal


class Process:
    """One process of a group, as start() returns it: the group's methods
    for its name, and start() to start it again. It follows the name: still
    valid after a stop() and a start()."""

    def __init__(self, group, name):
        self.group = group
        self.name = name

    def __repr__(self):
        return f"<Process {self.name}>"

    def start(self):
        """Start it again: the same command and start() options."""
        if self.group._adopted:
            raise ValueError(f"start(): {self.name} is the script's pane; it can't be started again")
        command, options = self.group._commands[self.name]
        return self.group.start(self.name, command, **options)

    def stop(self, sigint_timeout=None, check=True, sigterm_timeout=None) -> PaneStatus:
        return self.group.stop(self.name, sigint_timeout, check, sigterm_timeout)

    def kill(self):
        """SIGKILL it: a death, unless in an expect_exit() block."""
        self.group.kill(self.name)

    def wait_for(self, pattern, timeout=10.0) -> "re.Match":
        return self.group.wait_for(self.name, pattern, timeout)

    def wait_for_in_order(self, patterns, timeout=10.0) -> list:
        return self.group.wait_for_in_order(self.name, patterns, timeout)

    def wait_for_exit(self, timeout=10.0) -> PaneStatus:
        return self.group.wait_for_exit(self.name, timeout)

    def status(self) -> PaneStatus:
        return self.group.status(self.name)

    def log(self):
        return self.group.log(self.name)

    def output(self) -> str:
        return self.group.output(self.name)


class _BuildSession:
    """The session a build() function gets: the run's TmuxSession, as the
    script knows it. Names are the script's ('sim', 'sim:gazebo'); its
    windows are '<Class>/<window>'. Each new_pane() is also a process of
    the group, with a log; panes default to wrap mode. send_command_before(),
    copy_environment() and set_default_mode() apply to its own windows and
    panes only. attach(), dashboard() and the kill/close calls do nothing."""

    # Methods whose first argument (or a keyword below) names a pane or window.
    _TARGETED = {"select_window", "select_pane", "resolve_target_pane", "resolve_target_window",
                 "is_running", "exit_status", "wait_for_pane_idle", "stop_pane", "respawn_pane",
                 "watch_pane", "unwatch_pane", "wait_for_exit", "capture", "output_path",
                 "output_mark", "search_output", "output_text", "watch_output", "unwatch_output",
                 "wait_for_output", "expect", "pane_status", "get_pane", "get_window"}
    _TARGET_KEYWORDS = ("target", "target_pane", "target_window", "identifier", "window")

    def __init__(self, group, exits):
        self.processes = group  # the group its panes join: start() more there
        self._group = group
        self._exits = exits
        self._backend = group.backend
        self._session = group.backend.session
        self._mode = PANE_MODE_WRAP
        self._before = None  # send_command_before() without a window
        self._env = {}
        self._windows = []  # the windows build() made, short names

    def _full(self, target):
        if isinstance(target, str) and target and target[0] not in "%@":
            return self._backend.full(target)
        if isinstance(target, (list, tuple)):
            return type(target)(self._full(t) for t in target)
        return target

    def __getattr__(self, attr):
        value = getattr(self._session, attr)
        if attr not in self._TARGETED and attr != "send_keys":
            return value

        def call(*args, **kwargs):
            if args and attr != "send_keys":  # send_keys(keys, target_pane=...)
                args = (self._full(args[0]),) + args[1:]
            kwargs = {k: self._full(v) if k in self._TARGET_KEYWORDS else v
                      for k, v in kwargs.items()}
            return value(*args, **kwargs)
        return call

    def _setup(self):
        setup = self._group.setup
        return " && ".join(c for c in (setup, self._before) if c) or None

    def new_window(self, name, **kwargs):
        window = self._backend.window(name, self._setup(), **kwargs)
        if name not in self._windows:
            self._windows.append(name)
        return window

    def new_pane(self, name, cmd, **kwargs):
        group = self._group
        window = self._backend.short(self._session.current_window.name)
        full = f"{window}:{name}"
        on_exit, on_line = group._begin(full, cmd, exits=self._exits.get(full))
        group._commands[full] = (cmd, {"exits": self._exits.get(full)})
        mode = kwargs.pop("mode", None) or self._mode
        log = kwargs.get("cmd_capture") or kwargs.get("pane_capture") or group.log(full)
        if mode == PANE_MODE_WRAP:
            kwargs.setdefault("cmd_capture", log)
        else:  # cmd_capture is lte-run's; a pane capture keeps escapes, read as plain text
            kwargs.setdefault("pane_capture", log)
        theirs = kwargs.pop("on_exit", None)

        def exited(status):
            on_exit(full, status)
            if theirs is not None:
                theirs(status)
        on_output = dict(kwargs.pop("on_output", None) or {})
        if on_line is not None:
            on_output[_EVERY_LINE] = lambda m: on_line(m.line)
        env = {**self._env, **(kwargs.pop("env", None) or {}), "LTE_LOG_DIR": os.path.dirname(log)}
        pane = self._session.new_pane(name, cmd, mode=mode, on_exit=exited,
                                      on_output=on_output or None, env=env, **kwargs)
        self._backend.register(full, pane, log)
        return pane

    def send_command_before(self, command, window=None):
        if window is not None:
            command = " && ".join(c for c in (self._group.setup, command) if c)
            return self._session.send_command_before(command, window=self._full(window))
        self._before = command
        for name in self._windows:
            self._session.send_command_before(self._setup(), window=self._full(name))

    def copy_environment(self, env=None):
        self._env.update(os.environ if env is None else env)

    def set_default_mode(self, mode):
        self._mode = mode
        return mode

    def _nothing(self, *args, **kwargs):
        return None

    attach = dashboard = close_dashboard = kill = kill_session = close = release = _nothing


class Processes:
    """A group of processes and their health, in any mode.

        procs = Processes("bringup", fail_on={"*": r"Traceback"})
        procs.start("sim:server", "./server", ready=r"listening")
        ...
        procs.close()

    'name': the group's; its log dir and tmux window prefix by default.
    'tmux': a TmuxSession: each process in a wrap-mode pane, in windows
    '<prefix>/<window>' ('prefix' default 'name'; "" for bare window
    names). None: subprocesses, their output in their logs, and on 'tty'
    (default stdout) for names matching an 'echo' pattern ('*': all).
    'fail_on': {name or "*": a regex, a function line -> problem or None,
    or a list of them}; a match is a death.
    'stop_codes': {name or "*": codes} allowed when stopped (default 0; a
    signal N is 128+N, so 130 allows dying of SIGINT).
    'sigint_timeout': seconds SIGINT gets before SIGTERM; 'sigterm_timeout':
    seconds SIGTERM then gets before SIGKILL (start() can set either per
    process).
    'log_dir': where the logs go; default a new temp dir.
    'setup': a shell command run before each process.
    'on_exit': called as on_exit(name, PaneStatus, died) on each exit, from
    a watcher thread; died is True for an exit nobody asked for.
    'keep_open': close() stops nothing.
    'scope': groups with the same scope share names (default: this one).
    """

    def __init__(self, name="processes", *, tmux=None, prefix=None, log_dir=None, echo=(),
                 tty=None, setup=None, fail_on=None, stop_codes=None, sigint_timeout=10.0,
                 on_abort=None, on_exit=None, keep_open=False, scope=None,
                 sigterm_timeout=5.0):
        prefix = name if prefix is None else prefix
        if log_dir is None:
            log_dir = tempfile.mkdtemp(prefix=re.sub(r"[^\w-]", "_", name) + "-")
        os.makedirs(log_dir, exist_ok=True)
        if isinstance(tmux, _TmuxBackend):  # shared by several groups
            self.backend = tmux
        elif tmux is not None:
            self.backend = _TmuxBackend(tmux, prefix)
        else:
            self.backend = _SubprocessBackend(tty if tty is not None else sys.stdout,
                                              list(echo))
        self.scope = self if scope is None else scope
        self.owner = None
        self.prefix = prefix
        self.log_dir = str(log_dir)
        self.keep_open = keep_open
        self.setup = setup
        self.fail_on = {n: [p] if isinstance(p, str) or callable(p) else list(p)
                        for n, p in (fail_on or {}).items()}
        self.stop_codes = dict(stop_codes or {})
        self.sigint_timeout = sigint_timeout
        self.sigterm_timeout = sigterm_timeout
        self.on_abort = on_abort
        self.on_exit = on_exit
        self.names = []
        self.died = []  # (name, PaneStatus, _Run) of each exit nobody asked for
        self.failed_on = []  # (problem, during a stop) of each fail_on hit
        self.stopping = False
        self._aborted = None  # why a failed test stopped everything
        self._runs = {}  # name -> _Run of its latest start
        self._commands = {}  # name -> (command, start()'s options): for Process.start()
        self._sigint_timeouts = {}  # name -> its own sigint_timeout, from start()
        self._sigterm_timeouts = {}  # name -> its own sigterm_timeout, from start()
        self._adopted = False  # see adopt()
        self.session = None  # see build()
        self._raised_died = self._raised_failed_on = 0  # how many check() raised
        _open.append(self)

    def _fail(self, message):
        raise ProcessFailure(message)

    def _tmux_for_build(self):
        """The _TmuxBackend build() uses when the group has none."""
        raise ValueError("build(): needs a tmux session: Processes(..., tmux=TmuxSession)")

    def _kept_server(self, socket):
        """adopt()'s tmux server, kept to look at."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:  # else the test failed: the plugin stops it, after on_abort()
            self.close()

    def start(self, name, command, ready=None, timeout=30.0, fail_on=(), exits=None,
              stop_codes=None, env=None, sigint_timeout=None,
              sigterm_timeout=None) -> Process:
        """Start 'command' as 'window:name' (a name without ':' goes in a
        window of its own). With 'ready' (a regex), wait up to 'timeout' s
        for a line matching it, as wait_for() does. 'fail_on': more fail_on
        patterns for it. 'exits': a code (or several) it finishes with on
        its own, so that exit is no death; wait_for_exit() waits for it.
        'stop_codes': the codes it may exit with when stopped (default: the
        group's). 'env': variables to set (None: unset) on top of this
        process's environment. 'sigint_timeout': seconds SIGINT gets
        before SIGTERM when it is stopped, 'sigterm_timeout' seconds SIGTERM
        gets before SIGKILL (defaults: the group's). A stopped
        name can be started again. Returns its Process."""
        if self._adopted:
            raise ValueError("start(): this group adopted a script's session; "
                             "start more in another Processes")
        name = self._name(name)
        on_exit, on_line = self._begin(name, command, fail_on, exits, stop_codes)
        self._commands[name] = (command, dict(ready=ready, timeout=timeout, fail_on=fail_on,
                                               exits=exits, stop_codes=stop_codes, env=env,
                                               sigint_timeout=sigint_timeout,
                                               sigterm_timeout=sigterm_timeout))
        self._sigint_timeouts[name] = sigint_timeout
        self._sigterm_timeouts[name] = sigterm_timeout
        self.backend.start(name, command, self.setup, on_exit, on_line, self.log(name), env)
        if ready is not None:
            self.wait_for(name, ready, timeout)
        return Process(self, name)

    def _begin(self, name, command, fail_on=(), exits=None, stop_codes=None):
        """A new run of 'name': checks, bookkeeping. Returns the backend's
        (on_exit, on_line) for it."""
        if name in self._runs and self.status(name).running:
            raise ValueError(f"{name} is already running")
        if any(name in p._runs for p in _open if p is not self and p.scope is self.scope):
            raise ValueError(f"{name} is taken: another open Processes of {self.prefix} has it")
        patterns = self.fail_on.get("*", []) + self.fail_on.get(name, [])
        patterns += [fail_on] if isinstance(fail_on, str) or callable(fail_on) else list(fail_on)
        if stop_codes is None:
            stop_codes = self.stop_codes.get(name, self.stop_codes.get("*", 0))
        run = self._runs[name] = _Run(_codes(exits), _codes(stop_codes))
        if name not in self.names:
            self.names.append(name)
        return (lambda n, status: self._on_exit(n, status, run),
                self._on_line(name, [p if callable(p) else re.compile(p) for p in patterns]))

    def build(self, build, *args, exits=None, **kwargs):
        """Run 'build(session, *args, **kwargs)': a session script's function
        that makes its windows and panes. Each pane it makes is a process
        of this group, named 'window:pane' as in the script; its windows
        are '<Class>/<window>' in the run's tmux session (always tmux, even
        without --tmux). Panes default to wrap mode; attach() and
        dashboard() do nothing. 'exits': {pane name: code(s)} for panes
        that finish on their own, as start()'s exits=. Returns what build() returns. Then
        procs["window:pane"] is a pane's Process, and procs.session the
        session build() got, for the script's other functions."""
        if self._runs:
            raise ValueError("build(): call it first, before start()")
        if not isinstance(self.backend, _TmuxBackend):
            self.backend = self._tmux_for_build()
        self.session = _BuildSession(self, exits or {})
        return build(self.session, *args, **kwargs)

    def adopt(self, command, name="script", ready=None, timeout=60.0, exits=None) -> list:
        """Test a session script as it is: run 'command' (as process 'name'),
        then make every pane it made a process of this group, named
        'window:pane' (new_pane()'s name; else the pane's index). Its tmux
        server is its own: the script's session names can't clash with
        yours, and none of it shows in the run's dashboard. attach() and
        dashboard() do nothing (LTE_HEADLESS).

        The script must exit 0 within 'timeout' s; or, with 'ready' (a
        regex), print a matching line, and is then a process of the group
        too. 'exits': {pane name: code(s)} for panes that finish on their
        own, as start()'s exits=. Returns the panes' Processes. Exit statuses are exact for wrap
        and exec panes; for shell panes, only that they ended. A plain tmux
        script works too: a pane's command is an exec pane, an idle shell is
        left out. The panes can't be started again. Kept on failure:
        tmux -S <socket> attach. See docs/testing-scripts.md."""
        if self._runs:
            raise ValueError("adopt(): call it first, before start() or build()")
        self.backend = backend = _AdoptedBackend()
        self._adopted = True
        name = self._name(name)
        on_exit, on_line = self._begin(name, command, exits=None if ready else 0)
        backend.run_script(name, command, on_exit, on_line, self.log(name))
        if ready is not None:
            self.wait_for(name, ready, timeout)
        else:
            self.wait_for_exit(name, timeout)
        adopted = []
        for pane, pane_id, mode in backend.discover():
            on_exit, on_line = self._begin(pane, "", exits=(exits or {}).get(pane))
            backend.watch(pane, pane_id, mode, on_exit, on_line, self.log(pane))
            adopted.append(Process(self, pane))
        if not adopted:
            raise ProcessFailure(f"{command!r} made no tmux panes; its output:\n    "
                                 + self._tail(name))
        return adopted

    def __getitem__(self, name) -> Process:
        name = self._name(name)
        if name not in self._runs:
            raise KeyError(f"{name}: no such process; there are {', '.join(self.names) or 'none'}")
        return Process(self, name)

    def kill(self, name):
        """SIGKILL it: a death, unless in an expect_exit() block."""
        self.backend.kill(self._name(name))

    def kill_all(self):
        """SIGKILL everything still running, at once: no death, no grace.
        For a second Ctrl-C while close() waits."""
        self.stopping = True
        for name, run in self._runs.items():
            run.stopping = True
            if self.backend.status(name).running:
                with contextlib.suppress(Exception):
                    self.backend.kill(name)

    @contextlib.contextmanager
    def expect_exit(self, name, timeout=10.0):
        """For an exit the test causes on purpose (kill(), a bad input): an
        exit of 'name' in the block, or up to 'timeout' s after it, is no
        death. Fails if it is still running then.

            with procs.expect_exit("nav:planner"):
                procs["nav:planner"].kill()"""
        name = self._name(name)
        run = self._runs[name]
        run.stopping = True  # as for stop(): its exit is no death
        yield
        if not run.exited.wait(timeout):
            raise ProcessFailure(f"{name} still running {timeout}s after expect_exit()")

    def stop(self, name, sigint_timeout=None, check=True, sigterm_timeout=None):
        """Stop it (SIGINT, then SIGTERM, SIGKILL); its exit is no death.
        The timeouts default to start()'s, else the group's. With 'check',
        fail unless SIGINT stopped it, with one of its stop_codes. Returns
        its PaneStatus."""
        name = self._name(name)
        run = self._runs[name]
        run.stopping = True
        default_int, default_term = self._timeouts(name)
        self.backend.stop(name, default_int if sigint_timeout is None else sigint_timeout,
                          default_term if sigterm_timeout is None else sigterm_timeout)
        run.exited.wait(5)
        status = self.backend.status(name)
        if check and not run.finished and not self._clean(status, run):
            raise ProcessFailure(f"{name} did not stop cleanly: {status}\n    " + self._tail(name))
        return status

    def close(self, check=True):
        """Stop everything still running (SIGINT, then SIGTERM, SIGKILL).
        With 'check', fail unless each stopped cleanly: SIGINT was enough,
        with one of its stop_codes, and no fail_on line while stopping.
        A second close() does nothing; nor does --keep-open."""
        if self not in _open:
            return
        _open.remove(self)
        if self.keep_open:
            self._let_go(keep=True)
            return
        judged = [(name, run) for name, run in self._runs.items()
                  if not (run.died or run.stopping or run.finished)]
        self.stopping = True
        self.backend.stop_all(self.names, {n: self._timeouts(n) for n in self.names})
        self._let_go(keep=self._aborted is not None)
        if not check:
            return
        unclean = [f"{name}: {status}" for name, run in judged
                   for status in [self.backend.status(name)]
                   if not run.finished and not self._clean(status, run)]
        unclean += [f"while stopping: {what}" for what, late in self.failed_on if late]
        if unclean:
            self._fail("did not shut down cleanly:\n" + "\n".join(unclean))

    def _timeouts(self, name):
        """(sigint_timeout, sigterm_timeout) for 'name': start()'s, else the
        group's."""
        own_int = self._sigint_timeouts.get(name)
        own_term = self._sigterm_timeouts.get(name)
        return (self.sigint_timeout if own_int is None else own_int,
                self.sigterm_timeout if own_term is None else own_term)

    def _let_go(self, keep):
        """adopt()'s tmux server: killed, or kept to look at."""
        if isinstance(self.backend, _AdoptedBackend):
            self.backend.close(keep)
            if keep:
                self._kept_server(self.backend.socket)

    def _abort(self, why):
        """A test failed: stop everything now, without checks."""
        self._aborted = why
        self.close(check=False)

    def _name(self, name):
        return name if ":" in name else f"{name}:{name}"

    @staticmethod
    def _clean(status, run):
        if status.stopped not in (None, "SIGINT"):
            return False
        code = _exit_code(status)
        # An adopted shell pane's exit code is not known: SIGINT was enough.
        return code in run.stop_codes or (code is None and status.stopped == "SIGINT")

    def _on_exit(self, name, status, run):  # a watcher thread
        if run.exits is not None and _exit_code(status) in run.exits:
            run.finished = True
        elif not (self.stopping or run.stopping):
            run.died = True
            self.died.append((name, status, run))
        run.exited.set()
        if self.on_exit is not None:
            self.on_exit(name, status, run.died)

    def wait_for_exit(self, name, timeout=10.0) -> PaneStatus:
        """Wait for a process started with 'exits' to finish; returns its
        PaneStatus. Fails if it is still running after 'timeout' s, or
        exited with a code not in 'exits'."""
        name = self._name(name)
        run = self._runs[name]
        if run.exits is None:
            raise ValueError(f"{name} was not started with exits=: its exit would be a death")
        if not run.exited.wait(timeout):
            raise ProcessFailure(f"{name} still running after {timeout}s; last output:\n    "
                                 + self._tail(name))
        self.check()  # an exit code not in 'exits' is a death
        return self.backend.status(name)

    def _on_line(self, name, patterns):
        if not patterns:
            return None

        def on_line(line):  # a reader thread
            for check in patterns:
                if callable(check):
                    problem = check(line)
                    what = problem and f"{name}: {problem}"
                else:
                    what = check.search(line) and f"{name} printed {line!r} (fail_on {check.pattern!r})"
                if what:
                    what += f" (log: {self.log(name)})"
                    self.failed_on.append((what, self.stopping or self._runs[name].stopping))
        return on_line

    def wait_for(self, name, pattern, timeout=10.0) -> "re.Match":
        """Wait for the process to print a line matching 'pattern' (a regex,
        searched in plain text); what it printed already counts. Fails the
        test if it doesn't within 'timeout' s, or exits first. Name "*":
        any process (checking health while it waits)."""
        if name == "*":
            regex = re.compile(pattern)
            return self.wait_until(lambda: next((m for n in self.names for line in self.backend.lines(n)
                                                 for m in [regex.search(line)] if m), None),
                                   timeout, poll=0.1, what=f"a line matching {pattern!r}")
        name = self._name(name)
        regex = re.compile(pattern)
        try:
            return self.backend.wait_for(name, regex, timeout)
        except TimeoutError:
            why = f"printed no line matching {pattern!r} within {timeout}s"
        except PaneExited as exc:
            why = f"{exc.status.exit_status} before printing a line matching {pattern!r}"
        raise ProcessFailure(f"{name} {why}; last output:\n    " + self._tail(name))

    def wait_for_in_order(self, name, patterns, timeout=10.0) -> list:
        """Wait for lines matching each of 'patterns', in that order, each on
        a later line than the one before; returns the re.Matches. Fails
        after 'timeout' s (0: check now), naming the first pattern missing."""
        name = self._name(name)
        regexes = [re.compile(p) for p in patterns]
        found = []

        def matched():
            found.clear()
            lines = iter(self.backend.lines(name))
            for regex in regexes:
                m = next((m for line in lines for m in [regex.search(line)] if m), None)
                if m is None:
                    return False
                found.append(m)
            return True

        deadline = time.monotonic() + timeout
        while not matched():
            if time.monotonic() >= deadline:
                after = f" after {found[-1].group(0)!r}" if found else ""
                raise ProcessFailure(f"{name}: no line matching {patterns[len(found)]!r}{after} "
                                     f"within {timeout}s; last output:\n    " + self._tail(name))
            self.check()
            time.sleep(0.1)
        return list(found)

    def wait_until(self, predicate, timeout=30.0, poll=0.2, what=None):
        """Call predicate() until it returns something true, and return
        that; check() between calls, so a death fails now, not at the
        timeout. Fails if 'timeout' s pass first."""
        deadline = time.monotonic() + timeout
        while True:
            self.check()
            result = predicate()
            if result:
                return result
            if time.monotonic() >= deadline:
                raise ProcessFailure(f"timed out after {timeout}s waiting for "
                                     f"{what or getattr(predicate, '__name__', 'a condition')}")
            time.sleep(poll)

    def status(self, name) -> PaneStatus:
        return self.backend.status(self._name(name))

    def log(self, name):
        """The file the process's output goes to, as plain text."""
        return os.path.join(self.log_dir, re.sub(r"[^A-Za-z0-9_.-]", ".", self._name(name)) + ".log")

    def output(self, name) -> str:
        """Everything the process has printed so far (its latest start)."""
        return self.backend.output(self._name(name))

    def _tail(self, name, lines=8):
        return "\n    ".join(self.output(name).strip().splitlines()[-lines:])

    def check(self):
        """Raise AssertionError if anything died, or printed a fail_on line,
        since the last check(). Each problem fails one test: the one it
        happened in. Runs after every test; call it while waiting to fail
        fast."""
        died = self.died[self._raised_died:]
        failed_on = self.failed_on[self._raised_failed_on:]
        self._raised_died, self._raised_failed_on = len(self.died), len(self.failed_on)
        problems = [f"{name} {status.exit_status}" + ("" if run.exits is None else
                                                       f" (expected exit {', '.join(map(str, run.exits))})")
                    for name, status, run in died]
        problems += [what for what, late in failed_on if not late]
        for name, _, _ in died:
            problems.append(f"  last output of {name} ({self.log(name)}):\n    " + self._tail(name))
        if problems:
            raise ProcessFailure("launched process died early:\n" + "\n".join(problems))


_open = []  # every Processes not closed yet: what check_health() and the hooks check