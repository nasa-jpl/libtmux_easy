"""Shared fixtures. The tests drive a real tmux (the default server)."""
import pytest

from libtmux_easy import TmuxSession


@pytest.fixture
def make_session():
    """make_session(name="lte-test", window="main", **create_kwargs); each
    session made is killed after the test."""
    made = []

    def make(name="lte-test", window="main", **kwargs):
        sess = TmuxSession.create(name, kill_existing=True, **kwargs)
        if window:
            sess.new_window(window)
        made.append(sess)
        return sess

    yield make
    for sess in made:
        try:
            sess.kill()
        except Exception:  # already gone, e.g. replaced by a later create()
            pass


@pytest.fixture
def session(make_session):
    """A session with one window, 'main'."""
    return make_session()
