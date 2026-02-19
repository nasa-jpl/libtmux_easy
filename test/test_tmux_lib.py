from libtmux_wrapper import TmuxSession


session = TmuxSession.create("My Session")
session.new_window(f"window1")

session.new_pane(name="pane1", command=f"htop")
session.new_pane(name="pane2", command=f"journalctl -f")

session.new_window(f"window2")
session.new_pane(name="pane1", command=f"cat /etc/hosts")
session.new_pane(name="pane2", command=f"ls -la")

session.send_keys("ls -la")
session.send_keys("ls -la", target_pane="window2:pane1")

# session.select_window("window1")
session.select_first()

print(session)
session.attach_session()
