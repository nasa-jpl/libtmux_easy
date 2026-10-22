# Output: capture, search, watch (`output.py`)

Save a pane's output to a file, grep it later, or react to a line as it
is printed: `ERROR` in a log, `ready` from a server. Tour: `demos/interactive/06_output.py`.

```python
sess = TmuxSession.create("app", capture="/var/log/app")   # every pane, kept
sess.new_window("web", capture=True)                       # every pane in it
sess.new_pane("db", cmd, capture="db.log")                 # one pane
sess.new_pane("api", cmd, on_output={r"Traceback": on_crash})  # watched only

m = sess.wait_for_output("web:server", r"port (\d+)", timeout=10)
sess.search_output("web", r"ERROR")                         # [OutputMatch]
with sess.expect(pane, r"pong") as e:
    sess.send_keys("ping", target_pane=pane)
```

## API

| call | does |
|---|---|
| `capture(target=None, path=None, *, keep=True)` | pipe a pane, window or the session (`None`) to files, from now on; window/session also covers panes made later. Returns `Capture` |
| `capture=` on `create()`, `new_window()`, `new_pane()` | `True` or a path; from the first byte. `new_pane(capture=False)` opts out of a scope |
| `Capture`: `path(pane)`, `mark()`, `search(pattern, since=)`, `text(pane, since=)`, `stop()` | |
| `search_output(target, pattern, since=None)` | `[OutputMatch]` from the files; a pane never captured: its scrollback |
| `output_path(pane)`, `output_mark(pane)`, `output_text(pane, since=)` | the pane's file (a capture's or `lte-run --tee`'s), its size, its text |
| `watch_output(target, pattern, callback=None, *, once, regex, flags, since, skip_echo)` | `OutputWatch`: `callback(OutputMatch)` per match; `.matches` (last 1000), `.count`, `.wait(timeout)`, `.cancel()` |
| `wait_for_output(target, pattern, timeout=None, *, since="start")` | the first match; `TimeoutError`, or `PaneExited` (`.status`) if a pane's command ends |
| `expect(target, pattern, timeout=10)` | context manager: watch from entry, wait at exit; `.match` |
| `new_pane(..., on_output={pattern: callback})` | watched from the pane's first byte |
| `unwatch_output(target)` | cancel the target's watches |

`target`: a pane (object, `"window:pane"`, `%id`), a window (object, name,
`@id`), or `None` for the session. `OutputMatch`: `pane_id`, `name`,
`pattern`, `match` (`re.Match`), `line`, `time`, `offset` (in the file).

## Files

| capture | default place | after the session |
|---|---|---|
| chosen (`capture`, `capture=`) | `$TMPDIR/lte_output_<uid>/<session>/` | kept |
| watch only (`watch_output`, `on_output`) | `$TMPDIR/lte_watch_<session>_*/` | deleted; `sess.keep_output = True` keeps them |
| `lte-run --tee FILE` | FILE | kept |

- Raw bytes (colors kept), appended. Matching and `text()` use plain text.
- Names: `window.pane.log`; a pane made by hand, `window.%id.log`.

## How

| piece | |
|---|---|
| pipe | `pipe-pane -O 'exec cat >> FILE'`: one `cat` per captured pane; tmux has one pipe per pane |
| `@lte_pipe_file` (pane option) | where the pane's output goes: set by the library and by lte-run, so either can start the pipe and the other finds it |
| new panes | one session hook (`after-split-window[77]`, `after-new-window[77]`) pipes them from the first byte, to `@lte_pipe_next` or the window's / session's `@lte_pipe` template |
| reader | one thread per session. Woken by inotify when a watched file is written (or a capture dir gets a new file); reads only the new bytes, at most every 0.1 s per file. No watches: blocked. Safety net: re-reads `@lte_pipe_file` from tmux each second while watching |
| matching | a read's lines are made plain and searched as one text per watch; only lines that may match are looked at, each matched on its own. `\A`, `\Z`, lookarounds, atomic and possessive patterns go line by line |
| cost | uncaptured panes: none. Captured, unwatched: `cat` only. Watched (*measured*): idle 0%, 1 wake-up/s; 20k lines/s with a rare match ~4% of a core; a `yes` flood ~32% |
| fallback | no inotify (ctypes, no libc symbol): polls watched files every 0.1 s |
| file changes | a new `@lte_pipe_file` or `@lte_pipe_id` (a rerun): read from `@lte_pipe_from`; a file that shrinks: from 0. Lines already matched aren't matched again |
| memory | each watch keeps its last 1000 matches; `.count` counts all |

`lte-run --tee`: takes over the pane's capture pipe for the run (plain text
by default), closes it when the command ends. A rerun overwrites FILE (a
watch follows it), `-a` appends.

## Text

| input | rule |
|---|---|
| colors, OSC titles | dropped (`wrap.plain_line`, as `--tee`) |
| `\r` redraws (progress bars) | the line's last state, at `\n` |
| no newline yet (`Password: `) | matched once after 0.15 s quiet; not again at `\n` |
| a line over 64 KB | matched in pieces |
| commands the library types | not output (`skip_echo=True`); keys typed by hand are |
| `since="now"` / `"start"` | from now / from the capture's start (a pane not captured yet: its scrollback) |

## pytest plugin

```python
READY = {"servers:web": r"Serving HTTP"}  # waited for before the first test
FAIL_ON = {"*": [r"Traceback"], "servers:web": r"ERROR"}  # a match fails the test

def test_x(processes):
    port = processes.wait_for("servers:web", r"port (\d+)").group(1)
```

`READY` errors the fixture with the last output; `FAIL_ON` (a regex or a
function `line -> problem or None`) is recorded like a death; `wait_for`
fails on a timeout or an exit. Also `wait_for("*", ...)` (any process) and
`wait_for_in_order()`. Shell mode matches in its reader thread, tmux mode
reads the `--tee` log; same text rules. See [pytest-plugin.md](pytest-plugin.md).

## Limits

- Exec mode can lose a command's last output: tmux 3.4 drops what a pane's
  process writes just before it exits. Wrap or shell mode keep it.
- A pane already piped by another tool (lte-session's home transcript) can't
  be captured or watched.
- tmux expands formats and strftime in pipe commands: the library escapes
  `#` and `%` in paths.
- Not done: dashboard match markers.
