# Known issues

## tmux

**tmux can lose an exec pane's exit status.**
- In exec mode the command is the pane's process, so only tmux (its parent)
  can collect its status. Sometimes tmux never reaps it: the process stays a
  zombie, `pane_dead=1`, and `pane_dead_status` / `_signal` are never set.
  Only the parent can read a zombie's status, so it is lost for good.
- Reproduced with raw tmux, no library code. Seen on tmux 3.3a and 3.4;
  newer versions untested.
- Rare alone (0 in 65). Common for SIGINT'd panes on tmux 3.4, and under
  pytest for a command that sleeps then exits (2 in 3; an immediate exit:
  0 in 6).
- Ruled out: the server's signal mask, our watcher, status-line jobs. Root
  cause unknown; a race in tmux's child reaping (SIGCHLD) is a guess.
- Effect: `exit_status()` returns unknown and logs a warning. Exec-mode
  tests `xfail` on it (`_exec_status()` in `test_session.py`).
- **Workaround: wrap mode.** `lte-run` is the command's parent and reports
  the status itself. The main reason wrap mode exists.

**tmux drops output written just before a pane's process exits.**
- tmux 3.4, raw tmux: gone from the screen, `pipe-pane` and control-mode
  `%output` with an immediate exit; kept with 0.1 s to spare.
- Hits exec mode only (the command is the pane's process). Effect: a
  capture, an output watch or `capture-pane` misses a crash's last line.
- **Workaround: wrap or shell mode** (the shell outlives the command).

## Limits by design

| issue | workaround |
|---|---|
| Shell mode guesses the target pid: can catch a `.bashrc` process or a shell loop's transient child, so a running pane looks finished | one long-lived process per pane, or wrap mode |
| Shell mode: `exit 130` and SIGINT look the same (`inferred`) | wrap mode |
| `lte-run a && b` typed by hand supervises only `a` | `new_pane()` wraps in `bash -c` |
| A fast command can record no target pid (`target_pids == ()`) | handled |
| `kill_session()` only sends SIGHUP | `stop_all()` first |
| A Ctrl-C typed by hand counts as failed | none: intent is unknowable |
| Linux only (pidfds, `/proc`) | |
| One pipe per pane (tmux): a pane piped by another tool (lte-session's home) can't be captured or watched | wrap the command in a pane of its own |
| `pipe-pane` run after a split misses the first line | the library chains it into the split, or a hook does |
| A watch keeps its last 1000 matches (`.count` has all) | a callback |
| `refresh-client -A '%id:off'` (per-pane output filter) segfaults tmux 3.4 with a normal client attached | not used: pipes instead |

## pytest plugin

| limit | workaround |
|---|---|
| A restarted process's log: subprocesses append, tmux rewrites it | `output()` is the latest start in both |
| No stdout/stderr split: a pty (tmux) or one pipe (subprocess) merges them | match on content |
| The `--tmux` session closes when pytest ends, even on failure | `--keep-open` |
| `pytest.skip` after an abort reports the plugin's file as its location | the reason names the failed test |

## Open bugs

- `wait_for_output()` on a pane nobody watched or captured from its start
  can miss a line printed before the call (about 1 in 6: a fresh wrap pane,
  `http.server`'s first line; also on `main`). The late pipe's `cat` is
  gone and its file stays empty, though tmux shows the pane piped. Panes
  piped from creation (`capture=`, `on_output=`, `tee=`, or a watch made
  first) never miss it (0 in 16).

- Shell mode, `enter=False`: still probed for `$?`, which submits the
  half-typed line. Wrap mode handles it.
- `PaneStatus.command` is stale after a rerun typed by hand.
- `wait_for_exit()` registers a callback per call; they accumulate on the pane.

`wait_start=False` (fast bringup, docs/wrap-mode.md):
- `send_command_before()` isn't waited for: the setup line and `lte-run
  cmd` are typed back to back. bash runs them in order, but `cmd` runs even
  if the setup failed, and nothing reports the failure.
- The echo is expected twice for 10 s (`ECHO_TTL`). If the shell was already
  up, it echoes once, and the spare can hide one real output line ending
  with the command's text.
- Dashboard: a `starting` pane whose window closed shows `starting`, not
  `gone` (`DashApp`, the `kind == "running"` check).
- `test_testing.py::test_adopt` is flaky (about 1 in 2, either param; also
  before this change): `sim:a: no line matching 'hello_42' within 10s` in
  `TestDies`.

## Running tests

- Don't run two suites (or two tours) at once on the default tmux server:
  fixed session names collide, and `kill_existing=True` kills the other run's
  sessions.
- With DEBUG logging, libtmux emits ~400 KB of noise: `grep -v '^DEBUG'`.
- Python's fallback log handler prints warnings with no "WARNING" prefix.
