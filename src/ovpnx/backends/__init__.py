"""VPN backends and automatic selection."""
from .base import BackendError
from .openvpn3 import OpenVPN3Backend

OPENVPN3_URL = "https://community.openvpn.net/openvpn/wiki/OpenVPN3Linux"


def select_backend(preference, env):
    """Return a ready backend or raise BackendError with advice.

    preference is 'auto', 'openvpn3' or 'openvpn2'.
    """
    have3 = env.backends.get("openvpn3")
    have2 = env.backends.get("openvpn2")

    if preference == "openvpn2":
        raise BackendError(
            "The OpenVPN 2 backend is not implemented yet (roadmap step 5).",
            hints=["Set backend = auto or openvpn3 in your config."],
        )
    if preference == "openvpn3" and not have3:
        raise BackendError(
            "backend = openvpn3 is set but 'openvpn3' is not installed.",
            hints=["Install OpenVPN 3: %s" % OPENVPN3_URL],
        )
    if have3:
        backend = OpenVPN3Backend(command=have3.path)
        if backend.service_ok():
            return backend
        hints = ["Run 'openvpn3 sessions-list' to see the underlying error."]
        if not env.systemd:
            hints.insert(0, "systemd is not running; on WSL enable it in /etc/wsl.conf "
                            "([boot] systemd=true), then 'wsl --shutdown'.")
        if have2:
            raise BackendError(
                "OpenVPN 3 services do not respond, and the OpenVPN 2 backend is not "
                "implemented yet (roadmap step 5).", hints=hints)
        raise BackendError("OpenVPN 3 is installed but its services do not respond.",
                           hints=hints)
    if have2:
        raise BackendError(
            "Only OpenVPN 2 is installed, and its backend is not implemented yet "
            "(roadmap step 5).",
            hints=["Install OpenVPN 3 to use ovpnx now: %s" % OPENVPN3_URL],
        )
    raise BackendError(
        "No OpenVPN installation found.",
        hints=["Install OpenVPN 3 (%s) or OpenVPN 2 (e.g. 'sudo apt install openvpn')."
               % OPENVPN3_URL],
    )
