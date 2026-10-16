"""The viewport: swapping panes in and out of the dashboard window."""

from .._tmux import option, tmux as _tmux
from .options import (AUTO, FROM_OPTION, HOME_CMD_OPTION, HOME_OPTION, SIDEBAR_OPTION,
                      VIEW_OPTION, WIDTH_OPTION)


class Viewport:
    """The dashboard window's viewport slot.

    State, in window options: HOME is the placeholder pane, VIEW is whichever
    pane is in the viewport. When VIEW != HOME, HOME sits in VIEW's original
    slot (in window FROM). show() always swaps the occupant home first, so at
    most one pane is ever away from its window.

    'run' executes a tmux command and returns its stdout lines. The default
    uses the tmux binary (right for a process inside the session); the
    library passes its own server's cmd()."""

    def __init__(self, window, run=_tmux, sidebar=None):
        self.window = window
        self.run = run
        self.sidebar = sidebar or option(window, SIDEBAR_OPTION, run=run) or None
        self._layout = None  # (pane count, window_layout), last seen intact

    def option(self, name):
        """A window option of the dashboard window, or ''."""
        return option(self.window, name, "-wq", run=self.run)

    def _set(self, name, value):
        self.run("set-option", "-wq", "-t", self.window, name, value)

    @property
    def home(self):
        return self.option(HOME_OPTION)

    @property
    def view(self):
        return self.option(VIEW_OPTION)

    def panes(self):
        """pane id -> window id, for every pane in the session."""
        out = self.run("list-panes", "-s", "-t", self.window, "-F", "#{pane_id} #{window_id}")
        return dict(line.split() for line in out if len(line.split()) == 2)

    def active(self):
        """False when another window is the session's current one. True if
        unknown, so a failed query never moves panes."""
        out = self.run("display-message", "-p", "-t", self.window, "#{window_active}")
        return not out or out[0].strip() != "0"

    def show(self, pane_id):
        """Put 'pane_id' in the viewport. The home pane id shows home."""
        self.repair()
        home, view = self.home, self.view
        if pane_id == view:
            return
        if view != home:
            self._put_back(home, view)
            home = self.home  # _put_back may have replaced it
        if pane_id != home:
            from_window = self.panes().get(pane_id, "")
            self.run("swap-pane", "-d", "-s", pane_id, "-t", home)
            self._set(FROM_OPTION, from_window)
        self._set(VIEW_OPTION, pane_id)

    def go_to(self, pane_id):
        """Put 'pane_id' back in its own window and switch to it there."""
        self.restore()
        self.run("select-window", "-t", pane_id)
        self.run("select-pane", "-t", pane_id)

    def restore(self):
        """Send the viewed pane back to its window, leaving home in view."""
        self.repair()
        home, view = self.home, self.view
        if view and view != home:
            self._put_back(home, view)
            self._set(VIEW_OPTION, home)

    def _put_back(self, home, view):
        panes = self.panes()
        if home in panes:
            self.run("swap-pane", "-d", "-s", home, "-t", view)
            return
        # Home is gone (killed while parked), so 'view' has no slot to swap
        # back into. Rejoin its window as a new split, then make a new home.
        from_window = self.option(FROM_OPTION)
        if from_window:
            self.run("join-pane", "-d", "-s", view, "-t", from_window)
        self._new_home()

    def repair(self):
        """Recover from panes vanishing under the dashboard: the viewed pane
        exiting (its viewport slot goes with it) or home being killed."""
        panes = self.panes()
        home, view = self.home, self.view
        if view in panes and panes[view] == self.window:
            # Intact (a missing home is handled by _put_back). Remember the
            # layout, mouse resizes and extra panes included, for _fit().
            count = sum(1 for w in panes.values() if w == self.window)
            layout = self.run("display-message", "-p", "-t", self.window, "#{window_layout}")
            if layout:
                self._layout = (count, layout[0].strip())
            return
        if panes.get(home) == self.window:
            pass  # home already back in the viewport
        elif home in panes:
            # The viewed pane is gone and home is stranded in its old slot.
            self.run("join-pane", "-d", "-h", "-s", home, "-t", self.sidebar or self.window)
            self._fit()
        elif home not in panes:
            self._new_home()
            return
        self._set(VIEW_OPTION, home)

    def _new_home(self):
        cmd = self.option(HOME_CMD_OPTION) or "cat"
        out = self.run("split-window", "-d", "-h", "-t", self.sidebar or self.window,
                       "-P", "-F", "#{pane_id}", cmd)
        if not out:
            return
        home = out[0].strip()
        self.run("select-pane", "-t", home, "-T", "home")
        self._set(HOME_OPTION, home)
        self._set(VIEW_OPTION, home)
        self._fit()

    def _fit(self):
        """join-pane/split-window put home back as a split of the sidebar.
        Restore the last intact layout; failing that, the sidebar's width."""
        count = sum(1 for w in self.panes().values() if w == self.window)
        if self._layout and self._layout[0] == count:
            self.run("select-layout", "-t", self.window, self._layout[1])
            return
        width = self.option(WIDTH_OPTION)
        if width and width != AUTO and self.sidebar:
            self.run("resize-pane", "-t", self.sidebar, "-x", width)
