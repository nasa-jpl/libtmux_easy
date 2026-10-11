# Known issues

## tmux

**tmux can lose an exec pane's exit status.**
- Reproduced with raw tmux, no library code: tmux never reaps the pane
  process. Zombie, `pane_dead=1`, no `pane_dead_status` / `_signal`.
- Rare alone (0 in 65). Common for SIGINT'd panes on tmux 3.4, and under
  pytest for a command that sleeps then exits (2 in 3; an immediate exit:
  0 in 6).
- Ruled out: the server's signal mask, our watcher, status-line jobs. Root
  cause unknown.
- Effect: `exit_status()` returns unknown and logs a warning.
  `test_signal_distinguished_from_code` is `xfail`.
- **Workaround: wrap mode.** The main reason it exists.

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

## Open bugs

- Shell mode, `enter=False`: still probed for `$?`, which submits the
  half-typed line. Wrap mode handles it.
- `PaneStatus.command` is stale after a rerun typed by hand.
- `wait_for_exit()` registers a callback per call; they accumulate on the pane.

## Flaky

- `demos/demo_launch_like.py` fails now and then: the shell-mode pid guess.

## Running tests

- Don't run two suites (or two demos) at once on the default tmux server:
  fixed session names collide, and `kill_existing=True` kills the other run's
  sessions.
- With DEBUG logging, libtmux emits ~400 KB of noise: `grep -v '^DEBUG'`.
- Python's fallback log handler prints warnings with no "WARNING" prefix.
