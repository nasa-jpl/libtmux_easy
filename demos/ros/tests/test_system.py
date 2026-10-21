"""The system under test, up for the whole module: a launch file and two
nodes. Every test talks to it through the probe.

    ./run.sh tests/test_system.py [--tmux | --keep-open]
"""
import re
import sys

from example_interfaces.srv import AddTwoInts
from rcl_interfaces.srv import GetParameters
from std_msgs.msg import Int64, String
from std_srvs.srv import Trigger

PROCESSES = {
    "launch:system": "ros2 launch launch/system_launch.py",
    "nodes:adder": "ros2 run demo_nodes_cpp add_two_ints_server",
    "nodes:ticker": f"{sys.executable} nodes/counter_node.py --topic ticks --period 0.1"
                    " --ros-args -r __node:=ticker",
}
EXPECTED_NODES = {"talker", "listener", "counter", "add_two_ints_server", "ticker"}
FAIL_ON = {"*": [r"Traceback"]}


def test_graph(probe):
    topics = probe.wait_for_topics({"/chatter", "/counter", "/ticks"})
    assert topics["/chatter"] == ["std_msgs/msg/String"]
    assert topics["/counter"] == ["std_msgs/msg/Int64"]
    assert topics["/ticks"] == ["std_msgs/msg/Int64"]


def test_chatter_counts_up(probe):
    msgs = probe.wait_for_messages(String, "chatter", count=3)
    numbers = [int(re.fullmatch(r"Hello World: (\d+)", m.data).group(1)) for m in msgs]
    assert numbers == list(range(numbers[0], numbers[0] + 3))


def test_listener_hears_us(probe, processes):
    # The listener only logs what it hears: read the launch's output.
    heard = lambda: "I heard: [hello from the probe]" in processes.output("launch:system")
    probe.publish_until(String, "chatter", String(data="hello from the probe"), heard,
                        what="the listener to log our message")


def test_add_two_ints(probe):
    assert probe.call(AddTwoInts, "add_two_ints", AddTwoInts.Request(a=2, b=40)).sum == 42


def test_counter_resets(probe, processes):
    [before] = probe.wait_for_messages(Int64, "counter")
    assert probe.call(Trigger, "counter/reset", Trigger.Request()).success
    [after] = probe.wait_for_messages(Int64, "counter")
    assert after.data < before.data or before.data < 3
    processes.wait_for("launch:system", r"reset at \d+")


def test_ticker_rate(probe):
    assert 7 <= probe.rate(Int64, "ticks") <= 13  # --period 0.1: 10 Hz


def test_talker_parameter(probe):
    reply = probe.call(GetParameters, "talker/get_parameters",
                       GetParameters.Request(names=["use_sim_time"]))
    assert reply.values[0].bool_value is False
