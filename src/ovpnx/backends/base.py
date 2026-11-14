"""Backend-neutral types and helpers."""
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

CONFIG_PREFIX = "ovpnx-"     # every config ovpnx imports starts with this
IMPORT_TAG = "ovpnx"


class BackendError(Exception):
    def __init__(self, message, hints=None, output=None):
        super().__init__(message)
        self.hints = list(hints or [])
        self.output = output


@dataclass
class Session:
    path: str
    config_name: str = ""
    device: str = ""
    status: str = ""
    connected_to: str = ""
    pid: Optional[int] = None
    owner: str = ""
    created: str = ""

    @property
    def connected(self):
        return "client connected" in self.status.lower()

    @property
    def managed(self):
        """True if this session was started from a config imported by ovpnx."""
        return self.config_name.startswith(CONFIG_PREFIX)


def make_config_name(platform, profile):
    """Namespaced, shell-safe name for an imported config."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", profile)
    return "%s%s-%s" % (CONFIG_PREFIX, platform, safe)


def split_config_name(name, platform_keys: Sequence[str]) -> Optional[Tuple[str, str]]:
    """Best effort: 'ovpnx-htb-eu-4' -> ('htb', 'eu-4') using known platforms."""
    if not name.startswith(CONFIG_PREFIX):
        return None
    rest = name[len(CONFIG_PREFIX):]
    for key in sorted(platform_keys, key=len, reverse=True):
        if rest.startswith(key + "-"):
            return key, rest[len(key) + 1:]
    return None


def failure_hints(output, platform_key, compression) -> List[str]:
    """Turn raw backend output into advice the user can act on."""
    low = (output or "").lower()
    hints = []
    if "compression" in low and "not allowed" in low:
        if compression == "none":
            hints.append(
                "The server pushed compression that the client refuses. If you trust "
                "this platform, set 'compression = asym' in [platform:%s] of your "
                "config (receive-only, keeps most of the security)." % platform_key
            )
        else:
            hints.append(
                "Compression is already '%s' for this platform. Only if the server "
                "still refuses, 'yes' may work, but it weakens security." % compression
            )
    if "auth" in low and ("fail" in low or "denied" in low or "invalid" in low):
        hints.append(
            "Authentication failed: check your username/password, or download a "
            "fresh profile from the platform."
        )
    if "timeout" in low or "timed out" in low:
        hints.append(
            "The connection timed out: check your internet connection, try another "
            "profile/server, or raise connect_timeout in the config."
        )
    if "org.freedesktop" in low or "dbus" in low:
        hints.append(
            "OpenVPN 3 services are not reachable. Try 'openvpn3 sessions-list' to "
            "see the underlying error."
        )
    return hints
