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
    """Pick a profile by name: exact, case-insensitive, then unique prefix."""
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
