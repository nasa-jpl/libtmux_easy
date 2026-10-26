# Design docs

How libtmux_easy works, and why. User-facing docs: the top-level `README.md`.
Facts marked *verified* were measured (tmux 3.3a/3.4, libtmux 0.21 to 0.62,
Linux 6.x); don't re-research them.

| doc | covers |
|---|---|
| [architecture.md](architecture.md) | modules, sessions (created vs adopted), tmux access rules |
| [environment.md](environment.md) | a pane's exact environment: levels, `None`, rc files, how it is set |
| [exit-status.md](exit-status.md) | pane modes, target pids, exit events, stopping, status surfaces |
| [wrap-mode.md](wrap-mode.md) | `lte-run`: signals and the wrap protocol (public) |
| [dashboard.md](dashboard.md) | `lte-dash`: swap-pane viewport, state in tmux options, borrowed keys |
| [lte-session.md](lte-session.md) | running a command as the dashboard's home pane |
| [output-match.md](output-match.md) | pane output: `capture()` to files, `search_output()`, `watch_output()`, `expect()`, the plugin's `ready=` / `fail_on` |
| [tmux-defaults.md](tmux-defaults.md) | starter tmux settings for a server with no config; the library's keys (`g`, `Q`) and how to change them |
| [session-close.md](session-close.md) | every way to stop panes and close a session; `lte-close`, close actions, the close key |
| [pytest-plugin.md](pytest-plugin.md) | `libtmux_easy.testing`: options, `Processes`, how a test class runs, health and failure, logs, design |
| [packaging.md](packaging.md) | installs, the `.deb`s, `uv.lock` and when to update it, CI and releases |
| [releasing.md](releasing.md) | bumping the version and tagging a release |
| [races.md](races.md) | every race we hit, and the rule that avoids it |
| [KNOWN_ISSUES.md](KNOWN_ISSUES.md) | tmux bugs, limits, open bugs |

## Why it exists

Built as a test-launch backend to replace ROS `launch_testing`, whose output
drowns test output and whose exit codes miss node death. Requirements:

1. exact exit status for every process;
2. an event the moment one exits;
3. processes stay interactive (attach, type, Ctrl-C, pdb);
4. see everything at once (dashboard);
5. pytest on top.

The library itself stays generic: the ROS parts live in a separate
project, `lte_launch_test`.

To see the features run, take the tours in `demos/interactive/` (start with
`demos/interactive/01_session.py`).
