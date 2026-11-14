"""Guided setup: find folders with .ovpn files, let the user pick the platforms,
name them and choose defaults. Nothing is configured without an explicit choice.

All questions go through `ask` (input() by default) so tests can script them.
"""
import os
import re
from pathlib import Path

from . import config as cfg
from .log import clean
from .profiles import find_ovpn_dirs, scan_platform

_GENERIC = {"vpn", "vpns", "ovpn", "openvpn", "profiles", "profile", "config", "configs",
            "files", "lab", "labs", "downloads", "desktop", "documents", "connection",
            "connections", "keys", "openvpn-configs"}
_SECTION_RE = re.compile(r"^\s*\[platform:([^\]]+)\]", re.M | re.I)


class Aborted(Exception):
    pass


def suggest_label(directory, home):
    """Name a folder after its nearest meaningful part (~/CTF/HTB/vpn -> HTB)."""
    d, home = Path(directory), Path(home)
    while d != home and d.parent != d:
        if d.name.lower() not in _GENERIC:
            return d.name
        d = d.parent
    return "vpn"


def make_key(label, taken):
    key = re.sub(r"[^a-z0-9_-]+", "-", label.lower()).strip("-_")[:28] or "vpn"
    base, n = key, 2
    while key in taken:
        key = "%s-%d" % (base, n)
        n += 1
    return key


def existing_keys(text):
    return {m.group(1).strip().lower() for m in _SECTION_RE.finditer(text)}


def _short(path, home):
    return cfg.tilde(path, home)


def _pick_numbers(answer, count):
    picked = []
    for part in answer.replace(" ", ",").split(","):
        if not part:
            continue
        if not part.isdigit() or not 1 <= int(part) <= count:
            return None
        if int(part) - 1 not in picked:
            picked.append(int(part) - 1)
    return picked


def _choose_folders(con, ask, found, home):
    """Return the chosen folders as a list of Paths."""
    chosen = []
    while True:
        if found:
            con.plain()
            for i, (directory, count) in enumerate(found, 1):
                mark = "*" if directory in chosen else " "
                con.plain("   %s %d) %s   (%d file%s)" % (
                    mark, i, _short(directory, home), count, "" if count == 1 else "s"))
            con.plain()
            hint = ("Numbers (e.g. 1,3) to pick folders, 'p' to type a path yourself, "
                    "Enter when done")
        else:
            hint = "'p' to type a folder path, Enter to stop"
        answer = ask("%s: " % hint).strip()
        if not answer:
            return chosen
        if answer.lower() == "p":
            raw = ask("Folder with .ovpn files: ").strip().strip("\"'")
            path = Path(os.path.expanduser(raw)) if raw else None
            if not path or not path.is_dir():
                con.warn("Not a folder: %s" % (raw or "(empty)"))
                continue
            path = path.resolve()
            count = sum(1 for e in path.iterdir() if e.suffix.lower() == ".ovpn")
            if not count:
                con.warn("No .ovpn files in %s" % path)
                continue
            if all(path != d for d, _ in found):
                found.append((path, count))
            if path not in chosen:
                chosen.append(path)
            continue
        picked = _pick_numbers(answer, len(found))
        if picked is None:
            con.warn("Type numbers from the list, 'p', or just press Enter.")
            continue
        for index in picked:
            directory = found[index][0]
            if directory not in chosen:
                chosen.append(directory)


def _ask_text(con, ask, question, default, validator=None):
    while True:
        answer = ask("%s [%s]: " % (question, default)).strip()
        value = answer or default
        problem = validator(value) if validator else None
        if not problem:
            return value
        con.warn(problem)


def _display_ok(value):
    if clean(value) != value or "%" in value or ";" in value:
        return "Use plain text (no ';' or '%')."
    return None


def _configure(con, ask, directory, taken, home):
    plat = cfg.Platform("tmp", "tmp", directory)
    profiles = scan_platform(plat).profiles
    con.plain()
    con.info("%s  (%d profile%s)" % (_short(directory, home), len(profiles),
                                     "" if len(profiles) == 1 else "s"))
    if ";" in str(directory):
        con.warn("Skipped: a ';' in the folder name cannot be stored in the config. "
                 "Rename the folder first.")
        return None
    label = suggest_label(directory, home)
    suggested = make_key(label, taken)

    def key_ok(value):
        if not cfg._NAME_RE.match(value.lower()):
            return "Use 1-32 characters: a-z, 0-9, '-' or '_'."
        if value.lower() in taken:
            return "Already used by another platform."
        return None

    con.plain("   Short name: used for the commands, e.g. %s-start / %s-stop"
              % (suggested, suggested))
    key = _ask_text(con, ask, "Short name", suggested, key_ok).lower()
    display = _ask_text(con, ask, "Display name", label, _display_ok)

    default_profile = None
    if profiles:
        for i, prof in enumerate(profiles, 1):
            note = "  (asks for login)" if prof.auth == "prompt" else ""
            con.plain("      %d) %s%s" % (i, prof.name, note))
        fallback = profiles[0].name if len(profiles) == 1 else ""
        while True:
            hint = "Default profile for '%s-start' (number or name" % key
            hint += ", Enter = %s)" % (fallback or "none")
            answer = ask("%s: " % hint).strip()
            if not answer:
                default_profile = fallback or None
                break
            if answer.isdigit() and 1 <= int(answer) <= len(profiles):
                default_profile = profiles[int(answer) - 1].name
                break
            match = [p.name for p in profiles if p.name.lower() == answer.lower()]
            if match:
                default_profile = match[0]
                break
            con.warn("Not in the list.")

    con.plain("   Compression: leave as 'none' unless connecting fails with a compression")
    con.plain("   error (then choose 'asym', which is receive-only and safe).")
    while True:
        answer = ask("Compression none/asym [none]: ").strip().lower() or "none"
        if answer in ("none", "asym"):
            compression = answer
            break
        con.warn("Type none or asym.")
    return {"key": key, "display_name": display, "profile_dir": directory,
            "default_profile": default_profile, "compression": compression}


def run_setup(con, path, home, ask=input, wsl=False, mnt_root="/mnt"):
    """Interactive setup. Returns the list of platform keys written, or None
    if cancelled or nothing was chosen."""
    path = Path(path)
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            existing = ""
    taken = existing_keys(existing)

    try:
        mode = "new"
        if existing.strip():
            con.info("A config already exists: %s" % path)
            if taken:
                con.plain("   Platforms: %s" % ", ".join(sorted(taken)))
            answer = ask("[a]dd platforms to it, [r]eplace it (a backup is kept), "
                         "[c]ancel? [a]: ").strip().lower() or "a"
            if answer.startswith("c"):
                raise Aborted()
            if answer.startswith("r"):
                mode, taken = "replace", set()

        con.info("Looking for .ovpn files in your home folder%s..."
                 % (" and Windows folders" if wsl else ""))
        found = [(d, c) for d, c in find_ovpn_dirs(home, wsl=wsl, mnt_root=mnt_root)]
        if found:
            con.ok("Found %d folder%s with .ovpn files." % (len(found), "" if len(found) == 1 else "s"))
        else:
            con.warn("No .ovpn files found automatically.")
        folders = _choose_folders(con, ask, found, home)
        if not folders:
            raise Aborted()

        results = []
        for directory in folders:
            item = _configure(con, ask, directory, taken, home)
            if item:
                taken.add(item["key"])
                results.append(item)
        if not results:
            raise Aborted()

        con.plain()
        con.info("Summary")
        for r in results:
            con.plain("   %-10s %s -> %s%s%s" % (
                r["key"], r["display_name"], _short(r["profile_dir"], home),
                "   default: %s" % r["default_profile"] if r["default_profile"] else "",
                "   compression: %s" % r["compression"] if r["compression"] != "none" else ""))
        if ask("Write this to %s? [Y/n]: " % path).strip().lower().startswith("n"):
            raise Aborted()
    except (Aborted, EOFError, KeyboardInterrupt):
        con.plain()
        con.warn("Setup cancelled; nothing was changed.")
        return None

    sections = "\n".join(cfg.render_platform(
        r["key"], r["display_name"], r["profile_dir"], r["default_profile"],
        r["compression"], home) for r in results)
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "new" and not existing.strip():
        text = cfg.HEADER + "\n" + sections
    elif mode == "replace":
        backup = path.with_name(path.name + ".bak")
        backup.write_text(existing, encoding="utf-8")
        con.info("Old config saved as %s" % backup)
        text = cfg.HEADER + "\n" + sections
    else:
        text = existing.rstrip("\n") + "\n\n" + sections
    path.write_text(text, encoding="utf-8")
    con.ok("Saved %s" % path)
    return [r["key"] for r in results]
