"""Tiny helpers around the `ip` command."""
import re

from .env import run_command

_DEVICE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
_INET_RE = re.compile(r"\binet\s+(\d{1,3}(?:\.\d{1,3}){3})/(\d+)")


def valid_device(name):
    """True for plausible interface names (guards what we pass to `ip`)."""
    return bool(name) and bool(_DEVICE_RE.match(name))


def interface_ipv4(device, run=run_command):
    """Return the first IPv4 address on a device, or None."""
    if not valid_device(device):
        return None
    result = run(["ip", "-4", "-o", "addr", "show", "dev", device])
    if not result or result[0] != 0:
        return None
    match = _INET_RE.search(result[1])
    return match.group(1) if match else None
