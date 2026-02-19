from libtmux_wrapper.tmux_lib import *
from os import getenv


session = get_tmux_session("My Session")
session.new_window(f"window1")

session.new_pane(name="pane1", command=f"htop")
session.new_pane(name="pane2", command=f"journalctl -f")

session.new_window(f"window2")
session.new_pane(name="pane1", command=f"cat /etc/hosts")
session.new_pane(name="pane2", command=f"ls -la")

session.attach_session()
