# Races and how the design avoids them

Two processes touching one pane (the library, tmux, `lte-run`, the
sidebar), or one event arriving before its listener: every bug below
happened, and each rule here is what fixed it. Keep them when changing the
code. Tests named in the last column guard them.

## Pane creation

| race | rule | where |
|---|---|---|
| A fast command exits before `remain-on-exit` is set: tmux deletes the pane and its exit status (`new_pane()` crashed) | set it **before** the command can start: on the window for the split, then on the pane; the window's own value is put back | `_new_exec_pane()`; `test_exec_pane_exiting_at_once_in_a_split` |
| `pipe-pane` run after `split-window` misses the first line | pipe in the same tmux command list as the split (`@lte_pipe_next` + an `after-split-window` hook), never as a second call | `_arm_pipe()`, `_PIPE_HOOK`; `test_capture_pane_from_first_byte` |
| A window's first pane, reused: what its login shell printed before the respawn (a prompt) was captured as an exec command's output; `env=` needed a respawn | no reused panes: a window starts with a silent placeholder, the first pane is split off it like every other, then the placeholder is killed | `_claim_pane()`, `_PLACEHOLDER`; `test_capture_exec_skips_the_starting_shell`, `test_first_pane_replaces_the_placeholder` |
| Typing into a new shell before it has started: the command echoes as output, and the rc file's helpers are taken for the target pid | wait until the prompt is drawn and no child is left, twice in a row (5 s at most). Wrap mode with `wait_start=False` skips it: lte-run reports the pid | `_wait_shell_ready()` |
| A shell command exits before its pid is seen: no target pid | an empty target list means "already exited"; the status comes from `$?` | `_capture_target_pids()` |

## Exit events

| race | rule | where |
|---|---|---|
| `watch_pane()` / `wait_for_exit()` after the exit event fired: the callback never ran, `wait_for_exit()` timed out with `unknown` | `_ended` records panes whose run ended; a late callback fires at once. Checked under `_status_lock`, which the exit handler also takes, so exactly once | `watch_pane()`, `_on_target_exit()`; `test_wait_for_exit_after_it_ended` |
| Wrap pane: a callback registered as the `exited` message arrives fired twice, or never | check-and-register under the lock the message handler takes | `watch_pane()` (wrap branch) |
| `wait_start=False`: `started` arrives before the pane is marked pending, or the command starts and exits before `on_exit` is registered | mark pending before typing; the pending check in `watch_pane()` holds `_status_lock`, which `started` takes | `_new_shell_pane()`, `watch_pane()`; `test_wait_start_false` |
| `stop_pane()` on a `starting` pane: nothing to signal yet, and the command starts after the stop | wait for `started` first (5 s at most) | `stop_pane()`; `test_stop_pane_while_starting` |
| The watcher and a caller probe a shell pane's `$?` at once: the loser reads the winner's probe (0) | the whole check-probe-cache sequence holds `_status_lock`; the first result is cached (`$?` is one-shot) | `exit_status()` |
| Exec pane: the OS pid is gone a beat before tmux records the status | `is_running()` falls back to tmux's `pane_dead`; `_read_exec_status()` polls up to 2 s | `is_running()` |
| Shell mode: a loop the shell runs itself (`while sleep 2; do curl ...; done`) has one child at a time; the first child's exit read as the end, and the `$?` probe was typed into the running loop | targets gone is not the end: the shell must sit idle (no child, not running) for 0.3 s; a new child becomes the target | `_shell_busy()`, `_on_target_exit()`; `test_shell_loop_runs_until_the_loop_ends` |
| A failed tmux query returns `""`, read as "dead" | test `#{pane_dead}` against `"1"`, never against `"0"` | `_pane_is_dead()` |
| A rerun's old pid watch fires `on_exit` for the new run | `started` (wrap) or `respawn_pane()` drops the old watch, cached status, `stopped` and `_ended` | `_on_wrap_message()`, `respawn_pane()` |

## pytest plugin

| race | rule | where |
|---|---|---|
| A process stopped right after it started died *of* SIGINT: its handler wasn't installed yet | wait for a `ready` line before stopping what must exit 0 | `test_features` (`ready="hello_42"`) |
| Two processes die together; the second lands after the test failed and teardown reported it as an unclean shutdown | a failed test aborts the group (no shutdown checks) | `pytest_runtest_makereport` |
| tmux mode: `wait_for` matched the typed command in the pane, before the process printed anything | read the `--tee` log, never `capture-pane` | `_TmuxBackend.lines()`; `test_features[tmux]` |
| `--tmux` re-exec saw no tty: pytest had already swapped fd 0 for `/dev/null` | check `/dev/tty` and the foreground group; suspend capture (stdin too) before exec | `pytest_cmdline_main` |

## Wrap mode (`lte-run`)

| race | rule | where |
|---|---|---|
| `started` arrives before the library tracks the pane, and is ignored | set the pane's mode and arm the `started` event **before** typing the command | `_send_wrapped()` |
| A watcher reads `--tee`'s file before it is complete | drain until quiet, close the pipe, **then** send `exited` | `Tee.finish()` |
| The library and `lte-run --tee` both see a free pane and pipe it | check and pipe in **one** tmux call (`if -F` on `pane_pipe` and `@lte_pipe_tee`) | `wrap.guarded_pipe()`; `test_tee_never_loses_pipe_to_capture` |
| A new tmux server reuses pane ids: `lte-run` unpipes someone else's `%0` | its closing commands run only if the server's pid matches the one it started under | `wrap._on_own_server()` |

## Output reader

| race | rule | where |
|---|---|---|
| A rerun overwrites its `--tee` file (same name, maybe same size): lines matched twice, or missed | each pipe has a fresh `@lte_pipe_id`; a shrink is read from 0, and a pipe change just after a shrink keeps the offset | `OutputReader._sync()`, `_read()`; `test_tee_overwrite_and_append` |
| An appending rerun read before its `started`: lines matched twice | a new pipe on the same file resumes at `max(offset, where it began)` | `_sync()` |
| `expect()` matched output printed before it was armed | each watch remembers where each pane's file was when it was added | `OutputWatch._from` |
| The reader finds a new pane's file as it is split, before `new_pane()` names it: a session or window watch's match had no name for up to a second | a nameless source looks its name up again on every read | `OutputReader._read()`; `test_watch_window_and_session` |
| A callback adds or cancels a watch from the reader thread | the reader's lock is reentrant | `OutputReader._lock` |

## Dashboard and status

| race | rule | where |
|---|---|---|
| A reader sees a half-written status file | write a temp file, then `os.replace()` | `_write_status_file()` |
| Under lte-session, the next test's status file replaces the last one within a poll: the last write is missed | read the old file once more on a switch; merge entries, never replace | `DashApp.poll()`; `test_sidebar_survives_status_file_switch` |
| Closing the dashboard while the sidebar swaps a pane in: `kill-window` takes the viewed pane | kill the sidebar first, then restore, then kill the window | `close_dashboard()` |
| SIGUSR1 from the window hook before its handler exists kills the sidebar | install the handler, then the hook | `DashApp._watch_window()` |
| Killing the session from inside it detaches every client | `lte-session` sends its clients away (`switch-client`, `detach-client`) first | `Launch.leave()` |
