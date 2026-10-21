"""Nodes a test brings up and down itself: the 'launcher' fixture. What a
test leaves running is stopped after it.

    ./run.sh tests/test_per_test_nodes.py [--tmux]
"""
import sys

from std_msgs.msg import Int64, String

COUNTER = f"{sys.executable} nodes/counter_node.py"


def test_node_joins_and_leaves_the_graph(probe, launcher):
    launcher.start("nodes:ticker", f"{COUNTER} --topic ticks --ros-args -r __node:=ticker")
    probe.wait_for_nodes({"ticker"})
    assert probe.wait_for_messages(Int64, "ticks")
    launcher.stop("nodes:ticker")  # exits 0 on SIGINT, or this fails
    probe.wait_for_nodes_gone({"ticker"})


def test_restart_starts_counting_again(probe, launcher):
    launcher.start("nodes:counter", f"{COUNTER} --period 0.05")
    probe.spin_until(lambda: probe.wait_for_messages(Int64, "counter")[0].data > 20, 10,
                     "the count past 20")
    launcher.stop("nodes:counter")
    launcher.start("nodes:counter", f"{COUNTER} --period 0.05")
    [first] = probe.wait_for_messages(Int64, "counter", timeout=10)
    assert first.data < 20


def test_remapped_talker(probe, launcher):
    launcher.start("demo:talker", "ros2 run demo_nodes_cpp talker"
                   " --ros-args -r chatter:=babble -r __node:=babbler",
                   ready=r"Publishing: 'Hello World: 1'")
    [msg] = probe.wait_for_messages(String, "babble")
    assert msg.data.startswith("Hello World")
    assert "/babble" in probe.topic_types()


def test_two_counters_on_their_own_topics(probe, launcher):
    for name in ("left", "right"):
        launcher.start(f"pair:{name}", f"{COUNTER} --topic {name} --ros-args -r __node:={name}")
    probe.wait_for_nodes({"left", "right"})
    assert probe.wait_for_messages(Int64, "left") and probe.wait_for_messages(Int64, "right")
