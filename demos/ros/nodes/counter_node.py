#!/usr/bin/env python3
"""A small node under test: publishes an increasing count, resets on request.

    counter_node.py [--topic TOPIC] [--period S] [--crash-after S] [--ignore-sigint]
                    [--ros-args ...]

Topic TOPIC (std_msgs/Int64), service TOPIC/reset (std_srvs/Trigger).
--crash-after S exits with code 3 after S seconds (0: never), to show the
tests catching a node that dies early. --ignore-sigint makes it a node that
won't shut down when asked, so the tests' teardown has to escalate.
"""
import argparse
import os
import signal

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.utilities import remove_ros_args
from std_msgs.msg import Int64
from std_srvs.srv import Trigger


class Counter(Node):
    def __init__(self, topic, period, crash_after):
        super().__init__("counter")
        self.count = 0
        self.pub = self.create_publisher(Int64, topic, 10)
        self.create_service(Trigger, f"{topic}/reset", self.reset)
        self.create_timer(period, self.tick)
        if crash_after > 0:
            self.create_timer(crash_after, self.crash)

    def tick(self):
        self.count += 1
        self.pub.publish(Int64(data=self.count))

    def reset(self, request, response):
        self.get_logger().info(f"reset at {self.count}")
        self.count = 0
        response.success = True
        return response

    def crash(self):
        self.get_logger().fatal("simulated crash")
        os._exit(3)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", default="counter")
    parser.add_argument("--period", type=float, default=0.2)
    parser.add_argument("--crash-after", type=float, default=0.0)
    parser.add_argument("--ignore-sigint", action="store_true")
    args = parser.parse_args(remove_ros_args()[1:])

    rclpy.init()
    if args.ignore_sigint:
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # after init: rclpy installs its own
    node = Counter(args.topic, args.period, args.crash_after)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass  # Ctrl-C / SIGINT is the normal way to stop: exit 0
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
