"""lte-key: borrow a tmux prefix key for as long as something needs it.

    lte-key borrow KEY OPTION [--message TEXT] -- COMMAND
    lte-key return KEY

A key binding is server-global, but the thing it jumps to (a dashboard, a log
window) exists in one session. So the borrowed key is conditional: where the
session option OPTION is set, it runs COMMAND; anywhere else it does exactly
what it did before -- the original binding, captured from 'list-keys', becomes
the fallback branch. Nothing has to be added to ~/.tmux.conf.

Borrowers are counted in global options, so two dashboards can share a key:
the first saves the original binding, the last puts it back. A key already
borrowed is not wrapped a second time. If a borrower dies without returning
the key, the binding stays until the server exits -- harmless, since outside
OPTION's sessions it still runs the original.

The library's own keys (the dashboard's g, lte-close's Q) are borrowed only
when unbound, so a binding of the user's always wins, and ~/.tmux.conf can
pick others: 'set -g @lte_key_dash G', 'set -g @lte_key_close ""' (none).

Stdlib only, like wrap.py and dash/.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile

from .._tmux import option as _option, tmux as _tmux

TABLE = "prefix"

_LISTED = re.compile(r"^bind-key\s+(?P<flags>(?:-\S+\s+)*?)-T\s+(?P<table>\S+)\s+"
                     r"(?P<key>\S+)\s+(?P<command>.*)$")


def _slug(key):
    return "".join(c if c.isalnum() else f"_{ord(c):x}" for c in key)


def _options(key):
    slug = _slug(key)
    return f"@lte_key_{slug}_users", f"@lte_key_{slug}_saved"


def _source(run, text):
    """Run tmux config text: the only way to pass '{ ... }' blocks intact.
    Checked with 'source-file -n' first: tmux reports nothing on stdout, so a
    parse error would otherwise pass silently and leave the key unbound."""
    with tempfile.NamedTemporaryFile("w", suffix=".tmux", delete=False) as handle:
        handle.write(text + "\n")
    try:
        check = subprocess.run(["tmux", "source-file", "-n", handle.name],
                               capture_output=True, text=True)
        if check.returncode != 0:
            raise ValueError(f"bad tmux command: {check.stderr.strip() or text}")
        run("source-file", handle.name)
    finally:
        os.unlink(handle.name)


def current(key, run=_tmux):
    """The 'bind-key ...' line for 'key' in the prefix table, or ''."""
    for line in run("list-keys", "-T", TABLE, key):
        match = _LISTED.match(line)
        if match and match["table"] == TABLE:
            return line
    return ""


def _escape(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


def chosen(role, default, run=_tmux):
    """The key for one of the library's roles ("dash", "close"): the global
    option @lte_key_<role> if set ("" for none), else 'default'."""
    name = f"@lte_key_{role}"
    if not run("show-options", "-gq", name):
        return default
    return _option(None, name, "-gq", run=run)


def borrow(key, option, command, message=None, run=_tmux, if_free=False):
    """Bind 'key' to run 'command' (tmux command text) in sessions where
    'option' is set, and its previous binding everywhere else. 'if_free':
    only if 'key' is unbound (or already borrowed). True if borrowed."""
    users_option, saved_option = _options(key)
    users = int(_option(None, users_option, "-gq", run=run) or 0)
    if users == 0:
        saved = current(key, run)
        if saved and if_free:
            return False
        match = _LISTED.match(saved) if saved else None
        if match:
            fallback = match["command"]
        else:
            fallback = f'display-message "{_escape(message or key + " is not bound here")}"'
        _source(run, f"bind-key -T {TABLE} {key} if-shell -F '#{{{option}}}' "
                     f"{{ {command} }} {{ {fallback} }}")
        run("set-option", "-gq", saved_option, saved)
    run("set-option", "-gq", users_option, str(users + 1))
    return True


def give_back(key, run=_tmux):
    """Return a key borrowed with borrow(). The last borrower restores it."""
    users_option, saved_option = _options(key)
    users = int(_option(None, users_option, "-gq", run=run) or 0)
    if users > 1:
        run("set-option", "-gq", users_option, str(users - 1))
        return
    saved = _option(None, saved_option, "-gq", run=run)
    if saved:
        _source(run, saved)
    else:
        run("unbind-key", "-T", TABLE, key)
    run("set-option", "-gqu", users_option)
    run("set-option", "-gqu", saved_option)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lte-key", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    b = sub.add_parser("borrow", help="bind KEY to COMMAND where OPTION is set")
    b.add_argument("key")
    b.add_argument("option", help="session option, e.g. @lte_dash_window")
    b.add_argument("--message", help="shown elsewhere if KEY was unbound before")
    r = sub.add_parser("return", help="give KEY back")
    r.add_argument("key")
    argv = sys.argv[1:] if argv is None else list(argv)
    # COMMAND is everything after '--', split off by hand: argparse's
    # REMAINDER would also swallow an option given before it.
    command = argv[argv.index("--") + 1:] if "--" in argv else []
    args = parser.parse_args(argv[:argv.index("--")] if "--" in argv else argv)
    if args.action == "borrow":
        if not command:
            parser.error("borrow needs '-- COMMAND'")
        try:
            borrow(args.key, args.option, " ".join(command), args.message)
        except ValueError as exc:
            sys.stderr.write(f"lte-key: {exc}\n")
            return 1
    else:
        give_back(args.key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
