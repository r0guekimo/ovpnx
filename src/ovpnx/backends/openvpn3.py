"""OpenVPN 3 (openvpn3-linux) backend.

Every imported profile is namespaced ('ovpnx-<platform>-<profile>') and
tagged, so ovpnx never touches configs the user imported themselves. Profile
files are passed to `config-import` read-only; per-platform settings such as
compression are applied as overrides on the imported copy.
"""
import re
import time

from ..env import run_command
from ..runner import run_streaming
from .base import IMPORT_TAG, BackendError, Session

_LABEL_RE = re.compile(
    r"(Path|Created|PID|Owner|Device|Config name|Connected to|Status):"
)
_CONFIG_PATH_RE = re.compile(r"Configuration path:\s*(/net/openvpn/v3/configuration/\S+)")
_SESSION_PATH_RE = re.compile(r"Session path:\s*(/net/openvpn/v3/sessions/\S+)")
_FIELD_MAP = {
    "Path": "path", "Created": "created", "PID": "pid", "Owner": "owner",
    "Device": "device", "Config name": "config_name",
    "Connected to": "connected_to", "Status": "status",
}


def parse_sessions(text):
    """Parse `openvpn3 sessions-list` output into Session objects."""
    sessions, current = [], None
    for line in text.splitlines():
        matches = list(_LABEL_RE.finditer(line))
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
            value = line[match.end():end].strip()
            field_name = _FIELD_MAP[match.group(1)]
            if field_name == "path":
                if current is not None:
                    sessions.append(current)
                current = Session(path=value)
            elif current is not None:
                if field_name == "pid":
                    current.pid = int(value) if value.isdigit() else None
                else:
                    setattr(current, field_name, value)
    if current is not None:
        sessions.append(current)
    return sessions


def parse_config_path(text):
    match = _CONFIG_PATH_RE.search(text)
    return match.group(1) if match else None


def parse_session_path(text):
    match = _SESSION_PATH_RE.search(text)
    return match.group(1) if match else None


def parse_config_names(text):
    """Parse the table printed by `openvpn3 configs-list`."""
    names = []
    for line in text.splitlines():
        stripped = line.strip()
        if (not stripped or set(stripped) <= {"-"}
                or stripped.startswith("Configuration Name")
                or stripped.lower().startswith("no configuration")):
            continue
        names.append(re.split(r"\s{2,}", stripped)[0])
    return names


class OpenVPN3Backend:
    key = "openvpn3"
    label = "OpenVPN 3"

    def __init__(self, command="openvpn3", run=run_command, stream=run_streaming,
                 sleep=time.sleep):
        self.command = command
        self._run = run
        self._stream = stream
        self._sleep = sleep

    # -------------------------------------------------------- plumbing
    def _call(self, args, timeout=20):
        result = self._run([self.command] + list(args), timeout=timeout)
        if result is None:
            raise BackendError("could not run '%s %s'" % (self.command, args[0]))
        return result

    def service_ok(self):
        try:
            return self._call(["sessions-list"])[0] == 0
        except BackendError:
            return False

    # -------------------------------------------------------- sessions
    def list_sessions(self):
        rc, out = self._call(["sessions-list"])
        if rc != 0:
            raise BackendError("'openvpn3 sessions-list' failed", output=out)
        sessions = parse_sessions(out)
        for session in sessions:
            session.backend = self.key
        return sessions

    def wait_connected(self, session_path, timeout=10):
        waited, last = 0, None
        while True:
            for session in self.list_sessions():
                if session.path == session_path:
                    last = session
                    if session.connected:
                        return session
            if waited >= timeout:
                break
            self._sleep(1)
            waited += 1
        status = last.status if last else "session not found"
        raise BackendError("the session did not reach the connected state (%s)" % status)

    def wait_gone(self, session_path, timeout=10):
        waited = 0
        while True:
            if all(s.path != session_path for s in self.list_sessions()):
                return True
            if waited >= timeout:
                return False
            self._sleep(1)
            waited += 1

    # --------------------------------------------------------- configs
    def config_exists(self, name):
        rc, out = self._call(["config-manage", "--config", name, "--exists"])
        if rc == 0:
            return True
        if "no configuration profiles found" in out.lower():
            return False
        raise BackendError("could not check for config '%s'" % name, output=out)

    def remove_config(self, name=None, path=None):
        if path:
            args = ["config-remove", "--path", path, "--force"]
        elif name:
            args = ["config-remove", "--config", name, "--force"]
        else:
            raise ValueError("name or path required")
        rc, out = self._call(args)
        if rc != 0:
            raise BackendError("could not remove config '%s'" % (name or path), output=out)

    def list_managed_configs(self):
        rc, out = self._call(["configs-list"])
        if rc != 0:
            raise BackendError("'openvpn3 configs-list' failed", output=out)
        return [n for n in parse_config_names(out) if n.startswith("ovpnx-")]

    def prepare_config(self, profile_file, name, compression, auth=None):
        """Import the profile under `name` (replacing our own older copy).

        Returns the configuration path used for all later calls.
        """
        if self.config_exists(name):
            self.remove_config(name=name)
        rc, out = self._call(
            ["config-import", "--config", str(profile_file), "--name", name,
             "--tag", IMPORT_TAG]
        )
        path = parse_config_path(out) if rc == 0 else None
        if not path:
            raise BackendError("could not import the profile", output=out)
        if compression != "none":
            rc, out = self._call(
                ["config-manage", "--path", path, "--allow-compression", compression]
            )
            if rc != 0:
                self._best_effort_remove(path)
                raise BackendError("could not set the compression policy", output=out)
        return path

    def cleanup_session(self, session):
        """Remove the imported config behind a finished session (ours only)."""
        if session.managed:
            self.remove_config(name=session.config_name)

    def _best_effort_remove(self, path):
        try:
            self.remove_config(path=path)
        except BackendError:
            pass

    # ------------------------------------------------------ connection
    def start(self, config_path, timeout, interactive=False):
        """Start a session. `interactive` shows login prompts live."""
        cmd = [self.command, "session-start", "--config-path", config_path,
               "--timeout", str(timeout)]
        result = self._stream(
            cmd, echo=interactive, timeout=None if interactive else timeout + 30
        )
        if result is None:
            raise BackendError("could not run '%s session-start'" % self.command)
        rc, out = result
        if rc != 0:
            raise BackendError("the connection failed", output=out)
        session_path = parse_session_path(out)
        if not session_path:
            raise BackendError("connected, but no session path was reported", output=out)
        return self.wait_connected(session_path)

    def disconnect(self, session):
        rc, out = self._call(["session-manage", "--path", session.path, "--disconnect"])
        if rc != 0:
            raise BackendError("could not disconnect the session", output=out)
        self.wait_gone(session.path)
