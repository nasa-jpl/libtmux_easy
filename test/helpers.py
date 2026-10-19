"""Helpers shared by the test modules."""
import time


def wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def fmt(session, target, fmt_):
    """A tmux format, expanded for 'target'."""
    return session.server.cmd("display-message", "-p", "-t", target, fmt_).stdout[0]


def pane_text(pane):
    return "\n".join(pane.server.cmd("capture-pane", "-p", "-J", "-t", pane.id).stdout)


def title(pane):
    return pane.server.cmd("display-message", "-p", "-t", pane.id, "#{pane_title}").stdout[0]
