import libtmux
from libtmux.server import Server
from libtmux.window import Window
from libtmux.session import Session
from libtmux.pane import Pane
from libtmux.common import tmux_cmd, exc
import shlex
import psutil
import time

from .proc import pane_child_processes, get_session_pids

from pprint import pprint as pp
import logging
from dataclasses import dataclass

from typing import Optional, Union
from packaging import version

libtmux_version = version.parse(libtmux.__version__)

logger = logging.getLogger(__name__)

try:
    from libtmux.exc import DeprecatedError
except ImportError:
    # DeprecatedError doesn't exist in older versions. Create a dummy exception that will never be
    # raised by older versions
    class DeprecatedError(Exception):
        pass


"""
DESCRIPTION: TODO
"""

"""
TODO
1. Add more options for how to split and arrange panes (e.g. horizontal vs vertical, custom layouts, etc.)
2. Add logic for removing windows/panes
3. Add options for sending commands (e.g. send to multiple, wait and retrieve output, check on error code, check on status, etc)
4. Add support for using either the fully qualified pane name or just the pane name?
"""

"""TODO
1. Test across different versions
    - 0.10.1
    - 0.21.0
2. Update the construction of TmuxSession
    - Doesn't work in older 0.10.1 version
3. Do we want to support reusing an old session? What would the use case for that be? This lib is
all about generating a session. Maybe for updating a session? But that would also probably involve
deleting window/panes and generating new ones. And maybe moving them around. Which I don't support
yet.
    - How about we mark it as future functionality
    - If we did reuse an old session, we would have to manually repopulate the state of TmuxSession
    - ACTUALLY - Suppose our script is called within a tmux session. In that case we would want to
    use the existing session instead of trying to nest. We could still create our windows/panes, but
    just within the existing session.
    
    This raises some questions:
    - How do we init our TmuxSession? Because if we are merging with an existing session that has
    not used this lib, some assumptions might not hold. Like we enforce windows having unique names.
    But libtmux doesn't. Only sessions have unique names. Windows and panes can have duplicate
    names/titles, but they have unique ids. 
    - Do we just hope for the best? I like having the ability to specify a window or pane by name
    instead of having to create a bunch of dummy variables to get the windows and panes to pass. 
    - We could keep track of the windows/panes WE'VE created. Then if we pass in a name for lookup,
    it is assumed that we are looking for a window/pane we've created. Unless explicitly specified
    that it is outside our current "session"
    - Or maybe we could somehow namespace our names? 
    
    
4. Code in ability to retrieve the pane title from libtmux

    window.list_panes() # To force a refresh
    # For libtmux >= 0.21.0
    pane_title = pane.item.get("pane_title")
    # For older versions
    pane_title = pane.get("pane_title")
    
"""

# Global default server object
_server = libtmux.Server(
    socket_name=None,
    socket_path=None,
    config_file=None,
    colors=None,
)


def set_server(**kwargs):
    """Set the server object that will be used by the rest of the this library. This allow users to
    specify custom tmux server options (e.g. socket name)"""
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


def session_exists(session_name: str) -> bool:
    """Returns True if tmux session already exists."""
    assert isinstance(session_name, str)
    exists = _server.has_session(session_name)
    return exists


def _set_pane_title(pane: Pane, pane_name: str):
    """Set the title of a tmux pane. This is done by sending a tmux command to select the pane and
    set its title."""
    tmux_command(f"select-pane -T {pane_name} -t {pane.id}")


def _create_session(
    session_name,
    window_name="unnamed",
    pane_name="unnamed",
    kill_existing=True,
    **kwargs,
):
    """Create a new tmux session with the given name. If a session with the same name already
    exists, it can be killed and replaced with a new session. Returns the created session, window,
    and pane objects."""

    global _server
    # TODO - just use kill_session option in new_session()

    if session_exists(session_name):
        if not kill_existing:
            raise ValueError(
                f"Session {session_name} already exists and kill_existing is False."
            )
        session = [
            session for session in _server.sessions if session.name == session_name
        ][0]
        logger.info(
            f"Session {session_name} exists. Killing and creating new session."
        )
        try:
            session.kill_session()
        except DeprecatedError:  # kill_session() deprecated in >0.30.0
            session.kill()
        session = _server.new_session(
            session_name=session_name,
        )
    else:
        logger.info(f"Creating new session named {session_name}")
        session = _server.new_session(session_name=session_name, **kwargs)

    window = session.windows[0]
    pane = window.panes[0]

    if window_name:
        window.rename_window(window_name)

    if pane_name:
        _set_pane_title(pane, pane_name)

    return session, window, window.panes[0]


# =================================================================================
# -------------------------------  TmuxSession Class  -----------------------------
# =================================================================================


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

    def __init__(self, **kwargs):
        raise NotImplementedError(
            "TmuxSession cannot be directly instantiated. Use TmuxSession.create() instead."
        )

    @classmethod
    def create(
        cls, session_name: str, kill_existing: bool = True, **kwargs
    ) -> "TmuxSession":
        """
        The primary entry point to create a managed TmuxSession.
        """
        session, window, pane = _create_session(
            session_name, kill_existing=kill_existing, **kwargs
        )
        session.__class__ = cls
        session.session_name = session_name
        session.__post_init__(window, pane)
        return session

    def __post_init__(self, window, pane):
        # Set internal state
        self.current_window = window
        self.current_pane = pane

        # Set default options. These allow for pane titles to be displayed
        self.set_option("renumber-windows", "on")
        try:
            self.set_option("pane-border-status", "top", _global=True)
        except:  # global arg name changed in newer versions of libtmux
            self.set_option("pane-border-status", "top", global_=True)

        self.unnamed_window = True
        self.unnamed_pane = {self.current_window.id: True}

        self.pane_names = {}  # Mapping of pane_name -> Pane object
        self.commands = {}
        self.command_before = {"all": None}

    def new_window(
        self, name: str, layout: Union[None, str] = "tiled", **window_kwargs
    ) -> Window:
        """Create a new window with the given name. Due to the nature of tmux, this will by also
        create a default, unnamed pane. The next call to new_pane will reuse and rename this pane
        instead of actually creating a new pane."""

        if self.unnamed_window:
            window = self.windows[0]
            window.rename_window(name)
            self.unnamed_window = False
            # Update current_window to the renamed window
            self.current_window = window
            self.current_pane = window.panes[0]
        else:
            if name in [w.name for w in self.windows]:
                raise ValueError(f"Window name {name} already exists.")

            window = super().new_window(name, **window_kwargs)

            if layout:
                window.layout_str = (
                    layout  # 'layout' is an attribute already used by libtmux
                )

            self.current_window = window
            self.current_pane = window.panes[0]
            self.unnamed_pane[self.current_window.id] = True

        return window


    def send_command_before(self, command: str, window: Union[str, Window] = None):
        """Helper function to send a command before sending the actual command to a new pane. This
        is useful for setting up the context for the command (e.g. changing directories, setting env
        vars, etc.)
        
        If 'window' is provided, the command will be sent to all the new panes for that window.
        Otherwise, the command will be sent to all new panes for the entire session.
        
        NOTE - a window command_before will override a session command_before.
        """
        if window:
            window = self.resolve_target_window(window)
            self.command_before[window.id] = command
        else:
            self.command_before["all"] = command
        
    def new_pane(
        self,
        name: str,
        command: str,
        enter: bool = True,
        **pane_kwargs,
    ) -> Pane:
        """Create a new pane with the given name and send the provided command. Due to the nature of
        tmux, the first call to new_pane after creating a new window will reuse the default unnamed
        pane created by tmux. The next call to new_pane will actually create a new pane as
        normal.

        If 'enter' is False, the command will be sent to the pane but not executed (i.e. no 'Enter'
        key will be sent).
        """

        if self.unnamed_window:
            raise ValueError("Cannot create a new pane before creating a new window.")

        if self.unnamed_pane[self.current_window.id]:
            pane = self.current_window.panes[0]
            self.set_pane_title(pane, name)
            self.unnamed_pane[self.current_window.id] = False
        else:
            full_name = self._full_pane_name(self.current_window, name)
            if full_name in self.pane_names:
                raise ValueError(
                    f"Pane name {name} already exists in window {self.current_window.name}."
                )
            pane = self.split_window(self.current_window, name, **pane_kwargs)

        if self.command_before.get(self.current_window.id, None):
            pane.send_keys(self.command_before[self.current_window.id], enter=True)
        elif self.command_before["all"]:
            pane.send_keys(self.command_before["all"], enter=True)
            
        pane.send_keys(command, enter)
        self.commands[self.get_pane_name(pane)] = [command]
        self.current_pane = pane

        return pane

    # TODO - specify layout when creating the window. If 'None', then don't select layout.
    def split_window(self, window: Window, pane_name=None, **pane_kwargs) -> Pane:
        """Add a new pane to the given window"""
        # Splitting with layouts that aren't 'tiled' can run into issues. Switch to tiled, split,
        # and then back to the target layout.
        if hasattr(window, "layout_str"):
            window.select_layout("tiled")

        if "target" in pane_kwargs:
            pane_kwargs["target"] = self.resolve_target_pane(pane_kwargs["target"]).id
        else:
            pane_kwargs["target"] = window.panes[-1].id

        try:
            pane = window.split_window(**pane_kwargs)
        except DeprecatedError:  # Window.split_window() was deprecated in 0.33.0
            pane = window.split(**pane_kwargs)
        self.set_pane_title(pane, pane_name)

        if hasattr(window, "layout_str"):
            window.select_layout(window.layout_str)
        return pane

    def send_keys(
        self, command: str, enter: bool = True, target_pane: Union[Pane, str] = None
    ):
        """Send the provided command string to the last created pane, or to the target_pane.

        'target_pane' should either be a pane name, pane id, or a Pane object. If None, the command
        will be sent to the most recently created pane. Raises ValueError if the resolved pane isn't
        a pane tracked/named by this session (i.e. not created via new_pane()).
        """
        if self.unnamed_window or self.unnamed_pane[self.current_window.id]:
            raise ValueError("Must create a window and pane before sending commands.")

        if target_pane:
            pane = self.resolve_target_pane(target_pane)
        else:
            pane = self.current_pane

        pane_name = self.get_pane_name(pane)
        if pane_name is None:
            raise ValueError(f"Pane {pane} is not a named pane tracked by this session.")

        logger.debug(f"Sending: {command}")
        pane.send_keys(command, enter)
        self.commands.setdefault(pane_name, []).append(command)

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

    def print(self):
        """Print layout of the session, including windows and panes."""
        rep = f"Session: {self.session_name}\n"
        tab = "    "
        for window in self.windows:
            rep += f"{tab}Window: {window.name}\n"
            for pane in window.panes:
                pane_name = self.get_pane_name(pane)
                rep += f"{tab*2}Pane: {pane_name.split(':')[1]}\n"
                for cmd in self.commands.get(pane_name, []):
                    rep += f"{tab*3}{cmd}\n"
        print(rep)

    def select_window(self, target_window: Union[Window, str]) -> Window:
        """Select the given window. 'target_window' can be either the name of the window, the id of
        the window, or the Window object itself. Returns the Window object. Note that selecting a
        window will also select the most recently created pane in that window."""
        window = self.resolve_target_window(target_window)
        self.current_window = window
        self.current_pane = window.panes[-1]
        window = super().select_window(window.id)
        window.select_pane(self.current_pane.id)
        return window
    
    def select_pane(self, target_pane: Union[Pane, str]) -> Pane:
        """Select the given pane. 'target_pane' can be either the name of the pane, the id of
        the pane, or the Pane object itself. Returns the Pane object"""
        pane = self.resolve_target_pane(target_pane)
        self.current_pane = pane
        self.current_window = pane.window
        self.select_window(pane.window)
        pane = pane.window.select_pane(pane.id)
        return pane

    def select_first(self):
        """Select the the first pane in the first window."""
        self.current_window = self.windows[0]
        self.current_pane = self.windows[0].panes[0]
        return (
            super()
            .select_window(self.windows[0].id)
            .select_pane(self.windows[0].panes[0].id)
        )

    def attach(self) -> Session:
        """Attach to the tmux session"""
        try:
            return self.attach_session()
        except DeprecatedError:  # attach_session() deprecated in 0.30.0
            return super().attach()

    def wait_for_pane_idle(
        self,
        target_pane: Union[Pane, str],
        timeout: float = 10.0,
        poll_interval: float = 0.2,
    ) -> bool:
        """Poll the given pane's running child processes until none remain, or until
        'timeout' seconds have elapsed. Returns True if the pane became idle, False if
        it timed out."""
        pane = self.resolve_target_pane(target_pane)
        end_time = time.time() + timeout
        while pane_child_processes(pane):
            if time.time() >= end_time:
                return False
            time.sleep(poll_interval)
        return True

    def stop_pane(
        self,
        target_pane: Union[Pane, str],
        sigint_timeout: float = 10.0,
        sigterm_timeout: float = 5.0,
        sigkill_timeout: float = 2.0,
        poll_interval: float = 0.2,
    ) -> bool:
        """Gracefully stop whatever is running in a pane. Follow a SIGINT ->
        SIGTERM -> SIGKILL escalation. Returns True if the pane ended up idle, False if
        processes survived even SIGKILL (should be rare, but is possible for e.g.
        zombie/defunct processes).

        A caller escalating past SIGINT should treat that as a signal that whatever was
        running did not shut down cleanly (e.g. a bag recorder likely failed to finalize
        its metadata).

        Caveat: "idle" is judged by walking pane_pid's current descendant tree, so a
        grandchild orphaned (reparented) before its parent dies could go undetected."""
        pane = self.resolve_target_pane(target_pane)
        pane_name = self.get_pane_name(pane) or pane.id

        if not pane_child_processes(pane):
            return True

        # Step 1: SIGINT (Ctrl-C). Delivered to the pane's foreground process group,
        # same as a user hitting Ctrl-C in a real terminal. Catchable -- gives the
        # process a chance to run its normal graceful-shutdown path.
        pane.send_keys("C-c", enter=False)
        if self.wait_for_pane_idle(pane, timeout=sigint_timeout, poll_interval=poll_interval):
            return True

        logger.warning(
            f"Pane {pane_name} still running after SIGINT ({sigint_timeout}s). Escalating to SIGTERM."
        )
        # Step 2: SIGTERM. Still catchable, but a firmer "terminate now" request sent
        # directly to each surviving descendant process (bypasses the pane entirely).
        for proc in pane_child_processes(pane):
            try:
                proc.terminate()
            except psutil.NoSuchProcess:
                pass
        if self.wait_for_pane_idle(pane, timeout=sigterm_timeout, poll_interval=poll_interval):
            return True

        logger.warning(
            f"Pane {pane_name} still running after SIGTERM ({sigterm_timeout}s). Escalating to SIGKILL."
        )
        # Step 3: SIGKILL. Un-catchable, un-ignorable -- the kernel kills the process
        # immediately with no cleanup. Last resort.
        for proc in pane_child_processes(pane):
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass
        stopped = self.wait_for_pane_idle(pane, timeout=sigkill_timeout, poll_interval=poll_interval)
        if not stopped:
            logger.warning(f"Pane {pane_name} still running after SIGKILL ({sigkill_timeout}s).")
        return stopped

    # =============== Getters ===============
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
            ids = {p.id: p for p in self.panes}
            return ids.get(identifier, None)
        else:
            return self.pane_names.get(identifier, None)

    def get_window(self, identifier: str) -> Optional[Window]:
        """Return the Window object associated with the given 'identifier' which can either be the
        window name or the window id. Return None if no window is found."""
        if identifier[0] == "@":
            ids = {w.id: w for w in self.windows}
            return ids.get(identifier, None)
        else:
            names = {w.name: w for w in self.windows}
            return names.get(identifier, None)

    # =============== Private ===============
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
        panes = []
        for window in self.windows:
            panes.extend(window.panes)
        if pane not in panes:
            raise ValueError(f"Pane {pane} not found in this session.")

    def _check_window_membership(self, window: Window):
        """Check if the given window is part of this session. Raises an exception if not."""
        if window not in self.windows:
            raise ValueError(f"Window {window} not found in this session.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(levelname)s:  %(message)s",
        handlers=[logging.StreamHandler()],
    )

    TmuxSession.create("my_sesh")
