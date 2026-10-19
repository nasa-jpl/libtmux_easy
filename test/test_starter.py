"""Tests for the starter tmux settings (src/libtmux_easy/starter.py). Each test
gets its own tmux server, with no config file unless it writes one."""
import subprocess

import pytest

from libtmux_easy import starter


@pytest.fixture
def server(tmp_path, monkeypatch):
    sock = str(tmp_path / "tmux.sock")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("LTE_TMUX_DEFAULTS", raising=False)

    def start(*args):
        subprocess.run(["tmux", "-S", sock, *args, "new-session", "-d", "-s", "t"], check=True)

    def run(*args):
        return subprocess.run(["tmux", "-S", sock, *args], capture_output=True,
                              text=True).stdout.splitlines()

    yield start, run
    subprocess.run(["tmux", "-S", sock, "kill-server"], capture_output=True)


def _applied(run):
    return run("show-options", "-gv", "mouse") == ["on"] and \
        "split-window -h" in "\n".join(run("list-keys", "-T", "prefix", "|"))


def test_no_config_gets_the_starter_once(server):
    start, run = server
    start()
    assert starter.apply(run) is True
    assert _applied(run)
    assert "kill-session" in "\n".join(run("list-keys", "-T", "prefix", "C-x"))
    assert starter.apply(run) is False  # once per server


def test_a_config_file_is_left_alone(server, tmp_path):
    start, run = server
    conf = tmp_path / ".tmux.conf"
    conf.write_text("set -g status-left mine\n")
    start()
    assert starter.has_config(run)
    assert starter.apply(run) is False
    assert not _applied(run)


def test_env_turns_it_off(server, monkeypatch):
    start, run = server
    start("-f", "/dev/null")
    monkeypatch.setenv("LTE_TMUX_DEFAULTS", "0")
    assert starter.apply(run) is False
    assert not _applied(run)


def test_lte_defaults_prints_the_conf(capsys):
    assert starter.main([]) == 0
    assert capsys.readouterr().out == starter.CONF
