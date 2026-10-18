# lte-session

```bash
lte-session -- pytest -s tests/
```

CMD runs as the dashboard's **home pane**, a real terminal: `print()`,
`input()`, pdb and Ctrl-C work. Leaving prints its scrollback in your
terminal, and the exit code is CMD's.

## Startup

1. Save env and cwd to the run dir; `new-session -d`.
2. Set `history-limit`. It applies only to later panes, hence a throwaway
   first window.
3. Open the dashboard with home running `cat`.
4. Set `remain-on-exit` and `pipe-pane` to a transcript. Not a library
   capture: `capture()` skips the home pane, and a watch on it fails.
5. `respawn-pane -k` home with the runner.

The runner restores env and cwd (keeping the pane's `TMUX`, `TMUX_PANE`,
`TERM`), then runs `wrap.main(argv, channel=_ExitFile)`: the exact status
goes to `exit.json`. CMD gets your shell's environment, not the tmux server's.

## Attach and exit

| context | behavior |
|---|---|
| TTY, outside tmux | attach |
| inside tmux | `switch-client`, then wait until no client is attached (killing the session would detach you) |
| no TTY (CI) | follow the transcript |

When CMD ends, every client leaves: the one `switch-client` brought in goes
back to its session, the rest detach. Not `kill-session`: that would detach
them all (`detach-on-destroy`). With `--keep` they stay (a message says
"Detach to finish") and the session is left up afterwards.

After: print `capture-pane -p -e -J -S -` minus tmux's "Pane is dead" line,
then exit with CMD's status.

## Scripts inside it

Scripts adopt the session with `TmuxSession.current()`
([architecture.md](architecture.md)):

- the existing dashboard is reused;
- status files go in `@lte_run_dir`, so the sidebar keeps each test's final
  state. It merges entries across files and re-reads the old one once when
  `@lte_status` moves;
- `release(keep_windows=True, label=)` keeps windows as `<label>/<window>`
  for inspection; a later `new_window()` can reuse the plain name.

Without Textual, or with `--no-dash`: home pane only.
