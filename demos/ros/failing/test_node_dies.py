"""Fails on purpose: a node dies mid-test. Expect, for each test:

    FAILED ... launched process died early: nodes:ticker exited 3
"""
import sys

from std_msgs.msg import Int64

COUNTER = f"{sys.executable} nodes/counter_node.py --topic ticks --ros-args -r __node:=ticker"
PROCESSES = {"nodes:ticker": COUNTER.replace("--topic", "--crash-after 3 --topic")}
EXPECTED_NODES = {"ticker"}


def test_module_node_dies(probe):
    """60 ticks take 12 s; the ticker dies at 3 s. Fails fast, not by timeout."""
    assert probe.wait_for_messages(Int64, "ticks", count=60, timeout=15)
