"""
Tests for lte-key (src/libtmux_easy/keys.py): borrowing a prefix key so that
it works where a session option is set and does what it did before anywhere
else, then giving it back. Each test gets its own tmux server ($TMUX points
the tmux client at it), so no binding of the user's is touched.
"""
import subprocess

import pytest

from libtmux_easy import keys


@pytest.fixture(autouse=True)
def server(tmp_path, monkeypatch):
    sock = str(tmp_path / "tmux.sock")
    subprocess.run(["tmux", "-S", sock, "-f", "/dev/null", "new-session", "-d", "-s", "t"],
                   check=True)
    monkeypatch.setenv("TMUX", f"{sock},0,0")
    yield sock
    subprocess.run(["tmux", "-S", sock, "kill-server"], capture_output=True)


def tmux(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True).stdout.strip()


def test_borrowed_key_falls_back_to_the_original_and_is_restored():
    tmux("bind-key", "o", "rename-window", "before")
    original = keys.current("o")

    keys.borrow("o", "@mine", "rename-window ours")
    line = keys.current("o")
    assert '"#{@mine}"' in line and "rename-window ours" in line
    assert "rename-window before" in line          # the fallback branch

    keys.give_back("o")
    assert keys.current("o") == original
    assert tmux("show-options", "-g").count("@lte_key_") == 0


def test_an_unbound_key_is_unbound_again():
    assert keys.current("y") == ""
    keys.borrow("y", "@mine", "display-message ours", "not here")
    assert "not here" in keys.current("y")
    keys.give_back("y")
    assert keys.current("y") == ""


def test_borrowers_share_the_key_and_the_last_one_restores_it():
    tmux("bind-key", "o", "rename-window", "before")
    original = keys.current("o")
    keys.borrow("o", "@mine", "rename-window ours")
    keys.borrow("o", "@mine", "rename-window ours")   # not wrapped twice
    assert keys.current("o").count("#{@mine}") == 1

    keys.give_back("o")
    assert "#{@mine}" in keys.current("o")           # still borrowed
    keys.give_back("o")
    assert keys.current("o") == original


def test_a_bad_command_raises_and_leaves_the_key_alone():
    tmux("bind-key", "o", "rename-window", "before")
    original = keys.current("o")
    with pytest.raises(ValueError):
        keys.borrow("o", "@mine", "display-message {")
    assert keys.current("o") == original
    assert tmux("show-options", "-g").count("@lte_key_") == 0


def test_cli_message_option_is_not_taken_for_the_command():
    assert keys.main(["borrow", "y", "@mine", "--message", "not here",
                      "--", "display-message", "ours"]) == 0
    line = keys.current("y")
    assert "display-message ours" in line and "not here" in line
    assert keys.main(["return", "y"]) == 0
    assert keys.current("y") == ""
