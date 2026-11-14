"""VPN backends and automatic selection."""
import logging

from .base import BackendError
from .openvpn2 import OpenVPN2Backend
from .openvpn3 import OpenVPN3Backend

OPENVPN3_URL = "https://community.openvpn.net/openvpn/wiki/OpenVPN3Linux"
log = logging.getLogger("ovpnx")


class MultiBackend:
    """A primary backend that also *sees* sessions of the other backend.

    That keeps "one VPN at a time" honest when, say, an OpenVPN 2 tunnel was
    started by hand while ovpnx uses OpenVPN 3. New connections always go
    through the primary; disconnecting is routed to whichever backend owns the
    session.
    """

    def __init__(self, primary, extras):
        self.primary = primary
        self.extras = list(extras)
        self.key = primary.key
        self.label = primary.label
        self._config_owner = {}

    def _all(self):
        return [self.primary] + self.extras

    def _owner_of_session(self, session):
        for backend in self._all():
            if backend.key == session.backend:
                return backend
        return self.primary

    def list_sessions(self):
        sessions = list(self.primary.list_sessions())
        for extra in self.extras:
            try:
                sessions.extend(extra.list_sessions())
            except BackendError as exc:
                log.debug("could not list %s sessions: %s", extra.key, exc)
        return sessions

    def prepare_config(self, *args, **kwargs):
        return self.primary.prepare_config(*args, **kwargs)

    def start(self, *args, **kwargs):
        return self.primary.start(*args, **kwargs)

    def disconnect(self, session):
        self._owner_of_session(session).disconnect(session)

    def cleanup_session(self, session):
        self._owner_of_session(session).cleanup_session(session)

    def remove_config(self, name=None, path=None):
        owner = self._config_owner.get(name, self.primary)
        owner.remove_config(name=name, path=path)

    def list_managed_configs(self):
        names = []
        for backend in self._all():
            try:
                found = backend.list_managed_configs()
            except BackendError as exc:
                log.debug("could not list %s configs: %s", backend.key, exc)
                continue
            for name in found:
                self._config_owner.setdefault(name, backend)
                names.append(name)
        return names


def _openvpn2(env, state_dir, strict):
    info = env.backends["openvpn2"]
    if strict and not env.is_root and not env.has_sudo:
        raise BackendError(
            "OpenVPN 2 needs administrator rights and 'sudo' is not installed.",
            hints=["Install sudo, or run ovpnx as root.",
                   "OpenVPN 3 needs no root: %s" % OPENVPN3_URL])
    return OpenVPN2Backend(command=info.path, state_dir=state_dir,
                           version=info.version, uid=env.uid)


def _combine(primary, extras):
    extras = [e for e in extras if e is not None]
    return MultiBackend(primary, extras) if extras else primary


def select_backend(preference, env, state_dir=None):
    """Return a ready backend or raise BackendError with advice.

    preference is 'auto', 'openvpn3' or 'openvpn2'. 'auto' prefers OpenVPN 3
    when it is installed and its services respond, else uses OpenVPN 2.
    """
    if state_dir is None:
        from ..config import state_dir as default_state_dir
        state_dir = default_state_dir()
    have3 = env.backends.get("openvpn3")
    have2 = env.backends.get("openvpn2")

    if preference == "openvpn2":
        if not have2:
            raise BackendError(
                "backend = openvpn2 is set but 'openvpn' is not installed.",
                hints=["Install it, e.g.: sudo apt install openvpn"])
        return _combine(_openvpn2(env, state_dir, strict=True), [])

    if preference == "openvpn3" and not have3:
        raise BackendError("backend = openvpn3 is set but 'openvpn3' is not installed.",
                           hints=["Install OpenVPN 3: %s" % OPENVPN3_URL])

    ov3_hints = []
    if have3:
        ov3 = OpenVPN3Backend(command=have3.path)
        if ov3.service_ok():
            extra = _openvpn2(env, state_dir, strict=False) if have2 else None
            return _combine(ov3, [extra])
        ov3_hints = ["Run 'openvpn3 sessions-list' to see the underlying error."]
        if not env.systemd:
            ov3_hints.insert(0, "systemd is not running; on WSL enable it in /etc/wsl.conf "
                                "([boot] systemd=true), then 'wsl --shutdown'.")
        if preference == "openvpn3":
            raise BackendError("OpenVPN 3 is installed but its services do not respond.",
                               hints=ov3_hints)

    if have2:
        log.debug("using OpenVPN 2 (OpenVPN 3 %s)",
                  "not responding" if have3 else "not installed")
        return _combine(_openvpn2(env, state_dir, strict=True), [])

    raise BackendError(
        "No OpenVPN installation found.",
        hints=["Install OpenVPN 3 (%s) or OpenVPN 2 (e.g. 'sudo apt install openvpn')."
               % OPENVPN3_URL])
