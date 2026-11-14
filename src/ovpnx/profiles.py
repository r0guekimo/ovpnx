"""Discover and select .ovpn profiles. Profile files are only ever read.

The tool never modifies, moves or rewrites a profile. Reading is limited to
hashing (for later import de-duplication) and spotting the 'auth-user-pass'
directive; no other content is kept or displayed.
"""
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

MAX_SCAN_BYTES = 1024 * 1024
_SKIP_USERS = {"public", "default", "default user", "all users", "desktop.ini"}


class ProfileError(Exception):
    pass


@dataclass
class Profile:
    platform: str
    name: str
    path: Path
    auth: Optional[str] = None   # None | "prompt" | "file" | "inline"


@dataclass
class ScanResult:
    platform: object
    profiles: List[Profile] = field(default_factory=list)
    error: Optional[str] = None


def file_sha256(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def detect_auth(path):
    """Report how a profile asks for credentials, without keeping any of them."""
    seen = 0
    try:
        with open(str(path), "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                seen += len(line)
                if seen > MAX_SCAN_BYTES:
                    break
                parts = line.strip().split(None, 1)
                if not parts or parts[0][0] in "#;":
                    continue
                token = parts[0].lower()
                if token == "auth-user-pass":
                    return "file" if len(parts) > 1 else "prompt"
                if token == "<auth-user-pass>":
                    return "inline"
    except OSError:
        pass
    return None


def scan_platform(platform):
    result = ScanResult(platform=platform)
    directory = platform.profile_dir
    if not directory.exists():
        result.error = "profile directory does not exist: %s" % directory
        return result
    if not directory.is_dir():
        result.error = "profile path is not a directory: %s" % directory
        return result
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        result.error = "cannot read %s: %s" % (directory, exc.strerror or exc)
        return result
    for entry in entries:
        if entry.suffix.lower() != ".ovpn":
            continue
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        result.profiles.append(
            Profile(platform.key, entry.stem, entry, detect_auth(entry))
        )
    return result


def find_profile(platform, profiles, name=None):
    """Pick a profile by name: exact, case-insensitive, unique prefix, then unique substring."""
    available = ", ".join(p.name for p in profiles) or "none found"
    if not name:
        name = platform.default_profile
    if not name:
        if len(profiles) == 1:
            return profiles[0]
        raise ProfileError(
            "no profile given and no default_profile set for '%s'. Available: %s"
            % (platform.key, available)
        )
    if name.lower().endswith(".ovpn"):
        name = name[:-5]

    for p in profiles:
        if p.name == name:
            return p
    folded = [p for p in profiles if p.name.lower() == name.lower()]
    if len(folded) == 1:
        return folded[0]
    if len(folded) > 1:
        raise ProfileError(
            "'%s' matches several profiles: %s" % (name, ", ".join(p.name for p in folded))
        )
    prefixed = [p for p in profiles if p.name.lower().startswith(name.lower())]
    if len(prefixed) == 1:
        return prefixed[0]
    if len(prefixed) > 1:
        raise ProfileError(
            "'%s' is ambiguous: %s" % (name, ", ".join(p.name for p in prefixed))
        )
    inside = [p for p in profiles if name.lower() in p.name.lower()]
    if len(inside) == 1:
        return inside[0]
    if len(inside) > 1:
        raise ProfileError(
            "'%s' is ambiguous: %s" % (name, ", ".join(p.name for p in inside))
        )
    raise ProfileError(
        "no profile named '%s' for '%s'. Available: %s" % (name, platform.key, available)
    )


# ------------------------------------------------------ suggestions (init)
def _count_ovpn(directory):
    try:
        with os.scandir(str(directory)) as it:
            return sum(
                1 for e in it if e.name.lower().endswith(".ovpn") and e.is_file()
            )
    except OSError:
        return 0


def _subdirs(base, cap=200):
    out = []
    try:
        with os.scandir(str(base)) as it:
            for e in it:
                if e.name.startswith(".") or not e.is_dir():
                    continue
                out.append(Path(e.path))
                if len(out) >= cap:
                    break
    except OSError:
        pass
    return sorted(out)


def find_candidate_dirs(home, wsl=False, mnt_root="/mnt", limit=10):
    """Look in common places for folders that already hold .ovpn files.

    Only used to *suggest* profile_dir values; nothing is configured from it.
    """
    home = Path(home)
    bases = [home, home / "Downloads", home / "Desktop", home / "Documents",
             home / "vpn-profiles"]
    if wsl:
        try:
            drives = sorted(os.listdir(mnt_root))
        except OSError:
            drives = []
        for drive in drives:
            users = Path(mnt_root) / drive / "Users"
            if len(drive) != 1 or not users.is_dir():
                continue
            try:
                names = sorted(os.listdir(str(users)))
            except OSError:
                continue
            for name in names:
                if name.lower() in _SKIP_USERS:
                    continue
                u = users / name
                bases += [u, u / "Desktop", u / "Downloads", u / "Documents",
                          u / "OneDrive" / "Desktop"]
    found, seen = [], set()
    for base in bases:
        for directory in [base] + _subdirs(base):
            if directory in seen:
                continue
            seen.add(directory)
            count = _count_ovpn(directory)
            if count:
                found.append((directory, count))
                if len(found) >= limit:
                    return found
    return found


_SKIP_DIRS = {"node_modules", "snap", "venv", "site-packages", "__pycache__", "appdata",
              "library", "go", "proc", "sys", "dev", "windows", "program files"}


def find_ovpn_dirs(home, wsl=False, mnt_root="/mnt", max_depth=4, max_dirs=6000, limit=30):
    """Search a few levels below the home folder (and the Windows user folders
    on WSL) for directories that contain .ovpn files.

    Bounded by depth and by the number of folders visited, never follows
    symlinks, skips hidden and bulky folders. Only used to suggest folders.
    Returns a sorted list of (Path, count).
    """
    roots = [Path(home)]
    if wsl:
        try:
            drives = sorted(os.listdir(mnt_root))
        except OSError:
            drives = []
        for drive in drives:
            users = Path(mnt_root) / drive / "Users"
            if len(drive) != 1 or not users.is_dir():
                continue
            try:
                names = sorted(os.listdir(str(users)))
            except OSError:
                continue
            for name in names:
                if name.lower() in _SKIP_USERS:
                    continue
                for sub in ("Desktop", "Downloads", "Documents", "OneDrive/Desktop"):
                    roots.append(users / name / sub)

    found, seen, visited = [], set(), 0
    queue = [(r, 0) for r in roots]
    while queue and visited < max_dirs and len(found) < limit:
        directory, depth = queue.pop(0)
        if directory in seen:
            continue
        seen.add(directory)
        visited += 1
        count, children = 0, []
        try:
            with os.scandir(str(directory)) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if (depth < max_depth and not entry.name.startswith(".")
                                    and entry.name.lower() not in _SKIP_DIRS):
                                children.append(Path(entry.path))
                        elif (entry.name.lower().endswith(".ovpn")
                              and entry.is_file(follow_symlinks=False)):
                            count += 1
                    except OSError:
                        continue
        except OSError:
            continue
        if count:
            found.append((directory, count))
        queue.extend((c, depth + 1) for c in sorted(children))
    return sorted(found)
