"""Environment detection: native Linux vs WSL, systemd, privileges, backends.

Everything that touches the system is injectable (run, which, paths) so the
logic can be tested without a real WSL or OpenVPN installation.
"""
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

EXTRA_BIN_DIRS = ("/usr/sbin", "/sbin", "/usr/local/sbin")


def run_command(cmd, timeout=5):
    """Run a command, return (returncode, combined output) or None on failure."""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.returncode, proc.stdout


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except OSError:
        return ""


def detect_wsl(osrelease, environ):
    """Return (is_wsl, wsl_version) from the kernel release string and env."""
    low = osrelease.lower()
    if "microsoft" in low or "wsl" in low:
        if "wsl2" in low or "microsoft-standard" in low:
            return True, 2
        return True, 1
    if "WSL_INTEROP" in environ or "WSL_DISTRO_NAME" in environ:
        return True, None
    return False, None


def running_in_wsl(environ=None, osrelease_path="/proc/sys/kernel/osrelease"):
    env = os.environ if environ is None else environ
    return detect_wsl(_read_text(osrelease_path), env)[0]


def parse_os_release(text):
    """Return PRETTY_NAME (or NAME) from /etc/os-release content."""
    values = {}
    for line in text.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values.get("PRETTY_NAME") or values.get("NAME") or "unknown distro"


def parse_version(text, pattern):
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1) if match else None


@dataclass
class Backend:
    key: str                 # "openvpn3" or "openvpn2"
    path: str
    version: Optional[str]


@dataclass
class Environment:
    platform_name: str
    is_wsl: bool
    wsl_version: Optional[int]
    wsl_networking: Optional[str]
    distro: str
    systemd: bool
    uid: Optional[int]
    has_sudo: bool
    has_ip: bool
    python: str
    backends: Dict[str, Backend] = field(default_factory=dict)

    @property
    def is_linux(self):
        return self.platform_name.startswith("linux")

    @property
    def is_root(self):
        return self.uid == 0

    @property
    def kind(self):
        if self.is_wsl:
            return "WSL%s" % (self.wsl_version or "")
        if self.is_linux:
            return "native Linux"
        return "unsupported OS (%s)" % self.platform_name


def _find(binary, which, extra_dirs):
    path = which(binary)
    if path:
        return path
    for directory in extra_dirs:
        candidate = os.path.join(directory, binary)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _probe(key, binary, version_args, pattern, run, which, extra_dirs):
    path = _find(binary, which, extra_dirs)
    if not path:
        return None
    result = run([path] + version_args)
    version = parse_version(result[1], pattern) if result else None
    return Backend(key=key, path=path, version=version)


def detect_environment(
    run=run_command,
    which=shutil.which,
    environ=None,
    osrelease_path="/proc/sys/kernel/osrelease",
    os_release_path="/etc/os-release",
    systemd_path="/run/systemd/system",
    platform_name=None,
    extra_dirs=EXTRA_BIN_DIRS,
):
    env = os.environ if environ is None else environ
    is_wsl, wsl_version = detect_wsl(_read_text(osrelease_path), env)

    networking = None
    if is_wsl:
        wslinfo = which("wslinfo")
        if wslinfo:
            result = run([wslinfo, "--networking-mode"])
            if result and result[0] == 0 and result[1].strip():
                networking = result[1].strip().splitlines()[0].strip().lower()

    backends = {}
    ovpn3 = _probe(
        "openvpn3", "openvpn3", ["version"],
        r"OpenVPN3/Linux v?(\d+(?:\.\d+)*)", run, which, extra_dirs,
    )
    if ovpn3:
        backends["openvpn3"] = ovpn3
    # `openvpn --version` exits with status 1, so only the text is used.
    ovpn2 = _probe(
        "openvpn2", "openvpn", ["--version"],
        r"^OpenVPN (\d+(?:\.\d+)+)", run, which, extra_dirs,
    )
    if ovpn2:
        backends["openvpn2"] = ovpn2

    return Environment(
        platform_name=platform_name or sys.platform,
        is_wsl=is_wsl,
        wsl_version=wsl_version,
        wsl_networking=networking,
        distro=parse_os_release(_read_text(os_release_path)),
        systemd=os.path.isdir(systemd_path),
        uid=os.geteuid() if hasattr(os, "geteuid") else None,
        has_sudo=_find("sudo", which, extra_dirs) is not None,
        has_ip=_find("ip", which, extra_dirs) is not None,
        python="%d.%d.%d" % sys.version_info[:3],
        backends=backends,
    )
