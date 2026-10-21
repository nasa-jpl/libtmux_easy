"""Fails on purpose. Expect:

    test_launch_node_dies   FAILED ... launch:system > counter-3 (exit code 3)
    test_unclean_stop       FAILED ... nodes:stubborn did not stop cleanly: stopped (SIGTERM)
"""
import sys

from std_msgs.msg import Int64

COUNTER = f"{sys.executable} nodes/counter_node.py"


def test_launch_node_dies(probe, launcher):
    """A node inside 'ros2 launch' dies; the launch keeps running. Only its
    output says so, which lte_output_checks reads."""
    launcher.start("launch:system", "ros2 launch launch/system_launch.py crash_after:=2")
    assert probe.wait_for_messages(Int64, "counter", count=50, timeout=15)


def test_unclean_stop(probe, launcher):
    launcher.start("nodes:stubborn", f"{COUNTER} --ignore-sigint --ros-args -r __node:=stubborn")
    probe.wait_for_nodes({"stubborn"})
    launcher.stop("nodes:stubborn", sigint_timeout=2)
