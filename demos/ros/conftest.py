"""ROS 2 on libtmux_easy's pytest plugin (libtmux_easy.testing).

The plugin runs 'processes' (the module's PROCESSES) and 'launcher' (a
test's own) and fails a test when one dies. This adds:

  - each run its own ROS_DOMAIN_ID, sourced into every process
  - a check for nodes that die inside 'ros2 launch'
  - probe: an rclpy node to talk to the system with. Every wait checks the
    test's processes, so a dead node fails fast, not by timeout. With
    PROCESSES, it waits for the module's EXPECTED_NODES first.
"""
import os
import re
import shlex
import time
from pathlib import Path

import pytest

if "ROS_DISTRO" not in os.environ:
    pytest.exit("Source ROS 2 first: source /opt/ros/jazzy/setup.bash", returncode=2)

# Before rclpy.init(), and given to every process: keep this run's DDS
# traffic to itself and to this machine.
os.environ.setdefault("ROS_DOMAIN_ID", str(20 + os.getpid() % 80))
os.environ.setdefault("ROS_AUTOMATIC_DISCOVERY_RANGE", "LOCALHOST")

import rclpy  # noqa: E402

pytest_plugins = ["libtmux_easy.testing"]

ROOT = Path(__file__).resolve().parent

# A node the launch reports as dead: the launch itself keeps running and
# exits 0 on SIGINT regardless, so this line is the only evidence.
_LAUNCH_DIED = re.compile(r"\[ERROR\] \[([\w.-]+)\]: process has died \[pid \d+, exit code (-?\d+)")


@pytest.fixture(scope="session")
def lte_pane_setup():
    return (f"source /opt/ros/{os.environ['ROS_DISTRO']}/setup.bash"
            f" && export ROS_DOMAIN_ID={os.environ['ROS_DOMAIN_ID']}"
            f" ROS_AUTOMATIC_DISCOVERY_RANGE={os.environ['ROS_AUTOMATIC_DISCOVERY_RANGE']}"
            f" && cd {shlex.quote(str(ROOT))}")


@pytest.fixture(scope="session")
def lte_output_checks():
    def launch_deaths(name, output):
        return [f"{name} > {node} (exit code {code})"
                for node, code in _LAUNCH_DIED.findall(output)]
    return [launch_deaths]


class Probe:
    """An rclpy node the tests use to poke the system."""

    def __init__(self, node, health):
        self.node = node
        self.health = health

    def spin_until(self, done, timeout, what):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rclpy.spin_once(self.node, timeout_sec=0.1)
            self.health()
            result = done()
            if result:
                return result
        raise AssertionError(f"timed out after {timeout}s waiting for {what}")

    def nodes(self):
        return set(self.node.get_node_names())

    def wait_for_nodes(self, names, timeout=20.0):
        self.spin_until(lambda: set(names) <= self.nodes(), timeout, f"nodes {sorted(names)}")

    def wait_for_nodes_gone(self, names, timeout=10.0):
        self.spin_until(lambda: not set(names) & self.nodes(), timeout,
                        f"nodes {sorted(names)} to leave the graph")

    def topic_types(self):
        return dict(self.node.get_topic_names_and_types())

    def wait_for_topics(self, names, timeout=10.0):
        """The topic types, once every topic in 'names' has a type."""
        try:
            return self.spin_until(lambda: set(names) <= set(self.topic_types())
                                   and self.topic_types(), timeout, f"topics {sorted(names)}")
        except AssertionError as exc:
            raise AssertionError(f"{exc}; have {sorted(self.topic_types())}") from None

    def wait_for_messages(self, msg_type, topic, count=1, timeout=5.0):
        received = []
        sub = self.node.create_subscription(msg_type, topic, received.append, 10)
        try:
            self.spin_until(lambda: len(received) >= count, timeout, f"{count} on {topic}")
            return received[:count]
        finally:
            self.node.destroy_subscription(sub)

    def rate(self, msg_type, topic, seconds=2.0):
        """Messages per second on 'topic', counted for 'seconds'."""
        received = []
        sub = self.node.create_subscription(msg_type, topic, received.append, 10)
        try:
            self.spin_until(lambda: received, 5.0, f"a message on {topic}")
            received.clear()
            end = time.monotonic() + seconds
            self.spin_until(lambda: time.monotonic() >= end, seconds + 1, "the count")
            return len(received) / seconds
        finally:
            self.node.destroy_subscription(sub)

    def call(self, srv_type, name, request, timeout=5.0):
        client = self.node.create_client(srv_type, name)
        try:
            self.spin_until(client.service_is_ready, timeout, f"service {name}")
            future = client.call_async(request)
            self.spin_until(future.done, timeout, f"a reply from {name}")
            return future.result()
        finally:
            self.node.destroy_client(client)

    def publish_until(self, msg_type, topic, msg, done, timeout=5.0, what=None):
        """Publish 'msg' every half second until done() -- a fresh publisher's
        first messages can go out before a subscriber has matched it."""
        pub = self.node.create_publisher(msg_type, topic, 10)
        last = [0.0]

        def step():
            if time.monotonic() - last[0] > 0.5:
                pub.publish(msg)
                last[0] = time.monotonic()
            return done()

        try:
            return self.spin_until(step, timeout, what or f"a reaction to {topic}")
        finally:
            self.node.destroy_publisher(pub)


@pytest.fixture(scope="session")
def ros_node():
    rclpy.init()
    node = rclpy.create_node(f"test_probe_{os.getpid()}")
    yield node
    node.destroy_node()
    rclpy.try_shutdown()


@pytest.fixture
def probe(request, ros_node):
    # The module's PROCESSES, if it has any, and the test's own.
    used = [request.getfixturevalue("processes")] if hasattr(request.module, "PROCESSES") else []
    if "launcher" in request.fixturenames:
        used.append(request.getfixturevalue("launcher"))

    def health():
        for procs in used:
            procs.check()

    probe = Probe(ros_node, health)
    probe.wait_for_nodes(getattr(request.module, "EXPECTED_NODES", ()))
    return probe
