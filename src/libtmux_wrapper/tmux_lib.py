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

from typing import Optional, Dict, Union

logger = logging.getLogger(__name__)

"""
TODO
1. Add more options for how to split and arrange panes (e.g. horizontal vs vertical, custom layouts, etc.)
2. Add logic for removing windows/panes
3. Add options for sending commands (e.g. send to multiple, wait and retrieve output, check on error code, check on status, etc)
4. Add support for using either the fully qualified pane name or just the pane name?
"""

# Global default server object
_server = libtmux.Server(
    socket_name=None,
    socket_path=None,
    config_file=None,
    colors=None,
)


def set_server(**kwargs):
    """Set the server object that will be used by the rest of the this library. This is necessary to
    allow users to specify custom tmux server options (e.g. socket name)"""
    global _server
    _server = libtmux.Server(**kwargs)


def tmux_command(cmd: str) -> str:
    """Helper function to run a tmux command and return the output. Raises an exception if the
    command returns an error."""
    proc = tmux_cmd(*shlex.split(cmd))
    if proc.stderr:
        raise exc.LibTmuxException(
            "Command: %s returned error: %s" % (proc.cmd, proc.stderr)
        )
    return proc.stdout


def session_exists(server: Server, session_name: str) -> bool:
    """Returns True if tmux session already exists."""
    # TODO - clean up
    assert isinstance(session_name, str)
    exists = _server.has_session(session_name)
    if not exists:
        return exists

    try:
        _server.sessions.get(session_name=session_name)
    except ObjectDoesNotExist:
        return False
    return True


def _set_pane_title(pane: Pane, pane_name: str):
    """Set the title of a tmux pane. This is done by sending a tmux command to select the pane and
    set its title."""
    tmux_command(f"select-pane -T {pane_name} -t {pane.id}")
    # pane.cmd("select-pane", "-T", pane_name)


def _create_session(
    session_name, window_name="unnamed", pane_name="unnamed", kill_existing=True
):
    """Create a new tmux session with the given name. If a session with the same name already
    exists, it will can be killed and replaced with a new session. Returns the created session, window,
    and pane objects."""

    global _server

    if session_exists(_server, session_name):
        session = _server.sessions.get(session_name=session_name)
        if kill_existing:
            logger.info(
                f"Session {session_name} exists. Killing and creating new session."
            )
            # breakpoint()
            session.kill_session()
            session = _server.new_session(
                session_name=session_name, start_directory=None
            )
    else:
        logger.info(f"Creating new session named {session_name}")
        session = _server.new_session(session_name=session_name, start_directory=None)

    window = session.windows[0]
    pane = window.panes[0]

    if window_name:
        window.rename_window(window_name)

    if pane_name:
        _set_pane_title(pane, pane_name)

    return session, window, window.panes[0]


@dataclass
class TmuxSession(Session):
    """Helper class that streamlines the creation of a tmux session. It is "stateful" in that it
    will act on the most recent context. Sort of similar to matplotlib. (e.g. new_pane will create a
    new pane in the most recently created window)

    New Windows and Panes
    ---------------------
    For a new session, tmux will by default create a new window and pane (which we will title
    'unnamed'), and the first call to new_window() or new_pane() will reuse that initial
    window/pane.  Then subsequent calls will create new windows/panes as normal.

    Similarly, when new_window() creates a new window, tmux will create an new pane for that
    window. The next call to new_pane() will reuse that initial pane, and then subsequent calls will
    create new panes as normal.

    Naming
    ---------------------
    When created, windows and names will automatically be given unique ids. The first window will be
    @0, the second will be @1, etc. Panes are named %0, %1, etc.

    In this class, windows and panes are also always assigned a user-specified name.
        - Window names must be unique.
        - Pane names must be unique within a window.

    Becuase pane names can be repeated across windows, the identifying name of a pane is actually
    'window_name:pane_name'. This is the name that should be supplied to functions that accept a pane
    name.

    In general, functions that accept a target_pane will accept a Pane object, the pane id, or the
    fully qualified pane name (i.e. 'window_name:pane_name').
    """

    current_window: Window = None
    current_pane: Pane = None

    @classmethod
    def create(cls, session_name: str, kill_existing: bool = True, **kwargs):
        """
        The primary entry point to create a managed TmuxSession.
        """
        session, window, pane = _create_session(
            session_name, kill_existing=kill_existing, **kwargs
        )

        instance = cls(server=session.server, session_id=session.session_id)

        # Set internal state
        instance.current_window = window
        instance.current_pane = pane
        instance.set_default_options()

        return instance

    def __post_init__(self):
        self.unnamed_window = True
        self.unnamed_pane = True

        self.pane_names = {}  # Mapping of pane_name -> Pane object
        self.commands = {}

    def set_default_options(self):
        """Options for setting window/pane titles"""
        self.set_option("renumber-windows", "on")
        self.set_option("pane-border-status", "top", _global=True)

    def new_window(self, name: str, **kwargs) -> Window:
        """Create a new window with the given name. Due to the nature of tmux, this will by also
        create a default, unnamed pane. The next call to new_pane will reuse and rename this pane
        instead of actually creating a new pane."""

        if self.unnamed_window:
            window = self.windows[0]
            window.rename_window(name)
            self.unnamed_window = False
        else:
            if name in [w.name for w in self.windows]:
                raise ValueError(f"Window name {name} already exists.")

            window = super().new_window(name, **kwargs)
            self.current_window = window
            self.current_pane = window.panes[0]
            self.unnamed_pane = True

        return window

    def new_pane(self, name: str, command: str, enter: bool = True) -> Pane:
        """Create a new pane with the given name and send the provided command. Due to the nature of
        tmux, the first call to new_pane after creating a new window will reuse the default unnamed
        pane created by tmux. The next call to new_pane will actually create a new pane as
        normal.

        If 'enter' is False, the command will be sent to the pane but not executed (i.e. no 'Enter'
        key will be sent)."""
        # TODO - add more options for how to split and arrange.

        if self.unnamed_window:
            raise ValueError("Cannot create a new pane before creating a new window.")

        if self.unnamed_pane:
            pane = self.current_window.panes[0]
            self.set_pane_title(pane, name)
            self.unnamed_pane = False
        else:
            full_name = self._full_pane_name(self.current_window, name)
            if full_name in self.pane_names:
                raise ValueError(
                    f"Pane name {name} already exists in window {self.current_window.name}."
                )
            pane = self.split_and_tile(self.current_window, name)

        pane.send_keys(command, enter)
        self.commands[self.get_pane_name(pane)] = [command]
        self.current_pane = pane

        return pane

    def send_keys(
        self, command: str, enter: bool = True, target_pane: Union[Pane, str] = None
    ):
        """Send the provided command string to the last created pane, or to the target_pane.

        'target_pane' should either be a pane name, pane id, or a Pane object. If None, the command
        will be sent to the most recently created pane.
        """
        if self.unnamed_window or self.unnamed_pane:
            raise ValueError("Must create a window and pane before sending commands.")

        logger.debug(f"Sending: {command}")
        if target_pane:
            pane = self.resolve_target_pane(target_pane)
        else:
            pane = self.current_pane

        pane.send_keys(command, enter)
        pane_name = self.get_pane_name(pane)
        self.commands.get(pane_name, []).append(command)

    def resolve_target_pane(self, identifier: Union[Pane, str]) -> Pane:
        """Helper function to retrieve a Pane object from either a pane name, pane id, or a Pane
        object."""
        if isinstance(identifier, str):
            pane = self.get_pane(identifier)
            if pane is None:
                raise ValueError(f"Pane with identifier {identifier} not found.")
        else:
            self._check_pane_membership(identifier)
            pane = identifier
        return pane

    def resolve_target_window(self, identifier: Union[Window, str]) -> Window:
        """Helper function to retrieve a Window object from either a window name, window id, or a
        window object."""
        if isinstance(identifier, str):
            window = self.get_window(identifier)
            if window is None:
                raise ValueError(f"Window with identifier {identifier} not found.")
        else:
            self._check_window_membership(identifier)
            window = identifier
        return window

    def set_pane_title(self, pane: Pane, pane_name: str):
        """Set the title of a tmux pane. This is done by sending a tmux command to select the pane
        and set its title."""
        _set_pane_title(pane, pane_name)
        window_name = pane.window.name
        full_name = self._full_pane_name(pane, pane_name)
        self.pane_names[full_name] = pane

    def split_and_tile(self, window: Window, pane_name=None):
        """Add a new pane and reset layout to 'tiled'."""
        pane = window.panes[-1]
        pane = window.split_window(target=pane.id, vertical=True)
        window.select_layout(layout="tiled")
        self.set_pane_title(pane, pane_name)
        return pane

    def __repr__(self) -> str:
        """Print layout of the session, including windows and panes."""
        rep = f"Session: {self.session_name}\n"
        tab = "    "
        for window in self.windows:
            rep += f"{tab}Window: {window.name}\n"
            for pane in self.panes:
                if pane in window.panes:
                    pane_name = self.get_pane_name(pane)
                    rep += f"{tab*2}Pane: {pane_name.split(':')[1]}\n"
                    for cmd in self.commands[pane_name]:
                        rep += f"{tab*3}{cmd}\n"
        return rep

    def select_window(self, target_window: Union[Window, str]):
        """Select the given window. 'target_window' can be either the name of the window, the id of
        the window, or the Window object itself. Returns the Window object"""
        window = self.resolve_target_window(target_window)
        return super().select_window(window.id)

    def select_pane(self, target_pane: Union[Pane, str]):
        """Select the given window. 'target_window' can be either the name of the window, the id of
        the window, or the Window object itself. Returns the Window object"""
        pane = self.resolve_target_pane(target_pane)
        return super().select_pane(pane.id)

    def select_first(self):
        """Select the the first pane in the first window."""
        return (
            super()
            .select_window(self.windows[0].id)
            .select_pane(self.windows[0].panes[0].id)
        )

    # --------- Getters ---------
    def get_pane_name(self, pane: Pane) -> Optional[str]:
        """Return the fully qualified name of the given pane (i.e. 'window_name:pane_name'), or None
        if the pane is unnamed."""
        self._check_pane_membership(pane)
        for name, p in self.pane_names.items():
            if p == pane:
                return name
        return None

    def get_window_name(self, window: Window) -> Optional[str]:
        """Return the name of the given window, or None if the window is unnamed."""
        self._check_window_membership(window)
        return window.name

    def get_pane(self, identifier: str) -> Optional[Pane]:
        """Return the Pane object associated with the given 'identifier' which can either be the
        fully qualified pane name or the pane id. Return None if no pane is found."""
        if identifier[0] == "%":
            pane_id = identifier
            try:
                pane = self.panes.get(id=pane_id)
            except:
                pane = None
        else:
            pane = self.pane_names.get(identifier, None)
        return pane

    def get_window(self, identifier: str) -> Optional[Window]:
        """Return the Window object associated with the given 'identifier' which can either be the
        window name or the window id. Return None if no window is found."""
        if identifier[0] == "@":
            try:
                window = self.windows.get(id=identifier)
                return window
            except:
                return None
        else:
            try:
                window = self.windows.get(window_name=identifier)
                return window
            except:
                return None
            return None

    # --------- Private ---------
    def _full_pane_name(self, context: Union[Window, Pane], name):
        """Helper function to generate a fully qualified name for a pane (i.e.
        'window_name:pane_name')"""
        if isinstance(context, Window):
            full_name = f"{context.name}:{name}"
        elif isinstance(context, Pane):
            full_name = f"{context.window.name}:{name}"
        else:
            raise ValueError(
                f"Context must be a Window or Pane object, not {type(context)}."
            )

        return full_name

    def _check_pane_membership(self, pane: Pane):
        """Check if the given pane is part of this session. Raises an exception if not."""
        if pane not in self.panes:
            raise ValueError(f"Pane {pane} not found in this session.")

    def _check_window_membership(self, window: Window):
        """Check if the given window is part of this session. Raises an exception if not."""
        if window not in self.windows:
            raise ValueError(f"Window {window} not found in this session.")


# from contextlib import contextmanager
# @contextmanager
# def create_tmux_session(session_name, create_tui=True):
#     session = TmuxSession(session_name)
#     try:
#         yield session
#     finally:
#         session.close()


# def new_window(session: Session, window_name, pane_name=None):
#     window = session.new_window(window_name)
#     pane = window.panes[0]
#     if pane_name:
#         set_pane_title(pane, pane_name)
#     return window, pane


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


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:  %(message)s",
        handlers=[logging.StreamHandler()],
    )

    get_tmux_session("my_sesh")
