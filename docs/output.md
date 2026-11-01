# Output: capture, search, watch (`output.py`)

Save a pane's output to a file, grep it later, or react to a line as it
is printed: `ERROR` in a log, `ready` from a server. Tour: `demos/interactive/06_output.py`.

```python
sess = TmuxSession.create("app", pane_capture="/var/log/app")   # every pane, kept
sess.new_window("web", pane_capture=True)                       # every pane in it
sess.new_pane("db", cmd, pane_capture="db.log")                 # one pane: all it shows
sess.new_pane("job", cmd, mode="wrap", cmd_capture="job.log")   # the command's output only
sess.new_pane("api", cmd, on_output={r"Traceback": on_crash})   # watched only

m = sess.wait_for_output("web:server", r"port (\d+)", timeout=10)
sess.search_output("web", r"ERROR")                             # [OutputMatch]
with sess.expect(pane, r"pong") as e:
    sess.send_keys("ping", target_pane=pane)
```

## Two captures

| | pane capture | command capture |
|---|---|---|
| set with | `pane_capture=` (`create`, `new_window`, `new_pane`), `capture()` | `cmd_capture=FILE` (`new_pane`, `send_keys`); wrap mode only, else `ValueError` |
| holds | all the pane shows: rc output, prompt, typed lines, the command | the command's output only |
| format | raw bytes, appended | plain text; each run rewrites it |
| complete | as tmux writes it | when `on_exit` fires |
| by | `pipe-pane 'exec cat >> FILE'` | `lte-run --tee FILE` (its own pipe, for the run) |

tmux has **one pipe per pane**. While a run has a command capture, the
pane's capture and watches read the run's file; when the run ends,
lte-run closes its pipe and the pane capture is not restarted.

## API

| call | does |
|---|---|
| `capture(target=None, path=None, *, keep=True)` | pipe a pane, window or the session (`None`) to files, from now on; window/session also covers panes made later. Returns `Capture`: `path(pane)`, `panes()`, `stop()` |
| `new_pane(..., pane_capture=False)` | opt out of a window or session capture |
| `search_output(target, pattern, since="start")` | `[OutputMatch]` from the files; a pane never captured: its scrollback |
| `output_text(pane, since="start")` | its plain text |
| `output_path(pane)` | its file (a capture's, or a run's `cmd_capture`); `None` if never captured |
| `output_mark(target=None)` | where the output is now, for `since=`: an offset (pane), `{pane id: offset}` (window, session). Captures what isn't yet |
| `watch_output(target, pattern, callback=None, *, once, regex, flags, since="start", skip_echo)` | `OutputWatch`: `callback(OutputMatch)` per match; `.matches` (last 1000), `.count`, `.wait(timeout)`, `.cancel()` |
| `new_pane(..., on_output={pattern: callback})` | watched from the pane's first byte |
| `wait_for_output(target, pattern, timeout=None, *, since="start")` | the first match; `TimeoutError`, or `PaneExited` (`.status`) if a pane's command ends |
| `expect(target, pattern, timeout=10)` | context manager: watch from entry, wait at exit; `.match` |
| `unwatch_output(target)` | cancel the target's watches |

`target`: a pane (object, `"window:pane"`, `%id`), a window (object, name,
`@id`), or `None` for the session. `OutputMatch`: `pane_id`, `name`,
`pattern`, `match` (`re.Match`), `line`, `time`, `offset` (in the file).

**`since`**, the same everywhere:

| value | from |
|---|---|
| `"start"` (default) | where the pane's capture began (a pane not captured yet: its scrollback) |
| `"now"` | here on |
| `output_mark()` | the mark; a pane made after a window's mark: its first byte |
| `0` | the file's first byte (an appended file's earlier runs too) |

## Files

| capture | default place | after the session |
|---|---|---|
| chosen (`capture()`, `pane_capture=`) | `$TMPDIR/lte_output_<uid>/<session>/` | kept |
| made for a watch or a mark | the same | deleted; one there already is kept; `sess.keep_output = True` keeps all |
| `cmd_capture=FILE` | FILE | kept |

- Names: `window.pane.log`; a pane made by hand, `window.%id.log`.
- A pane capture appends: a kept file collects every run of the session's name.

## Text

| input | rule |
|---|---|
| colors, OSC titles | dropped (`wrap.plain_line`, as `--tee`) |
| `\r` redraws (progress bars) | the line's last state, at `\n` |
| no newline yet (`Password: `) | matched once after 0.15 s quiet; not again at `\n` |
| a line over 64 KB | matched in pieces |
| commands the library types | not output (`skip_echo=True`); keys typed by hand are |

## How

| piece | |
|---|---|
| `@lte_pipe_file` (pane option) | where the pane's output goes now: set by the library and by lte-run, so either can start the pipe and the other finds it. Also `@lte_pipe_from` (where it began), `@lte_pipe_id` (new per pipe), `@lte_pipe_tee` (a run has it), `@lte_pipe_whole` (the library's pipe began at the pane's first byte: the plugin's `adopt()` can trust the file) |
| guard | check the pane and pipe it in one tmux call (`guarded_pipe()`), so the library and lte-run can't both take it |
| new panes | one session hook (`after-split-window[77]`, `after-new-window[77]`) pipes them from the first byte, to `@lte_pipe_next` or the window's / session's `@lte_pipe` template |
| a late capture | the scrollback is read in the same tmux call as the pipe starts: no line falls between |
| reader | one thread per session. Woken by inotify when a watched file is written (or a capture dir gets a new file); reads only the new bytes, at most every 0.1 s per file. No watches: blocked. Safety net: re-reads `@lte_pipe_file` from tmux each second while watching |
| matching | a read's lines are made plain and searched as one text per watch; only lines that may match are looked at, each matched on its own. `\A`, `\Z`, lookarounds, atomic and possessive patterns go line by line |
| cost | uncaptured panes: none. Captured, unwatched: `cat` only. Watched (*measured*): idle 0%, 1 wake-up/s; 20k lines/s with a rare match ~4% of a core; a `yes` flood ~32% |
| fallback | no inotify (ctypes, no libc symbol): polls watched files every 0.1 s |
| file changes | a new `@lte_pipe_file` or `@lte_pipe_id` (a rerun): read from `@lte_pipe_from`; a file that shrinks: from 0. Lines already matched aren't matched again |

`lte-run --tee` itself (FIFOs, `-a`, `--raw`, `$LTE_RUN_TEE`, outside
tmux): [wrap-mode.md](wrap-mode.md).

## Other output

| | |
|---|---|
| `log_home()` (`dashboard(log_home=True)`) | this script's own stdout, stderr and logging, to the dashboard's home pane |
| pytest plugin | subprocess mode: its reader thread writes each log; tmux mode: `cmd_capture=`; `build()`: `cmd_capture=` (wrap panes), else `pane_capture=`; `adopt()`: the script's capture, else its own pipe. See [pytest-plugin.md](pytest-plugin.md) |
| lte-session's home | a transcript pipe of its own: that pane can't be captured or watched |

```python
cls.procs = Processes(cls, fail_on={"*": [r"Traceback"], "servers:web": r"ERROR"})  # a match fails the test
cls.procs.start("servers:web", cmd, ready=r"Serving HTTP")   # waits for it
port = cls.procs.wait_for("servers:web", r"port (\d+)").group(1)
```

## Limits

- Exec mode can lose a command's last output: tmux 3.4 drops what a pane's
  process writes just before it exits. Wrap or shell mode keep it.
- A pane already piped by another tool can't be captured or watched.
- tmux expands formats and strftime in pipe commands: the library escapes
  `#` and `%` in paths.
- Not done: dashboard match markers.
