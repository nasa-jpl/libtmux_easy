"""lte-run: run a command under libtmux_easy supervision.

    lte-run [--tee FILE [-a] [--raw]] [--] COMMAND [ARG...]

Prepend it to any command typed into a pane of a libtmux_easy session. It
reports the command's pid and exact exit status to the session's Python
process, and accepts control messages (e.g. "send SIGINT") back. Outside such
a session, or once that process has gone away, it simply runs the command --
same output, same exit status -- so it is always safe to prepend.

  --tee FILE  also copy the command's output to FILE, like 'tee': stdout and
              stderr, as one stream. In a tmux pane the command keeps its
              terminal (tmux copies the pane's output); elsewhere its output
              goes through a pipe. FILE is complete when the exit is reported.
              Takes over the pane's capture pipe (TmuxSession.capture()) for
              the run; the pipe closes when the command ends.
  -a          append to FILE instead of overwriting it
  --raw       keep terminal escapes and carriage returns (default: plain text)

$LTE_RUN_TEE=FILE does what --tee FILE does (--tee wins). It is removed from the
command's environment, so an lte-run inside it doesn't tee again.
"""

import json
import os
import re
import resource
import select
import shlex
import shutil
import stat
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from .proc import descendants, proc_table

PROTOCOL_VERSION = 1
DIR_OPTION = "@lte_dir"   # tmux session option set by TmuxSession
DIR_ENV = "LTE_DIR"       # overrides DIR_OPTION; for tests and non-tmux use
SOCKET_NAME = "ctl.sock"
TEE_ENV = "LTE_RUN_TEE"   # a default for --tee
PIPE_FILE_OPTION = "@lte_pipe_file"  # pane option: the file the pane's output goes to
PIPE_FROM_OPTION = "@lte_pipe_from"  # pane option: where in it the current capture began
PIPE_TEE_OPTION = "@lte_pipe_tee"    # pane option: lte-run --tee has the pipe
PIPE_ID_OPTION = "@lte_pipe_id"      # pane option: new for each pipe (a rerun's same file)
_PIPE_REQ = "@lte_pipe_req"          # pane options: a pipe request, see guarded_pipe()

# When a pane may be piped: not piped, or piped by a capture (it has a file).
# A capture of the library's, unless lte-run --tee has it; with_tee, either.
_FREE = (f"#{{||:#{{==:#{{pane_pipe}},0}},"
         f"#{{&&:#{{{PIPE_FILE_OPTION}}},#{{!=:#{{{PIPE_TEE_OPTION}}},1}}}}}}")
_FREE_WITH_TEE = f"#{{||:#{{==:#{{pane_pipe}},0}},#{{{PIPE_FILE_OPTION}}}}}"


def _on_own_server(pane, commands):
    """tmux args that run the tmux command strings 'commands' only on the
    server this process started under. A server that exited and a new one
    reuse pane ids: our '%0' would be someone else's pane."""
    server_pid = (os.environ.get("TMUX", "").split(",") + ["", ""])[1]
    if not server_pid.isdigit():
        return [part for c in commands for part in (*shlex.split(c), ";")][:-1]
    return ["if", "-F", "-t", pane, f"#{{==:#{{pid}},{server_pid}}}", " ; ".join(commands)]


def tmux_literal(text):
    """'text' as itself in a pipe-pane command, which tmux expands as a
    format and with strftime: 'main.%4.log' would become 'main.'."""
    return text.replace("#", "##").replace("%", "%%")


def guarded_unpipe(pane, *then):
    """tmux args that close 'pane''s pipe, then run the tmux command
    strings 'then', unless lte-run --tee has the pipe: checked in the same
    tmux call, so a run starting meanwhile keeps it."""
    commands = " ; ".join((f"pipe-pane -t {pane}", *then))
    return ["if", "-F", "-t", pane, f"#{{!=:#{{{PIPE_TEE_OPTION}}},1}}", commands]


def guarded_pipe(pane, command, options=None, with_tee=False):
    """tmux args that pipe 'pane' to 'command' (formats escaped, as for
    pipe-pane) and set its pane 'options' ({name: value}), but only if the
    pane is free. The check and the pipe are one tmux call, so the library
    and lte-run can't both see a free pane and pipe it. The call prints
    "<pane> 1" if it piped the pane, "<pane> " if not."""
    options = options or {}
    req = [(f"{_PIPE_REQ}_cmd", command)] + \
        [(f"{_PIPE_REQ}_{i}", value) for i, value in enumerate(options.values())]
    ok = f"{_PIPE_REQ}_ok"
    branch = f'pipe-pane -O -t {pane} "#{{E:{_PIPE_REQ}_cmd}}"' + "".join(
        f' ; set -pF -t {pane} {name} "#{{{_PIPE_REQ}_{i}}}"' for i, name in enumerate(options)) + \
        f" ; set -p -t {pane} {ok} 1"
    args = ["set", "-p", "-u", "-t", pane, ok, ";"]
    for name, value in req:
        args += ["set", "-p", "-t", pane, name, value, ";"]
    args += ["if", "-F", "-t", pane, _FREE_WITH_TEE if with_tee else _FREE, branch, ";",
             "display", "-p", "-t", pane, f"{pane} #{{{ok}}}", ";"]
    for name, _ in req + [(ok, None)]:
        args += ["set", "-p", "-u", "-t", pane, name, ";"]
    return args[:-1]


# The terminal sends these to the whole foreground process group, so the
# target already gets them directly. Ignoring them is what lets the wrapper
# outlive the target and report how it ended.
_TERMINAL_SIGNALS = (signal.SIGINT, signal.SIGQUIT)

# These arrive at the wrapper specifically (kill <pid>, the pane closing).
# Passed on rather than dying, so the target is not orphaned.
_FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


def find_dir():
    """The session's control directory, or None when unsupervised."""
    if os.environ.get(DIR_ENV):
        return os.environ[DIR_ENV]
    pane = os.environ.get("TMUX_PANE")
    if not pane or not os.environ.get("TMUX"):
        return None
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{%s}" % DIR_OPTION],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


class Channel:
    """Newline-delimited JSON over a unix stream socket. Every method is a
    no-op when unconnected, so supervised and unsupervised runs share one
    code path."""

    def __init__(self, directory):
        self._sock = None
        self._send_lock = threading.Lock()
        if directory is None:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(os.path.join(directory, SOCKET_NAME))
        except OSError:
            sock.close()
            return  # nobody listening, e.g. the session's script has exited
        # Close-on-exec by default (PEP 446), so the target does not inherit
        # it -- which matters: an inherited copy would hide our EOF.
        self._sock = sock

    def send(self, **msg):
        sock = self._sock
        if sock is None:
            return
        with self._send_lock:
            try:
                sock.sendall((json.dumps(msg) + "\n").encode())
            except OSError:
                self._sock = None  # listener went away; carry on unsupervised

    def messages(self):
        """Yield control messages until the connection closes."""
        sock = self._sock
        if sock is None:
            return
        buf = b""
        while True:
            try:
                chunk = sock.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for line in lines:
                try:
                    yield json.loads(line)
                except ValueError:
                    pass


def signal_target(sig, target):
    """Deliver 'sig' to the target and every descendant of ours in our process
    group -- what a Ctrl-C at the terminal reaches, minus the wrapper itself.

    The descendant filter matters without job control (e.g. 'sh -c "lte-run
    ..."'), where our process group also contains whatever started us."""
    table = proc_table()
    if not table:  # no /proc: best effort
        pids = [target]
    else:
        pgid = os.getpgrp()
        pids = [p for p in descendants(os.getpid(), table) if p == target or table[p][2] == pgid]
    for pid in pids:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


# Terminal escapes (CSI, OSC, two-byte ESC sequences): what a pane's output
# carries besides text.
_ESCAPES = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
                      rb"|\x1b[@-Z\\-_]")


def plain_line(line):
    """One line of pane output (bytes, no LF) as plain text: escapes dropped,
    a trailing CR dropped, and a line redrawn with CR kept as its last state."""
    if b"\x1b" not in line and b"\r" not in line:
        return line  # the usual case: nothing to do
    return _ESCAPES.sub(b"", line).rstrip(b"\r").rsplit(b"\r", 1)[-1]


_CR_LF = re.compile(rb"\r+\n")
_TO_LAST_CR = re.compile(rb"[^\n]*\r")


def plain_text(data):
    """plain_line() for every line of 'data' at once."""
    if b"\x1b" in data:
        data = _ESCAPES.sub(b"", data)
    if b"\r" in data:
        data = _TO_LAST_CR.sub(b"", _CR_LF.sub(b"\n", data).rstrip(b"\r"))
    return data


QUIET = 0.15  # after the command ends, its last output is taken to have arrived
              # once the copy has been this quiet (at most DRAIN seconds)
DRAIN = 2.0


def open_tee(path, append):
    """FILE opened for writing. A FIFO must already have a reader: refusing
    beats blocking forever."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    try:
        if stat.S_ISFIFO(os.stat(path).st_mode):
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            os.set_blocking(fd, True)
            return fd
    except FileNotFoundError:
        pass
    return os.open(path, flags, 0o666)


class Tee:
    """Copies the command's output to a file: from tmux's pipe-pane in a pane
    (the command keeps its terminal), else from a pipe the command writes to,
    passed on to our own stdout too. Plain text unless 'raw': escapes
    dropped, CRLF made LF, and a line redrawn with CR kept as its last state."""

    def __init__(self, fd, raw, path=None):
        self.fd, self.raw, self.path = fd, raw, path
        self.partial = b""
        self.pane = None       # pipe-pane: the pane, and the FIFO dir
        self.scratch = None
        self.source = None     # the fd we read
        self.hold = None       # pipe-pane: our own write end, so no early EOF
        self.echo = False      # pipe: pass output on to our stdout
        self.child_out = None  # pipe: the command's end
        self.done = threading.Event()
        self.thread = None

    def prepare(self):
        """Before the command starts, so not even its first line is missed."""
        pane = os.environ.get("TMUX_PANE")
        in_tmux = pane and os.environ.get("TMUX") and shutil.which("tmux")
        publish = self._publish() if in_tmux else {}
        if not (in_tmux and self._pipe_pane(pane, publish)):
            self.source, self.child_out = os.pipe()
            self.echo = True
            if publish:
                args = []
                for name, value in publish.items():
                    args += ["set", "-p", "-t", pane, name, value, ";"]
                subprocess.run(["tmux", *args[:-1]], capture_output=True)

    def _publish(self):
        """Pane options naming FILE as the pane's output file, for watches
        and searches; none for a FIFO."""
        try:
            if not (self.path and stat.S_ISREG(os.fstat(self.fd).st_mode)):
                return {}
            start = os.fstat(self.fd).st_size
        except OSError:
            return {}
        return {PIPE_FILE_OPTION: os.path.abspath(self.path), PIPE_FROM_OPTION: str(start),
                PIPE_ID_OPTION: f"lte-run-{os.getpid()}", PIPE_TEE_OPTION: "1"}

    def start(self):
        """After fork(): a thread before it would be copied half-way."""
        self.thread = threading.Thread(target=self._copy, daemon=True)
        self.thread.start()

    def _pipe_pane(self, pane, publish=None):
        """tmux copies the pane's output to a FIFO we read. A capture pipe
        (TmuxSession.capture()) is taken over: tmux has one pipe per pane.
        False when someone else's pipe is there (e.g. lte-session's
        transcript)."""
        self.scratch = tempfile.mkdtemp(prefix="lte_tee_")
        fifo = os.path.join(self.scratch, "out")
        os.mkfifo(fifo)
        self.source = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.hold = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        cat = tmux_literal(f"exec cat > {shlex.quote(fifo)}")
        out = subprocess.run(["tmux", *guarded_pipe(pane, cat, publish, with_tee=True)],
                             capture_output=True, text=True)
        if out.returncode or "1" not in out.stdout.split():
            for fd in (self.source, self.hold):  # not self.fd: the fallback writes it
                os.close(fd)
            self.source = self.hold = None
            shutil.rmtree(self.scratch, ignore_errors=True)
            self.scratch = None
            return False
        self.pane = pane
        return True

    def _copy(self):
        quiet_since = None
        while True:
            ready = select.select([self.source], [], [], QUIET / 3)[0]
            chunk = b""
            if ready:
                try:
                    chunk = os.read(self.source, 65536)
                except BlockingIOError:
                    pass
                except OSError:
                    break
                if not chunk and self.echo:
                    break  # the command, and everything it started, closed the pipe
            if chunk:
                quiet_since = None
                self._write(chunk)
            elif self.done.is_set() and self.pane:
                quiet_since = quiet_since or time.monotonic()
                if time.monotonic() - quiet_since >= QUIET:
                    break
        self._write(b"", final=True)

    def _write(self, chunk, final=False):
        if self.echo and chunk:
            try:
                os.write(sys.stdout.fileno(), chunk)
            except OSError:
                pass
        if self.raw:
            out = chunk
        else:
            *lines, self.partial = (self.partial + chunk).split(b"\n")
            if final and self.partial:
                lines, self.partial = lines + [self.partial], b""
            out = b"".join(plain_line(line) + b"\n" for line in lines)
        try:
            if out:
                os.write(self.fd, out)
        except OSError:
            pass

    def finish(self):
        """After the command has ended: the rest of its output, then close."""
        self.done.set()
        if self.child_out is not None:
            os.close(self.child_out)  # only the command's copies keep it open now
            self.child_out = None
        if self.thread is not None:
            # A pipe ends when the command's processes close it; a background
            # child that keeps it open would hold us forever, hence the bound.
            self.thread.join(DRAIN if self.pane else 30)
        pane = os.environ.get("TMUX_PANE") if os.environ.get("TMUX") else None
        commands = [f"pipe-pane -t {self.pane}"] if self.pane else []
        if pane and shutil.which("tmux"):
            commands.append(f"set -p -u -t {pane} {PIPE_TEE_OPTION}")
        if commands:
            subprocess.run(["tmux", *_on_own_server(pane or self.pane, commands)],
                           capture_output=True)
        self._close()

    def _close(self):
        for fd in (self.source, self.hold, self.fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.source = self.hold = self.fd = None
        if self.scratch:
            shutil.rmtree(self.scratch, ignore_errors=True)


def parse(argv):
    """(options, command). Options come before the command; '--' ends them."""
    opts = {"tee": None, "append": False, "raw": False}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            i += 1
            break
        if arg in ("-a", "--append"):
            opts["append"] = True
        elif arg == "--raw":
            opts["raw"] = True
        elif arg == "--tee" and i + 1 < len(argv):
            i += 1
            opts["tee"] = argv[i]
        elif arg.startswith("--tee="):
            opts["tee"] = arg.split("=", 1)[1]
        elif arg in ("-h", "--help"):
            return None, []
        elif arg.startswith("-"):
            raise ValueError(f"unknown option {arg}")
        else:
            break
        i += 1
    return opts, argv[i:]


def main(argv=None, channel=None):
    """lte-run. 'channel' replaces the session socket: any object with
    send(**msg) and messages(), like Channel (lte-session uses this)."""
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        opts, argv = parse(argv)
    except ValueError as exc:
        sys.stderr.write(f"lte-run: {exc}\n{__doc__}")
        return 2
    if not argv:
        sys.stderr.write(__doc__)
        return 2

    tee = None
    opts["tee"] = opts["tee"] or os.environ.get(TEE_ENV) or None
    os.environ.pop(TEE_ENV, None)  # the command inherits os.environ
    if opts["tee"]:
        try:
            tee = Tee(open_tee(opts["tee"], opts["append"]), opts["raw"], opts["tee"])
            tee.prepare()
        except OSError as exc:  # the command still runs, untee'd: its status matters more
            sys.stderr.write(f"lte-run: --tee {opts['tee']}: {exc.strerror or exc}\n")
            tee = None

    if channel is None:
        channel = Channel(find_dir())

    # Pass the inherited dispositions on to the target. Python installs its
    # own SIGINT handler at startup unless SIGINT was already ignored, so
    # "not SIG_IGN" here means "default" at the OS level.
    inherited_ign = {s: signal.getsignal(s) == signal.SIG_IGN for s in _TERMINAL_SIGNALS}
    for s in _TERMINAL_SIGNALS:
        signal.signal(s, signal.SIG_IGN)

    pid = os.fork()
    if pid == 0:
        for s, ign in inherited_ign.items():
            signal.signal(s, signal.SIG_IGN if ign else signal.SIG_DFL)
        if tee is not None and tee.child_out is not None:
            os.dup2(tee.child_out, 1)
            os.dup2(tee.child_out, 2)
        try:
            os.execvp(argv[0], argv)
        except OSError as exc:
            sys.stderr.write(f"lte-run: {argv[0]}: {exc.strerror}\n")
            sys.stderr.flush()  # os._exit skips it; 3.8 buffers stderr to a pipe
            os._exit(127 if isinstance(exc, FileNotFoundError) else 126)

    if tee is not None:
        tee.start()
    channel.send(event="started", version=PROTOCOL_VERSION,
                 pane=os.environ.get("TMUX_PANE"), wrapper_pid=os.getpid(),
                 target_pid=pid, argv=argv, time=time.time())

    for s in _FORWARDED_SIGNALS:
        signal.signal(s, lambda signum, _frame: signal_target(signum, pid))

    def serve():
        for msg in channel.messages():
            if msg.get("cmd") == "signal":
                try:
                    signal_target(int(msg["signal"]), pid)
                except (KeyError, TypeError, ValueError):
                    pass

    threading.Thread(target=serve, daemon=True).start()

    _, status = os.waitpid(pid, 0)  # retried across signal handlers (PEP 475)

    if os.WIFSIGNALED(status):
        code, sig = None, os.WTERMSIG(status)
    else:
        code, sig = os.WEXITSTATUS(status), None
    if tee is not None:
        tee.finish()  # before 'exited': whoever acts on that reads a whole file
    channel.send(event="exited", target_pid=pid, code=code, signal=sig, time=time.time())

    if sig is not None:
        # Die the same way, so a calling shell sees a signal death rather than
        # an exit code (e.g. Ctrl-C still aborts an enclosing shell loop).
        # No core file for the wrapper itself.
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        signal.signal(sig, signal.SIG_DFL)
        os.kill(os.getpid(), sig)
        return 128 + sig  # only reached if 'sig' does not terminate
    return code


if __name__ == "__main__":
    sys.exit(main())
