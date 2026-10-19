# tmux defaults (`starter.py`, `lte-defaults`)

Two things, both kept out of the way of a user's own tmux setup:

| what | applies when | turn off / change |
|---|---|---|
| starter settings (`starter.CONF`) | the tmux server loaded **no** config file | `LTE_TMUX_DEFAULTS=0` |
| library keys: `prefix g`, `prefix Q` | the key is **unbound**; only in lte sessions | `@lte_key_dash`, `@lte_key_close` in `~/.tmux.conf` |

Python code never sets or unsets these: how keys work is the user's call,
made in tmux.

## Starter settings

`lte-defaults` prints them, ready to copy into `~/.tmux.conf`:

| setting | does |
|---|---|
| `setw -g mode-keys vi` | vi keys in copy mode |
| `prefix \|` / `prefix -` | split side by side / stacked |
| `prefix h` `j` `k` `l` | move to the pane left / down / up / right |
| `prefix C-x` | `kill-session` |
| `set -g mouse on` | click a pane, drag a border, wheel scrolls |

- Checked once per server, when the library first makes or adopts a session
  there: `create()`, `current()`, `lte-session`. Global `@lte_starter`
  records it (`1` sourced, `0` skipped).
- "No config" comes from tmux itself: `#{config_files}` is empty (or only
  `/dev/null`). Covers `~/.tmux.conf`, `~/.config/tmux/tmux.conf`,
  `/etc/tmux.conf` and `tmux -f FILE`.
- Server-wide (`-g`): a user with no config has set no preference.
- The tests set `LTE_TMUX_DEFAULTS=0`, so they run the same with or without
  a `~/.tmux.conf`.

## Library keys

| key | role | borrowed by | does |
|---|---|---|---|
| `prefix g` | `dash` | the dashboard sidebar, while it runs | back to the dashboard |
| `prefix Q` | `close` | `create()`, `lte-session` | `lte-close`: close actions, stop panes, kill |

- Borrowed with `lte-key` ([dashboard.md](dashboard.md#borrowed-keys-dashkeyspy-lte-key)):
  in other sessions the key does what it did before. Given back when the
  script or lte-session lets go (`lte-session --keep` keeps it).
- **Only if unbound** (or already borrowed by another lte session). A key
  the user bound is left alone: no `prefix Q` in that session.
- Pick others, or none, in `~/.tmux.conf`:

  ```tmux
  set -g @lte_key_dash  G     # default g
  set -g @lte_key_close ""    # "" = none; default Q
  ```

- An explicit key from the script (`bind_close_key("C-x")`,
  `dashboard(key="v")`, `lte-dash --key`) is borrowed even if bound: the
  user's binding becomes the fallback in other sessions.
- `Q` matches the dashboard's `Q` `Q`. Stock tmux 3.4 leaves `g` and `Q`
  unbound (*verified*); unused letters: `a b e g h j k u v y`, most capitals.
