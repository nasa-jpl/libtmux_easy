from libtmux_easy import TmuxSession


session = TmuxSession.create("My Session")
session.new_window(f"window1")

p1 = session.new_pane(name="pane1", command=f"htop")
session.new_pane(name="pane2", command=f"journalctl -f")

w = session.new_window(f"window2", layout="main-horizontal")
session.new_pane(name="pane1", command=f"cat /etc/hosts")
session.new_pane(name="pane2", command=f"ls -la")
session.new_pane(name="pane3", command=f"ls -la", target="window2:pane1")
session.new_pane(name="pane4", command=f"ls -la")
# w.select_layout("main-horizontal")

session.send_keys("ls -la")
session.send_keys("ls -la", target_pane="window2:pane1")

session.select_first()
session.print()
session.select_window("window2")


p1.resize_pane(height=20)
session.attach()
