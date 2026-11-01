# Future development

Open items and ideas, staged before they are worked on. Bugs that affect
users today go in [KNOWN_ISSUES.md](KNOWN_ISSUES.md); when an item here is
done, delete it (the commit is the record).

| status | meaning |
|---|---|
| next | agreed, worth doing soon |
| later | agreed, not urgent |
| idea | not decided |

## pytest plugin: session scripts ([testing-scripts.md](testing-scripts.md))

| status | item | notes |
|---|---|---|
| next | An lte_launch_test test that adopts a real ros2_launch `tlaunch` session | the real-world check of `adopt()` |
| later | Restart adopted panes | the plugin doesn't own their commands; could respawn with the pane's `@lte` command, for wrap panes |
| later | Exit codes for adopted shell panes | unknown today; only wrap mode knows |
| idea | `PaneStatus.command` for adopted panes | `""` today; wrap panes could report lte-run's command |
| idea | A line in flight may come twice when `adopt()` follows a script's late watch pipe | rare; dedupe by offset if it bites |

## Output ([output.md](output.md))

| status | item | notes |
|---|---|---|
| later | Dashboard match markers | show watch matches in the dashboard |
| idea | A script that watches a pane and exits leaves the pane piped to its watch file, which grows and is never deleted | the session outlives the script; unpipe watch-only panes at exit, or document |

## Shell mode ([exit-status.md](exit-status.md))

| status | item | notes |
|---|---|---|
| later | Enter typed by hand on a pane left unsubmitted (`enter=False`) is not seen | status stays unknown; wrap mode sees it |
| later | Keys sent without Enter into a running command land on the next prompt, and the `$?` probe submits them | left as is so running commands behave the same |
| later | `respawn_pane()` on an unsubmitted pane types after the half-typed text | clear the line first (`C-u`) |
| idea | A run started by Enter after `enter=False` uses a fixed 1 s target-pid timeout | could reuse the pane's `target_pid_timeout` |

## Setup and typing (`send_command_before()`, `wait_start=False`)

| status | item | notes |
|---|---|---|
| next | Exec mode: `respawn_pane()` reruns without the setup | open bug in KNOWN_ISSUES |
| later | Wrap mode types `setup; LTE_RUN_SETUP=$? lte-run cmd` on one line: a setup ending in `;` or `&`, or with a `#` comment, breaks it | wrap the setup in `{ ...; }`, or reject such setups |
| idea | Shell mode leaves the setup's `$?` probe line in the pane | watches skip it; raw captures show it |
| idea | A `PS1` ending in a newline makes the prompt's echo bare: the spare-echo bug returns there only | rare |

## Project

| status | item | notes |
|---|---|---|
| next | Merge `pytest-launcher` to `main` | |
