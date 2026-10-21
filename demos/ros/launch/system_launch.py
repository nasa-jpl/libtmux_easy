"""The system under test: a talker, a listener and our counter node.

    ros2 launch launch/system_launch.py [crash_after:=S]
"""
import os
import sys

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

COUNTER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "nodes", "counter_node.py")


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("crash_after", default_value="0"),
        Node(package="demo_nodes_cpp", executable="talker", output="screen"),
        Node(package="demo_nodes_py", executable="listener", output="screen"),
        ExecuteProcess(name="counter", output="screen",
                       cmd=[sys.executable, os.path.normpath(COUNTER),
                            "--crash-after", LaunchConfiguration("crash_after")]),
    ])
