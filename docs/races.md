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
| Exec mode on a window's first pane (reused): its login shell prints its prompt after the pipe starts, before the respawn, and the prompt is captured as the command's output | respawn the shell away (a silent `sleep`) **before** piping, then respawn with the command | `_claim_pane()`, `_SILENT`; `test_capture_exec_skips_the_starting_shell` |
| Typing into a new shell before it has started: the command echoes as output, and the rc file's helpers are taken for the target pid | wait until the prompt is drawn and no child is left, twice in a row (5 s at most) | `_wait_shell_ready()` |
| A shell command exits before its pid is seen: no target pid | an empty target list means "already exited"; the status comes from `$?` | `_capture_target_pids()` |

## Exit events

| race | rule | where |
|---|---|---|
| `watch_pane()` / `wait_for_exit()` after the exit event fired: the callback never ran, `wait_for_exit()` timed out with `unknown` | `_ended` records panes whose run ended; a late callback fires at once. Checked under `_status_lock`, which the exit handler also takes, so exactly once | `watch_pane()`, `_on_target_exit()`; `test_wait_for_exit_after_it_ended` |
| Wrap pane: a callback registered as the `exited` message arrives fired twice, or never | check-and-register under the lock the message handler takes | `watch_pane()` (wrap branch) |
| The watcher and a caller probe a shell pane's `$?` at once: the loser reads the winner's probe (0) | the whole check-probe-cache sequence holds `_status_lock`; the first result is cached (`$?` is one-shot) | `exit_status()` |
| Exec pane: the OS pid is gone a beat before tmux records the status | `is_running()` falls back to tmux's `pane_dead`; `_read_exec_status()` polls up to 2 s | `is_running()` |
| A failed tmux query returns `""`, read as "dead" | test `#{pane_dead}` against `"1"`, never against `"0"` | `_pane_is_dead()` |
| A rerun's old pid watch fires `on_exit` for the new run | `started` (wrap) or `respawn_pane()` drops the old watch, cached status, `stopped` and `_ended` | `_on_wrap_message()`, `respawn_pane()` |

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
| A callback adds or cancels a watch from the reader thread | the reader's lock is reentrant | `OutputReader._lock` |

## Dashboard and status

| race | rule | where |
|---|---|---|
| A reader sees a half-written status file | write a temp file, then `os.replace()` | `_write_status_file()` |
| Under lte-session, the next test's status file replaces the last one within a poll: the last write is missed | read the old file once more on a switch; merge entries, never replace | `DashApp.poll()`; `test_sidebar_survives_status_file_switch` |
| Closing the dashboard while the sidebar swaps a pane in: `kill-window` takes the viewed pane | kill the sidebar first, then restore, then kill the window | `close_dashboard()` |
| SIGUSR1 from the window hook before its handler exists kills the sidebar | install the handler, then the hook | `DashApp._watch_window()` |
| Killing the session from inside it detaches every client | `lte-session` sends its clients away (`switch-client`, `detach-client`) first | `Launch.leave()` |
