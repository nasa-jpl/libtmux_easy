# Pane environment

Every pane the library makes starts with exactly the environment you give
it: nothing from the tmux server's, nothing an rc file exports.

## Quick start

```python
s = TmuxSession.create("demo", env={"ROS_DOMAIN_ID": "7"})   # on top of os.environ
s.new_window("w", env={"LOG": "debug"})
s.new_pane("p", "my_node", env={"SECRET": None})              # None removes
```

## What a pane gets

`os.environ`, then the session's `env=` / `copy_environment()`, the
window's, the pane's; later wins, `None` removes, `SKIP_ENV` never goes in.
Plus what tmux always sets itself:

| variable | from |
|---|---|
| `TMUX`, `TMUX_PANE`, `TERM`, `TERM_PROGRAM*` | the pane |
| `SHELL` | the `default-shell` option |
| `PWD`, `SHLVL` | the start dir; +1 |

`SKIP_ENV` (`_tmux.py`): `TMUX*`, `TERM*`, `COLUMNS`, `LINES`, `LTE_DIR`,
`BASH_ENV`. `BASH_ENV` would make each bash script the command starts
source it, and a container's that sources ROS drops a sourced overlay.

## Shell and wrap panes: your rc, our env

The pane runs `bash --rcfile lte_rc.bash -i -s -- RC`. `lte_rc.bash`:

1. sources `RC` (`create(rc=)` / `current(rc=)`, default `~/.bashrc`;
   `None` for none). Debian's bash also reads `/etc/bash.bashrc` first;
2. unexports everything, then re-exports `/proc/$$/environ`, the
   environment tmux started the shell with.

Aliases, functions, the prompt and shell options stay; exports are undone.
This happens once, at shell start: after that the shell is yours. Stop the
command, `export X=...`, rerun, and the run gets it.

Not undone: a `PROMPT_COMMAND` hook that changes the environment on every
prompt (direnv, conda auto-activate). No bash: plain `sh -i`, no rc.

Exec panes run the command directly: no shell, no rc.

## How it is done

tmux copies the session's environment into a new pane, and `-e` is capped
by tmux's 16 KB command limit (a sourced ROS workspace is ~11 KB). So each
split or respawn is one `tmux source-file -` call, commands on stdin:

1. `set-environment` each variable of the pane's env on the session;
2. `set-environment -r` every other session or global variable (hidden
   from the pane);
3. the `split-window` / `respawn-pane`;
4. the session's environment put back as it was.

One call: no length limit, no other client's command in between; the
restore runs even if the split fails. The session ends as it began, created
or adopted. Only `copy_environment()` on a created session changes it for
good, so panes made by hand get that too.

`PATH` is the exception: tmux gives a new pane the tmux *client's* PATH,
not the session's or `-e`'s. So the client runs with the pane's PATH.

Code: `_spawn()`, `_source()`, `_pane_environment()`, `_shell_command()`
in `tmux_lib.py`.

## Verified (tmux 3.3a, 3.4)

| fact | |
|---|---|
| command over ~16300 bytes: `command too long` | `-e` can't carry a big env |
| pane PATH = client's PATH, over session env and `-e` | client runs with the pane's PATH |
| session `-r` hides a global variable from new panes | removal per pane |
| `set-environment X 'a;'` in a command line stores `a` | values go through `source-file`, quoted |
| newlines, quotes, `$`, `#{}`, `%`, empty values survive `source-file` | |
| a login shell's `/etc/profile` resets PATH | we use `--rcfile`, not `-l` |

## Troubleshooting

| symptom | cause |
|---|---|
| a variable from `~/.bashrc` is missing | by design; export it in your script's env, or type it in the pane |
| `PATH` in a pane lacks a dir | not in `os.environ` or `env=`: pass it |
| a command not found in a wrap pane | PATH; lte-run itself is called by full path |
