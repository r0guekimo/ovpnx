"""Create and remove the <platform>-start / <platform>-stop launcher scripts.

Each launcher is a tiny POSIX shell script that runs ovpnx with the right
program name, so 'htb-start eu' behaves like 'ovpnx connect htb eu'. Every
script carries a marker line; ovpnx only ever overwrites or deletes files that
contain it, so it can never clobber a program it did not create.
"""
import os
import shlex
import stat
import sys
from pathlib import Path

MARKER = "# ovpnx-shortcut (created by 'ovpnx install-shortcuts'; safe to delete)"
ACTIONS = ("start", "stop")


def default_dir(environ=None):
    env = os.environ if environ is None else environ
    home = env.get("HOME") or str(Path.home())
    return Path(home) / ".local" / "bin"


def names_for(platforms):
    return ["%s-%s" % (p, a) for p in platforms for a in ACTIONS]


def _package_parent():
    return str(Path(__file__).resolve().parent.parent)


def _needs_pythonpath(parent):
    """True when ovpnx runs from a source checkout rather than an installed package."""
    return "site-packages" not in parent and "dist-packages" not in parent


def render(name, python=None, parent=None):
    """Return the launcher script text for shortcut `name`."""
    python = python or sys.executable or "python3"
    parent = parent or _package_parent()
    lines = ["#!/bin/sh", MARKER]
    if _needs_pythonpath(parent):
        lines.append('PYTHONPATH=%s${PYTHONPATH:+:$PYTHONPATH}' % shlex.quote(parent))
        lines.append("export PYTHONPATH")
    code = ("import sys; from ovpnx.cli import main; "
            'sys.exit(main(sys.argv[1:], prog="%s"))' % name)
    lines.append('exec %s -c %s "$@"' % (shlex.quote(python), shlex.quote(code)))
    return "\n".join(lines) + "\n"


def is_ours(path):
    """True if `path` is a regular file written by ovpnx."""
    try:
        if path.is_symlink() or not path.is_file():
            return False
        with open(str(path), "r", errors="replace") as fh:
            head = fh.read(512)
    except OSError:
        return False
    return MARKER in head


def install(directory, names, python=None, parent=None):
    """Write launchers. Returns (created, updated, skipped) name lists."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    created, updated, skipped = [], [], []
    for name in names:
        target = directory / name
        existed = target.exists() or target.is_symlink()
        if existed and not is_ours(target):
            skipped.append(name)
            continue
        target.write_text(render(name, python, parent))
        mode = target.stat().st_mode
        target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        (updated if existed else created).append(name)
    return created, updated, skipped


def remove(directory, names):
    """Delete launchers that ovpnx created. Returns (removed, skipped) lists."""
    directory = Path(directory)
    removed, skipped = [], []
    for name in names:
        target = directory / name
        if not (target.exists() or target.is_symlink()):
            continue
        if is_ours(target):
            target.unlink()
            removed.append(name)
        else:
            skipped.append(name)
    return removed, skipped


def on_path(directory, environ=None):
    env = os.environ if environ is None else environ
    wanted = os.path.realpath(str(directory))
    for entry in env.get("PATH", "").split(os.pathsep):
        if entry and os.path.realpath(entry) == wanted:
            return True
    return False
