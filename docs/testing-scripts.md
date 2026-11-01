# Testing a tmux session script

A session script brings a system up in tmux: a libtmux_easy script, a
`tmux new-session ...` shell script, or `ros2 launch` under tlaunch. The
pytest plugin can test one in three ways. Pick the first one the script
allows. Demos: `demos/pytest/script_*`. Tests: `test/pytest/test_scripts.py`
(one system, all three ways). The plugin itself: [pytest-plugin.md](pytest-plugin.md).

| method | the script must | the test | restarts | exit codes |
|---|---|---|---|---|
| 1. layout | list its panes as data: `panes() -> [("window:pane", cmd)]` | `procs.start()` each | yes | exact |
| 2. build | make its panes in a `build(session)` function | `procs.build(launch.build)` | yes | exact |
| 3. adopt | nothing: run it as it is | `procs.adopt("python3 launch.py")` | no | exact for wrap and exec panes |

1 and 2 give the test full control: it owns every command. 3 tests the
script itself, its startup order included, at the cost of restarts.

## 1. Layout: the system as data

```python
# launch.py
def panes(port):
    return [("servers:web", f"web {port}"), ("jobs:migrate", "migrate")]

# test_launch.py
for name, cmd in launch.panes(PORT):
    cls.procs.start(name, cmd, ready=READY.get(name))
```

The test chooses the order, `ready=` lines, gates and modes. The script's
own startup code isn't tested. Demo: `script_layout/`.

## 2. Build: the script's code on the test's session

```python
# launch.py
def build(session, port):
    session.new_window("servers")
    session.new_pane("web", f"web {port}")
    session.wait_for_output("servers:web", r"listening", timeout=10)

def main():
    session = TmuxSession.create("app", pane_capture=True)
    build(session, port=8000)
    session.dashboard()

# test_launch.py
cls.procs.build(launch.build, port=PORT, exits={"jobs:migrate": 0})
launch.monitor(cls.procs.session, alert=cls.alerts.append)   # more of the script's code
```

- Each `new_pane()` is a process, named as in the script; logged from its first byte.
- Windows go in the run's session as `<Class>/<window>`. Always tmux.
- Panes default to wrap mode. `attach()`, `dashboard()` and the close calls do nothing.
- `procs.session` is the session `build()` got: pass it to the script's other functions.

Demo: `script_build/`.

## 3. Adopt: the script as it is

```python
cls.procs = Processes(cls)
cls.procs.adopt(f"{sys.executable} launch.py --port {PORT}", exits={"jobs:migrate": 0})
cls.procs["servers:web"].wait_for(r"GET / HTTP")
```

`adopt(cmd, name="script", ready=None, timeout=60, exits=None)`, called
first in the group:

1. Runs `cmd` as process `name` on a **tmux server of its own**
   (`TMUX_TMPDIR`). The script's session names can't clash with yours, and
   its panes don't show in the run's dashboard. `LTE_HEADLESS=1`:
   `attach()` and `dashboard()` do nothing.
2. Waits for the script to exit 0 within `timeout`. With `ready=`, it waits
   for a matching line instead, and the script stays a process of the group.
3. Makes every pane on that server a process, named `window:pane`
   (`new_pane()`'s name, else the pane index; `session.window:pane` if
   there are several sessions). Returns their `Process`es.

| | wrap pane | exec pane, plain tmux command | shell pane |
|---|---|---|---|
| found as | `@lte_mode` | `@lte_mode`; untagged, not a shell or `sh -c` | `@lte_mode`; untagged, an interactive shell running something |
| running, deaths | yes | yes | while the shell has children |
| exit code | exact (lte-run's status file) | exact with `remain-on-exit`; else unknown | unknown; a Ctrl-C stop counts as clean |
| output | from the first byte: the script's capture (`pane_capture=True`); else the scrollback, then a pipe (or the script's late one). `log()` is the group's file | same | same |
| stop | Ctrl-C, then SIGTERM, SIGKILL | same | same |

- An idle shell (a prompt, nothing running) is not adopted.
- A plain-tmux pane gone with no status reads as `unknown`: pass
  `stop_codes={"*": (0, 130)}` or accept it with `exits=`.
- On failure the server is kept: `tmux -S <socket> attach` is printed.

Demo: `script_adopt/`. A plain tmux script works too:

```python
# up.sh:  tmux new-session -d -s app -n web 'web'; tmux new-window -t app -n worker 'worker'
cls.procs = Processes(cls, stop_codes={"*": (0, 130)})
cls.procs.adopt("sh up.sh")
cls.procs["web:0"].wait_for(r"listening")
```

## Making a script easy to test

| do | why |
|---|---|
| `mode="wrap"` | exact exit codes, in every method |
| name every pane (`new_pane("web", ...)`) | stable test names: `servers:web`, not `servers:0` |
| `pane_capture=True` in `create()` | adopt reads every line from the first byte |
| a `build(session)` function, `main()` around it | method 2; the script still runs on its own |
| take ports and paths as arguments | tests run beside a live copy |
| print a ready line, or exit when up | adopt's `ready=` or its exit-0 wait |

## Limits

- Adopted panes can't be restarted (`start()` raises): the plugin doesn't own their commands.
- Shell-pane exit codes are unknown: use wrap mode.
- `adopt()` and `build()` must be the group's first call, before `start()`.
