"""Pane output: captured to files by tmux pipe-pane, searched, and watched.

Capturing a pane means tmux appends its raw output to a file
(pipe-pane -O 'exec cat >> FILE'). The pane option @lte_pipe_file names the
file, so this library, lte-run --tee and a later process all agree on where
a pane's output is. A watch tails the file (OutputReader: one thread per
session, woken by inotify when a watched file is written); a search scans
it once. Lines are matched as plain text (wrap.plain_line). Panes nobody
captured cost nothing. See docs/output-match.md.
"""

import bisect
import collections
import ctypes
import ctypes.util
import logging
import os
import re
import select
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .wrap import plain_line, plain_text

logger = logging.getLogger(__name__)

QUIET = 0.15       # a partial line (a prompt, no LF yet) is matched once quiet this long
MAX_LINE = 65536   # a longer line is matched and flushed in pieces
ECHO_TTL = 10.0    # a typed command's echo is expected within this
POLL = 0.1         # a watched file is read at most this often (and polled this often without inotify)
REFRESH = 1.0      # pane -> file re-read from tmux this often while watched: a safety net
READ_MAX = 1 << 20  # bytes read per file per poll: one flood doesn't starve the rest
KEEP = 1000        # matches an OutputWatch keeps (.matches) and holds unread for wait()
BATCH = 4          # from this many lines, a read is matched as one text (see _match_batch)
# Could match less on lines joined by newlines than one by one: matched per line.
_CROSS_LINE = re.compile(r"\\[AZ]|\(\?<?[=!]|\(\?>|[*+?}]\+")


class PaneExited(RuntimeError):
    """wait_for_output(): the command ended before printing a match."""

    def __init__(self, status):
        super().__init__(f"{status.name} ended before a match ({status.exit_status})")
        self.status = status


@dataclass(frozen=True)
class OutputMatch:
    pane_id: str
    name: Optional[str]
    pattern: str
    match: "re.Match" = field(repr=False)
    line: str            # plain text, no newline
    time: float          # time.time() when the line was read
    offset: int = -1     # where the line starts in the pane's file (-1: not from a file)


def file_size(path) -> int:
    try:
        return os.stat(path).st_size
    except OSError:
        return 0


def typed_echo(line, typed):
    """'line' is the shell echoing one of the commands 'typed' (stripped,
    non-empty) into its pane: prompt + command."""
    tail = line.rstrip()
    return any(tail.endswith(t) for t in typed)


def plain_lines(data):
    """(offset, text) for each line of raw pane output: escapes dropped, a
    CR-redrawn line as its last state. A trailing partial line counts."""
    pos = 0
    for raw in data.split(b"\n"):
        start, pos = pos, pos + len(raw) + 1
        if raw:
            yield start, plain_line(raw).decode("utf-8", "replace")


class OutputWatch:
    """One pattern on a pane, a window or the whole session ('target': a
    pane id, a window id, or None). 'callback(OutputMatch)' runs on the
    reader's thread: keep it short. With 'once', the watch is done after a
    match."""

    def __init__(self, target, name, pattern, callback=None, once=False, skip_echo=True):
        self.target, self.name = target, name
        self.regex = pattern
        self.callback, self.once, self.skip_echo = callback, once, skip_echo
        self.matches = collections.deque(maxlen=KEEP)  # the latest
        self.count = 0   # matches in all
        self.done = False
        self._cond = threading.Condition()
        self._unread = collections.deque(maxlen=KEEP)  # for wait()
        self._reader = None
        self._from = {}  # pane_id -> file offset: earlier lines aren't for this watch
        # For a batch of lines joined by newlines: line anchors, per line.
        self._joined = None if _CROSS_LINE.search(pattern.pattern) else \
            re.compile(pattern.pattern, pattern.flags | re.MULTILINE)

    @property
    def pane_id(self):
        return self.target if self.target and self.target.startswith("%") else None

    def __repr__(self):
        return f"<OutputWatch {self.name or self.target or 'session'} {self.regex.pattern!r}>"

    def covers(self, pane_id, window_id):
        return self.target is None or self.target in (pane_id, window_id)

    def _offer(self, line, now, pane_id, name, offset=-1):
        """Match 'line'; True if it matched."""
        if self.done or 0 <= offset < self._from.get(pane_id, 0):
            return False
        m = self.regex.search(line)
        if m is None:
            return False
        match = OutputMatch(pane_id, name, self.regex.pattern, m, line, now, offset)
        with self._cond:
            self.matches.append(match)
            self._unread.append(match)
            self.count += 1
            if self.once:
                self.done = True
            self._cond.notify_all()
        if self.callback is not None:
            try:
                self.callback(match)
            except Exception:
                logger.exception(f"on_output callback {self.callback!r} raised")
        if self.done and self._reader is not None:
            self._reader.remove(self)
        return True

    def wait(self, timeout=None):
        """The next match not yet returned by wait(), or None on timeout."""
        with self._cond:
            if not self._cond.wait_for(lambda: self._unread or self.done, timeout):
                return None
            return self._unread.popleft() if self._unread else None

    def cancel(self):
        with self._cond:
            self.done = True
            self._cond.notify_all()
        if self._reader is not None:
            self._reader.remove(self)


class _Inotify:
    """Linux inotify through libc: which watched files were written."""

    MODIFY, CREATE = 0x2, 0x100
    _EVENT = struct.Struct("iIII")  # wd, mask, cookie, len (then the name)

    def __init__(self):
        libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
        self._add, self._rm = libc.inotify_add_watch, libc.inotify_rm_watch
        self._add.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
        self._rm.argtypes = (ctypes.c_int, ctypes.c_int)
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")

    def add(self, path, mask):
        """The watch descriptor, or None (no such file yet, or out of watches)."""
        wd = self._add(self.fd, os.fsencode(path), mask)
        return wd if wd >= 0 else None

    def remove(self, wd):
        self._rm(self.fd, wd)

    def read(self):
        """The descriptors with events since the last read."""
        wds = set()
        while True:
            try:
                data = os.read(self.fd, 65536)
            except (BlockingIOError, InterruptedError):
                return wds
            pos = 0
            while pos + self._EVENT.size <= len(data):
                wd, _mask, _cookie, length = self._EVENT.unpack_from(data, pos)
                wds.add(wd)
                pos += self._EVENT.size + length

    def close(self):
        os.close(self.fd)


class _Source:
    """One pane's file, as far as it has been read."""

    __slots__ = ("pane_id", "window_id", "name", "path", "start", "pipe", "offset", "buf",
                 "last", "fired", "fd", "more", "truncated")

    def __init__(self, pane_id, window_id, name, path, start, pipe, offset):
        self.pane_id, self.window_id, self.name = pane_id, window_id, name
        self.path, self.start, self.pipe, self.offset = path, start, pipe, offset
        self.buf = b""
        self.last = 0.0
        self.fired = set()  # watches that matched the partial line already
        self.fd = None      # kept open while watched
        self.more = True    # may have unread bytes: read without waiting for an event
        self.truncated = -1e9  # when a shrink was seen (a rerun overwriting the file)

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


class OutputReader(threading.Thread):
    """Tails the files of watched panes and offers their lines to watches.

    Woken by inotify when a watched file is written (else it polls every
    POLL), it reads only the new bytes, at most every POLL per file. Idle
    with nothing written; blocked with no watches. Every REFRESH, while
    watches exist, it re-reads pane -> file from tmux: a safety net for
    lte-run retargets and panes it wasn't told about.

    'panes()' -> {pane_id: (window_id, piped, path, start, pipe id)} for
    the session's panes with a file; 'names(pane_id)' -> its name or None;
    'typed(pane_id)' -> texts typed into it (for typed_echo()), for echoes
    in a replay."""

    def __init__(self, panes, names, typed, ignore=None):
        super().__init__(daemon=True, name="lte-output")
        self._panes, self._names, self._typed = panes, names, typed
        self.ignore = ignore          # compiled regex: lines that are never output (probes)
        # Reentrant: callbacks run under it and may add or cancel watches.
        self._lock = threading.RLock()
        self._watches = []            # OutputWatch
        self._sources = {}            # pane_id -> _Source
        self._echo = {}               # pane_id -> deque of (text, monotonic time)
        self._closed = threading.Event()
        self._refreshed = 0.0
        self._synced_once = False
        self._resync = False          # resync(): pane -> file changed
        try:
            self._inotify = _Inotify()
        except (OSError, AttributeError) as exc:  # not Linux, no libc symbol, no fds
            logger.debug(f"inotify unavailable ({exc}); polling every {POLL}s")
            self._inotify = None
        self._wds = {}                # path -> watch descriptor (files and capture dirs)
        self._by_wd = {}              # wd -> path
        self._dir_wds = set()         # a new file there: a new pane, maybe
        self._wake_lock = threading.Lock()
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        self.start()

    @property
    def alive(self):
        return not self._closed.is_set() and self.is_alive()

    def close(self):
        self._closed.set()
        self._wake()
        with self._lock:
            watches, self._watches = self._watches, []
        for w in watches:
            with w._cond:
                w.done = True
                w._cond.notify_all()

    def _wake(self):
        with self._wake_lock:
            if self._wake_w is not None:
                try:
                    os.write(self._wake_w, b"x")
                except OSError:
                    pass

    # ----- registration (any thread) -----

    def add(self, watch, since=None):
        """Start matching 'watch'. 'since': None, from now on; "start", from
        where each pane's capture began; or {pane_id: offset}."""
        watch._reader = self
        panes = self._panes()
        with self._lock:
            self._sync(panes)
            for pane_id, src in self._sources.items():
                if not watch.covers(pane_id, src.window_id):
                    continue
                begin = (src.start if since == "start" else
                         since.get(pane_id, 0) if isinstance(since, dict) else file_size(src.path))
                watch._from[pane_id] = begin  # a pane made later: from its first byte
                if begin < src.offset:
                    self._replay(src, begin, src.offset, watch)
            self._watches.append(watch)
            self._update_watched()
        self._wake()

    def remove(self, watch):
        with self._lock:
            if watch in self._watches:
                self._watches.remove(watch)
                self._update_watched()

    def remove_target(self, target):
        with self._lock:
            gone = [w for w in self._watches if w.target == target]
        for w in gone:
            w.cancel()

    def expect_echo(self, pane_id, text):
        """'text' is about to be typed into the pane: its echo isn't output."""
        with self._lock:
            self._echo.setdefault(pane_id, collections.deque(maxlen=16)).append(
                (text.strip(), time.monotonic()))

    def new_run(self, pane_id):
        """The pane's command was restarted (or lte-run retargeted its file):
        drop the half line, pick up a new file."""
        with self._lock:
            src = self._sources.get(pane_id)
            if src is not None:
                src.buf, src.fired = b"", set()
        self.resync()

    def resync(self):
        """A pane's file changed (the library piped it): the thread re-reads
        pane -> file, if anything is watched."""
        self._resync = True
        self._wake()

    # ----- the reader thread -----

    def run(self):
        try:
            while not self._closed.is_set():
                ready = select.select([self._wake_r] + ([self._inotify.fd] if self._inotify else []),
                                      [], [], self._timeout())[0]
                if self._closed.is_set():
                    break
                if self._wake_r in ready:
                    _drain(self._wake_r)
                written = self._inotify.read() if self._inotify and self._inotify.fd in ready \
                    else set()
                with self._lock:
                    watching = bool(self._watches)
                    created = bool(written & self._dir_wds)
                due = self._resync or created or time.monotonic() - self._refreshed >= REFRESH
                if watching and due:
                    self._resync = False
                    panes = self._panes()  # a tmux call: outside the lock
                    with self._lock:
                        self._sync(panes)
                        self._update_watched()
                read_any = False
                with self._lock:
                    for src in list(self._sources.values()):
                        if src.fd is not None and (src.more or self._inotify is None or
                                                   self._wds.get(src.path) in written):
                            read_any |= self._read(src)
                    self._flush_partials()
                if read_any:
                    self._closed.wait(POLL)  # batch what a busy pane writes meanwhile
        except Exception:
            if not self._closed.is_set():
                logger.exception("output reader failed")
        finally:
            self.close()
            with self._lock:
                for src in self._sources.values():
                    src.close()
                if self._inotify is not None:
                    self._inotify.close()
            with self._wake_lock:
                os.close(self._wake_r)
                os.close(self._wake_w)
                self._wake_w = None

    def _timeout(self):
        """How long select() may block: forever when idle."""
        with self._lock:
            if not self._watches:
                return None
            now = time.monotonic()
            waits = [max(0.0, REFRESH - (now - self._refreshed))]
            for src in self._sources.values():
                if src.fd is None:
                    continue
                if src.more or self._inotify is None or src.path not in self._wds:
                    waits.append(POLL)
                if src.buf and not src.fired:
                    waits.append(max(0.0, QUIET - (now - src.last)))
            return min(waits)

    def _covering(self, src):
        return [w for w in self._watches if w.covers(src.pane_id, src.window_id)]

    def _update_watched(self):
        """Under the lock: open (and inotify-watch) the files of watched
        panes and their directories; close the rest."""
        paths, dirs = set(), set()
        for src in self._sources.values():
            if self._covering(src):
                dirs.add(os.path.dirname(src.path))  # where a file not made yet appears
                if src.fd is None:
                    try:
                        src.fd = os.open(src.path, os.O_RDONLY | os.O_CLOEXEC)
                        src.more = True
                    except OSError:
                        continue
                paths.add(src.path)
            else:
                src.close()
        if self._inotify is None:
            return
        wanted = paths | dirs
        for path in list(self._wds):
            if path not in wanted:
                wd = self._wds.pop(path)
                self._by_wd.pop(wd, None)
                self._dir_wds.discard(wd)
                self._inotify.remove(wd)
        for path in wanted - set(self._wds):
            is_dir = path in dirs
            wd = self._inotify.add(path, _Inotify.CREATE if is_dir else _Inotify.MODIFY)
            if wd is not None:
                self._wds[path], self._by_wd[wd] = wd, path
                if is_dir:
                    self._dir_wds.add(wd)

    def _sync(self, panes):
        """Under the lock: follow tmux's pane -> file. A new pane is read from
        where its capture began; a pane whose file changed, likewise."""
        self._refreshed = time.monotonic()
        for pane_id in list(self._sources):
            if pane_id not in panes:
                self._sources.pop(pane_id).close()
        for pane_id, (window_id, _piped, path, start, pipe) in panes.items():
            src = self._sources.get(pane_id)
            if src is None:
                # Known before any watch: only what comes next. Else (a pane
                # made or captured since): all of its capture.
                offset = start if self._watches or self._synced_once else file_size(path)
                src = self._sources[pane_id] = _Source(pane_id, window_id, None, path, start,
                                                       pipe, offset)
            elif (src.path, src.start, src.pipe) != (path, start, pipe):
                # A new pipe: another file, or a rerun overwriting this one
                # (start 0). A run appending to it may have been read already.
                if path == src.path and start:
                    src.offset = max(src.offset, start)
                elif path == src.path and time.monotonic() - src.truncated < 2 * REFRESH:
                    pass  # this overwrite was seen as a shrink and read from 0 already
                else:
                    src.close()
                    src.path, src.offset = path, start
                    src.buf, src.fired = b"", set()
                src.start, src.pipe = start, pipe
            src.window_id = window_id
            src.name = self._names(pane_id)
        self._synced_once = True

    def _read(self, src):
        """Under the lock: match what was added to the file. True if
        anything was read."""
        try:
            size = os.fstat(src.fd).st_size
        except OSError:
            return False
        if size < src.offset:  # truncated: a new run overwrote it
            src.offset, src.buf, src.fired = 0, b"", set()
            src.truncated = time.monotonic()
        src.more = size - src.offset > READ_MAX
        if size == src.offset:
            return False
        try:
            data = os.pread(src.fd, min(size - src.offset, READ_MAX), src.offset)
        except OSError:
            return False
        base = src.offset - len(src.buf)
        src.offset += len(data)
        src.last = time.monotonic()
        *lines, src.buf = (src.buf + data).split(b"\n")
        while len(src.buf) > MAX_LINE:
            lines.append(src.buf[:MAX_LINE])
            src.buf = src.buf[MAX_LINE:]
        fired, src.fired = src.fired, set()
        if lines:
            self._match_batch(src, lines, base, self._covering(src), time.time(), fired)
        return True

    def _match_batch(self, src, raws, base, watches, now, fired):
        """Under the lock: offer complete lines to the watches. 'fired':
        watches that matched the first line while it was partial."""
        offsets, pos = [], base
        for raw in raws:
            offsets.append(pos)
            pos += len(raw) + 1
        text = None
        if len(raws) >= BATCH and not self._echo.get(src.pane_id):
            text = plain_text(b"\n".join(raws)).decode("utf-8", "replace")
            lines = text.split("\n")
            if len(lines) != len(raws):  # an escape spanned lines: go line by line
                text = None
        if text is None:
            for i, raw in enumerate(raws):
                self._match(src, raw, offsets[i], watches, now, skip=fired if i == 0 else ())
            return
        # One search per watch over the batch; only lines it may match are
        # looked at, each matched again on its own (the line is the unit).
        starts, pos = [], 0
        for line in lines:
            starts.append(pos)
            pos += len(line) + 1
        candidates = {}
        for w in watches:
            if w.done:
                continue
            hits = (i for i, line in enumerate(lines) if w.regex.search(line)) \
                if w._joined is None else _joined_hits(w._joined, text, starts)
            for i in hits:
                ws = candidates.setdefault(i, [])
                if not ws or ws[-1] is not w:
                    ws.append(w)
        for i in sorted(candidates):
            line = lines[i]
            if self._skip(line):
                continue
            for w in candidates[i]:
                if not (i == 0 and w in fired):
                    w._offer(line, now, src.pane_id, src.name, offsets[i])

    def _replay(self, src, begin, end, watch):
        """Offer the file's [begin, end) to one new watch."""
        try:
            with open(src.path, "rb") as f:
                f.seek(begin)
                data = f.read(end - begin)
        except OSError:
            return
        typed = self._typed(src.pane_id) if watch.skip_echo else ()
        now = time.time()
        for off, line in plain_lines(data):
            if watch.done:
                break
            if not self._skip(line) and not typed_echo(line, typed):
                watch._offer(line, now, src.pane_id, src.name, begin + off)

    def _flush_partials(self):
        now = time.monotonic()
        for src in self._sources.values():
            if src.buf and now - src.last >= QUIET and not src.fired:
                src.fired = self._match(src, src.buf, src.offset - len(src.buf),
                                        self._covering(src), time.time()) or {None}

    def _skip(self, line):
        return not line.strip() or (self.ignore is not None and self.ignore.search(line))

    def _match(self, src, raw, offset, watches, now, skip=()):
        """Under the lock: offer one line to the watches on its pane."""
        if not watches:
            return set()
        line = plain_line(raw).decode("utf-8", "replace")
        if self._skip(line):
            return set()
        echo = self._is_echo(src.pane_id, line)
        matched = set()
        for w in watches:
            if w in skip or (echo and w.skip_echo):
                continue
            if w._offer(line, now, src.pane_id, src.name, offset):
                matched.add(w)
        return matched

    def _is_echo(self, pane_id, line):
        """The shell echoing a command we typed (prompt + text): consumes it."""
        pending = self._echo.get(pane_id)
        if not pending:
            return False
        now = time.monotonic()
        while pending and now - pending[0][1] > ECHO_TTL:
            pending.popleft()
        tail = line.rstrip()
        for i, (text, _) in enumerate(pending):
            # The end of a long command readline redrew after a wrap counts too.
            if text and (tail.endswith(text) or
                         (len(tail.lstrip()) >= min(len(text), 8) and text.endswith(tail.lstrip()))):
                del pending[i]
                return True
        return False


def _joined_hits(regex, text, starts):
    """Indexes of the lines of 'text' (starting at 'starts') that 'regex'
    may match. After a hit, from the next line on: a match that runs into
    the next lines doesn't hide them."""
    pos, end = 0, len(text)
    while pos <= end:
        m = regex.search(text, pos)
        if m is None:
            return
        i = bisect.bisect_right(starts, m.start()) - 1
        yield i
        if i + 1 >= len(starts):
            return
        pos = starts[i + 1]


def _drain(fd):
    try:
        while os.read(fd, 4096):
            pass
    except (BlockingIOError, InterruptedError):
        pass


class Capture:
    """Output capture over a pane, a window or the session: TmuxSession.capture().

    'target': a pane id, a window id, or None (the session). 'path': the
    file (a pane) or the directory (a window or the session). Files are
    kept after the session unless 'keep' is False."""

    def __init__(self, session, target, path, keep):
        self.session, self.target, self.path_, self.keep = session, target, path, keep
        self.stopped = False

    def __repr__(self):
        return f"<Capture {self.target or 'session'} -> {self.path_}>"

    @property
    def directory(self):
        return os.path.dirname(self.path_) if self._is_pane else self.path_

    @property
    def _is_pane(self):
        return bool(self.target and self.target.startswith("%"))

    def _pane(self, pane):
        if pane is None:
            if not self._is_pane:
                raise ValueError("this capture covers several panes: name one")
            return self.target
        return pane

    def panes(self):
        """{pane id: file} for the panes in scope that have a file."""
        return self.session._output_files(self.target)

    def path(self, pane=None) -> Optional[str]:
        """The file a pane's output goes to."""
        return self.session.output_path(self._pane(pane))

    def mark(self, pane=None):
        """Where the output is now: an offset, or {pane id: offset} for a
        window or session capture. Pass it as search(since=)."""
        if pane is None and not self._is_pane:
            return {p: file_size(f) for p, f in self.panes().items()}
        return self.session.output_mark(self._pane(pane))

    def search(self, pattern, pane=None, since=None, *, regex=True, flags=0):
        """[OutputMatch] for lines in the captured output (since a mark())."""
        target = pane if pane is not None else self.target
        return self.session.search_output(target, pattern, since=since,
                                          regex=regex, flags=flags)

    def text(self, pane=None, since=None) -> str:
        """A pane's captured output as plain text (since a mark())."""
        return self.session.output_text(self._pane(pane), since=since)

    def stop(self):
        """Stop piping the panes in scope; files are left as they are."""
        if not self.stopped:
            self.stopped = True
            self.session._stop_capture(self)


class OutputExpect:
    """TmuxSession.expect(): '.match' (an OutputMatch) after the block."""

    def __init__(self, session, target, pattern, timeout, regex, flags):
        self.session, self.target, self.pattern = session, target, pattern
        self.timeout, self.regex, self.flags = timeout, regex, flags
        self.match = None
        self.watch = None

    def __enter__(self):
        self.watch = self.session.watch_output(self.target, self.pattern, once=True,
                                               regex=self.regex, flags=self.flags)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                kind, obj = self.session._resolve_scope(self.target)
                self.match = self.session._await(self.watch, obj if kind == "pane" else None,
                                                 self.timeout)
        finally:
            self.watch.cancel()
        return False
