# ROS 2 demo

ROS 2 tests on the pytest plugin (`libtmux_easy.testing`): nodes under
test are launched and watched, tests talk to them through an rclpy
`probe`, and a node that dies fails the test.

```bash
./run.sh                         # subprocesses, output in this terminal
./run.sh --tmux                  # detached tmux session: tmux attach -t ... (printed)
./run.sh --keep-open             # leave every node running after the run
./run.sh failing                 # the failing demos
lte-session ./run.sh -s          # with the dashboard
```

`run.sh` sources ROS (Jazzy) and uses this checkout's library if none is
installed. Extra args go to pytest.

| module | fixture | shows |
|---|---|---|
| `tests/test_system.py` | `processes` | a launch file + two nodes up for the module; graph, topics, a rate, services, a parameter, a node's log line |
| `tests/test_per_test_nodes.py` | `launcher` | nodes per test: join/leave the graph, restart, remap, two at once |
| `failing/test_node_dies.py` | `processes` | a node dies mid-test: fails fast, `nodes:ticker exited 3` |
| `failing/test_launched_node_dies.py` | `launcher` | a node inside `ros2 launch` dies (`lte_output_checks`); a node ignoring SIGINT fails `stop()` |

`conftest.py`:

- `lte_pane_setup`: sources ROS, this run's `ROS_DOMAIN_ID`, `cd` here
- `lte_output_checks`: finds `process has died` in launch output
- `probe` (function): `wait_for_nodes`, `wait_for_topics`, `wait_for_messages`,
  `rate`, `call`, `publish_until`. Every wait checks the test's processes.
  With `PROCESSES`, waits for `EXPECTED_NODES` first.

Notes:

- Each run picks its own `ROS_DOMAIN_ID` and localhost discovery, so two
  runs don't see each other.
- A node that should exit 0 on SIGINT must catch `KeyboardInterrupt` /
  `ExternalShutdownException` around `rclpy.spin()` (see `nodes/counter_node.py`).
- A stopped node's name can linger in the graph briefly. Give per-test
  nodes names of their own (`-r __node:=...`).
