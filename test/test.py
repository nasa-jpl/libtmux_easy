import pytest
from types import SimpleNamespace
from libtmux_easy.tmux_lib import TmuxSession

import libtmux_easy.tmux_lib as tmux_lib


# Helper fake classes to simulate libtmux objects
class FakePane:
    def __init__(self, id, name, window, pane_pid=None):
        self.id = id
        self.name = name
        self.window = window
        self.pane_pid = pane_pid
        self.sent = []

    def send_keys(self, command, enter=True):
        self.sent.append((command, enter))


class FakeWindow:
    def __init__(self, id, name):
        self.id = id
        self.name = name
        self.panes = []

    def rename_window(self, name):
        self.name = name

    def select_layout(self, layout="tiled"):
        pass


class FakeCollection:
    def __init__(self, items):
        self._items = list(items)

    def get(self, **kwargs):
        for item in self._items:
            ok = True
            for k, v in kwargs.items():
                if k == "window_name":
                    if getattr(item, "name", None) != v:
                        ok = False
                        break
                else:
                    if getattr(item, k, None) != v:
                        ok = False
                        break
            if ok:
                return item
        raise KeyError

    def __iter__(self):
        return iter(self._items)

    def __contains__(self, item):
        return item in self._items

    def __len__(self):
        return len(self._items)


@pytest.fixture
def session_instance(monkeypatch):
    # Build fake windows and panes
    w1 = FakeWindow(id="@0", name="window1")
    w2 = FakeWindow(id="@1", name="window2")

    p1 = FakePane(id="%0", name="pane0", window=w1)
    p2 = FakePane(id="%1", name="pane1", window=w1)
    p3 = FakePane(id="%2", name="pane0", window=w2)

    w1.panes = [p1, p2]
    w2.panes = [p3]

    panes_coll = FakeCollection([p1, p2, p3])
    windows_coll = FakeCollection([w1, w2])

    # Create TmuxSession instance without invoking base Session __init__
    inst = object.__new__(TmuxSession)
    # initialize dataclass post-init vars
    inst.__post_init__()  # sets unnamed flags, pane_names, commands

    # attach attributes expected by methods
    inst.windows = windows_coll
    inst.panes = panes_coll
    inst.current_window = w1
    inst.current_pane = p1

    return inst


def test_full_pane_name_and_full_from_pane(session_instance):
    inst = session_instance
    # Using window context
    full = inst._full_pane_name(inst.current_window, "myPane")
    assert full == "window1:myPane"
    # Using pane context
    full2 = inst._full_pane_name(inst.current_pane, "other")
    assert full2 == "window1:other"


def test_set_pane_title_updates_mapping_and_calls_internal(
    monkeypatch, session_instance
):
    inst = session_instance
    called = {}

    def fake_set_pane_title(pane, pane_name):
        called["args"] = (pane, pane_name)

    monkeypatch.setattr(tmux_lib, "_set_pane_title", fake_set_pane_title)

    inst.set_pane_title(inst.current_pane, "newname")
    # internal _set_pane_title should be called
    assert called["args"][1] == "newname"
    # mapping should contain fully qualified name
    assert inst.pane_names["window1:newname"] == inst.current_pane


def test_get_pane_name_and_get_pane_by_id(session_instance):
    inst = session_instance
    # populate pane_names mapping and test get_pane_name
    inst.pane_names["window1:alias"] = inst.current_pane
    name = inst.get_pane_name(inst.current_pane)
    assert name == "window1:alias"

    # get_pane by id using panes collection .get
    pane_by_id = inst.get_pane("%1")
    assert pane_by_id.id == "%1"
    # get_pane by fully qualified name
    pane_by_name = inst.get_pane("window1:alias")
    assert pane_by_name is inst.current_pane


def test_get_window_by_name_and_id(session_instance):
    inst = session_instance
    # by name
    w = inst.get_window("window1")
    assert w.name == "window1"
    # by id (starts with @)
    w_id = inst.get_window("@1")
    assert w_id.id == "@1"
    # unknown returns None
    assert inst.get_window("@doesnotexist") is None


def test_resolve_target_pane_and_window(session_instance):
    inst = session_instance
    # add mapping for target lookup
    inst.pane_names["window1:alias"] = inst.current_pane
    # resolve by string
    pane = inst.resolve_target_pane("window1:alias")
    assert pane is inst.current_pane
    # resolve by object when in session
    pane2 = inst.resolve_target_pane(inst.current_pane)
    assert pane2 is inst.current_pane
    # unknown string raises
    with pytest.raises(ValueError):
        inst.resolve_target_pane("no:suchpane")
    # unknown object not in session raises
    other_win = FakeWindow(id="@X", name="other")
    other_pane = FakePane(id="%X", name="p", window=other_win)
    with pytest.raises(ValueError):
        inst.resolve_target_pane(other_pane)

    # windows
    w = inst.resolve_target_window("window2")
    assert w.name == "window2"
    wobj = inst.resolve_target_window(inst.windows.get(id="@0"))
    assert wobj.id == "@0"
    with pytest.raises(ValueError):
        inst.resolve_target_window(FakeWindow(id="@Z", name="zzz"))


def test_send_keys_updates_commands_and_calls_pane_send(session_instance):
    inst = session_instance
    # ensure session considered initialized
    inst.unnamed_window = False
    inst.unnamed_pane = False
    # register pane name so commands map key will be available
    inst.pane_names["window1:alias"] = inst.current_pane

    inst.send_keys("echo hi", enter=False)
    # current_pane should have received send_keys call
    assert inst.current_pane.sent[-1] == ("echo hi", False)
    # commands dict should contain the command
    key = inst.get_pane_name(inst.current_pane)
    assert inst.commands[key][-1] == "echo hi"

    # sending to explicit target_pane by name
    inst.send_keys("ls -la", target_pane="window1:alias")
    assert inst.current_pane.sent[-1] == ("ls -la", True)
    assert inst.commands[key][-1] == "ls -la"
