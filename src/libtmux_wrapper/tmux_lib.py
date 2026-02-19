import argparse
import os
import tmuxp

import libtmux
from libtmux.server import Server
from libtmux.window import Window
from libtmux.session import Session
from libtmux.pane import Pane
from libtmux._internal.query_list import ObjectDoesNotExist
from libtmux.common import tmux_cmd, exc
import shlex
import psutil

from pprint import pprint as pp
import logging
from dataclasses import dataclass

from typing import Optional, Dict

logger = logging.getLogger(__name__)

# Global default server object
server = libtmux.Server(
    socket_name=None,
    socket_path=None,
    config_file=None,
    colors=None,
)


def set_server(**kwargs):
    global server
    server = libtmux.Server(**kwargs)


def tmux_command(cmd: str) -> str:
    proc = tmux_cmd(*shlex.split(cmd))
    if proc.stderr:
        raise exc.LibTmuxException(
            "Command: %s returned error: %s" % (proc.cmd, proc.stderr)
        )
    return proc.stdout


def set_pane_title(pane: Pane, pane_name: str):
    tmux_command(f"select-pane -T {pane_name} -t {pane.id}")
    # pane.cmd("select-pane", "-T", pane_name)


def session_exists(server: Server, session_name: str) -> bool:
    """Return true if tmux session already exists."""
    # TODO - clean up
    assert isinstance(session_name, str)
    exists = server.has_session(session_name)
    if not exists:
        return exists

    try:
        server.sessions.get(session_name=session_name)
    except ObjectDoesNotExist:
        return False
    return True


def create_session(
    session_name, window_name="__placeholder__", pane_name=None, kill_existing=True
):
    global server

    if session_exists(server, session_name):
        session = server.sessions.get(session_name=session_name)
        if kill_existing:
            logger.info(
                f"Session {session_name} exists. Killing and creating new session."
            )
            # breakpoint()
            session.kill_session()
            session = server.new_session(
                session_name=session_name, start_directory=None
            )
    else:
        logger.info(f"Creating new session named {session_name}")
        session = server.new_session(session_name=session_name, start_directory=None)

    window = session.windows[0]
    pane = window.panes[0]

    if window_name:
        window.rename_window(window_name)

    if pane_name:
        set_pane_title(pane, pane_name)

    return session, window, window.panes[0]


def new_window(session: Session, window_name, pane_name=None):
    window = session.new_window(window_name)
    pane = window.panes[0]
    if pane_name:
        set_pane_title(pane, pane_name)
    return window, pane


def split_and_tile(window: Window, pane_name=None):
    """Add a new pane and reset layout to 'tiled'."""
    pane = window.panes[-1]
    pane = window.split_window(target=pane.id, vertical=True)
    window.select_layout(layout="tiled")
    set_pane_title(pane, pane_name)
    return pane


def get_child_pids(parent_pid: int, recursive=False):
    """
    Finds the PIDs of all child processes of a given parent PID.

    :param parent_pid: The PID of the parent process.
    :param recursive: If True, includes children of children, and so on.
    :return: A list of child PIDs (integers).
    """
    try:
        # Get the Process object for the parent PID
        parent = psutil.Process(parent_pid)

        # Get the children. Pass recursive=True to include the entire process tree.
        children = parent.children(recursive=recursive)

        # Extract and return the PIDs
        child_pids = [p.pid for p in children]
        return child_pids

    except psutil.NoSuchProcess:
        print(f"Error: Process with PID {parent_pid} does not exist.")
        return []
    except Exception as e:
        print(f"An error occurred: {e}")
        return []


def get_session_pids(session: Session):
    """Return a tree containing all of the currently running process pids in a tmux session.
    Each pane is running a 'bash' process, and commands entered into the pane will start a subprocess.
    It is that subprocess (and any children) that is reported here.

    The output is a dictionary:

        window_i: {
            pane_i: {
                child1: (
                    child1,
                    {
                        child2:
                    }
                    child3
                )
            }
            ...
            pane_j: ...
        }
        ...
        window_j: ...
    }

    window_<window_name> --> dict containing panes with subprocesses.
    pane_<pane_name> --> dict with
    """

    for window in session.windows:
        for pane in window.panes:
            parent = pane.pane_pid
            children = get_child_pids(int(parent))
            if children:
                print(
                    f"parent: {parent}, found children {children} in {window.name}:{pane.id}"
                )


def get_tmux_session(session_name, **kwargs):
    """Factory function to generate a TmuxSession object from an existing or newly created Session
    object"""

    global server
    session, window, pane = create_session(session_name, **kwargs)
    tmux_session = TmuxSession(server)

    # Update our new TmuxSession wrapper object with the data of the retrieved session
    for key, val in session.__dict__.items():
        if key in tmux_session.__dict__:
            tmux_session.__dict__[key] = val

    tmux_session.current_window = window
    tmux_session.current_pane = pane
    tmux_session.set_default_options()

    return tmux_session


@dataclass  # TODO - necessary?
class TmuxSession(Session):
    """Helper class that streamlines the creation of a tmux session. It is "stateful" in that it
    will act on the most recent context. Sort of similar to matplotlib. (e.g. new_pane will create a
    new pane in the most recently created window)"""

    # TODO - we ultimately also want to maintain a data structure that tracks the window/panes and
    # their names and the command issued to each.

    current_window: Window = None
    current_pane: Pane = None

    def __post_init__(self):
        self.commands = {}
        self.unused_pane = True

    def set_default_options(self):
        # For setting window/pane titles
        self.set_option("renumber-windows", "on")
        self.set_option("pane-border-status", "top", _global=True)

    def new_window(self, name: str, **kwargs) -> Window:
        """Create a new window with the given name. Due to the nature of tmux, this will by also
        create a default, unnamed pane. The next call to new_pane will reuse and rename this pane
        instead of actually creating a new pane."""

        # TODO - have some logic similar to unused_pane so that we can reuse the initial default window.
        # Or have the get_tmux_session explicitly require an initial window (and pane?) name?
        #

        window = super().new_window(name, **kwargs)

        self.current_window = window
        self.current_pane = window.panes[0]
        self.unused_pane = True

        return window

    def new_pane(
        self, name: str, command: str, enter: bool = True, target_window: str = None
    ) -> Pane:
        """Create a new pane with the given name and send the provided command."""

        # TODO - logic for target_window
        # TODO - add more options for how to split and arrange.
        new_pane = split_and_tile(self.current_window, name)
        new_pane.send_keys(command, enter)

        self.commands[name] = [command]
        self.current_pane = new_pane

        return new_pane

    def send_keys(self, command: str, enter: bool = True, target_pane: str = None):
        """Send the provided command string to the last created pane, or to the target_pane"""

        # TODO - logic for target_pane
        print(f"Sending: {command}")

        breakpoint()

        self.current_pane.send_keys(command, enter)
        name = (
            self.current_pane.id
        )  # Can we recover the name? Or do we need to keep track?

        self.commands[name].append(command)

    def close(self):
        pass

    def __repr__(self) -> str:
        """Representation of :class:`Session` object."""
        # TODO - update to show the layout of the current session (windows and panes)
        rep = f"{self.__class__.__name__}({self.session_id} {self.session_name})\n"

        for window in self.windows:
            rep += f"{window.name}\n"
            for pane in window.panes:
                rep += f"\t{pane.name}\n"


from contextlib import contextmanager


@contextmanager
def create_tmux_session(session_name, create_tui=True):
    session = TmuxSession(session_name)
    try:
        yield session
    finally:
        session.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:  %(message)s",
        handlers=[logging.StreamHandler()],
    )

    get_tmux_session("my_sesh")
