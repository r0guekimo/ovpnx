"""OpenVPN 2 backend (the classic `openvpn` daemon).

How it works, based on real OpenVPN 2.7 behaviour:
  * the tunnel needs root, so only the single `openvpn` start and the final
    `kill` use sudo (never the whole tool);
  * `--daemon` returns immediately, so progress is read from a log file that
    ovpnx pre-creates in its private state directory (owned by you, mode 600);
  * every session gets `--daemon ovpnx-<platform>-<profile>`, which shows up in
    the process command line. That is how sessions are found again, and how
    other openvpn processes (not started by ovpnx) are recognised;
  * profile files are only passed with --config and never modified.

Not supported yet: profiles that prompt for a username/password. Prompts
cannot be shown once the daemon redirects its output, and writing a password
to a temporary file is something ovpnx will not do.
"""
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from ..env import run_command
from ..runner import run_detached
from .base import CONFIG_PREFIX, BackendError, Session

POLL = 0.5
LOG_TAIL_BYTES = 512 * 1024
HANDLE_PREFIX = "openvpn2:"


# ----------------------------------------------------------------- parsing
@dataclass
class Proc:
    pid: int
    args: List[str]


@dataclass
class LogInfo:
    state: str = "starting"   # starting|connecting|connected|reconnecting|stopping|failed
    device: str = ""
    connected_to: str = ""
    created: str = ""
    error: str = ""


_STATUS_TEXT = {
    "starting": "Connection, Connecting",
    "connecting": "Connection, Connecting",
    "connected": "Connection, Client connected",
    "reconnecting": "Connection, Reconnecting",
    "stopping": "Connection, Disconnecting",
    "failed": "Connection, Failed",
}
UNKNOWN_STATUS = "Running (started outside ovpnx, status unknown)"

_STAMP_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s")
_DEVICE_RE = re.compile(r"(?:TUN/TAP|DCO) device (\S+) opened")
_REMOTE_RE = re.compile(r"(TCP|UDP)v[46]?_CLIENT link remote: \[AF_INET6?\](\S+)")
# Real restart events only. A bare "ping-restart" must NOT match: it also appears in
# the server's normal PUSH_REPLY and "Timers:" lines of a healthy connection.
_RESTART_RE = re.compile(
    r"SIGUSR1\[|Connection reset, restarting|Inactivity timeout|Restart pause")
_CONNECTING_RE = re.compile(
    r"Attempting to establish|TCP connection established|TLS: Initial packet")


def parse_log(text):
    """Work out what an OpenVPN 2 log says about its connection."""
    info = LogInfo()
    for line in text.splitlines():
        stamp = _STAMP_RE.match(line)
        if stamp and not info.created:
            info.created = stamp.group(1)
        device = _DEVICE_RE.search(line)
        if device:
            info.device = device.group(1)
        remote = _REMOTE_RE.search(line)
        if remote:
            info.connected_to = "%s:%s" % (remote.group(1).lower(), remote.group(2))

        if "Initialization Sequence Completed" in line:
            info.state = "connected"
        elif "AUTH_FAILED" in line:
            info.state, info.error = "failed", "authentication failed"
        elif "Options error" in line or "Exiting due to fatal error" in line:
            info.state = "failed"
            info.error = info.error or " ".join(line.split(" ", 2)[-1].split())
        elif "SIGTERM[" in line:
            info.state = "stopping"
        elif _RESTART_RE.search(line):
            info.state = "reconnecting"
        elif _CONNECTING_RE.search(line) and info.state in ("starting", "failed"):
            info.state = "connecting"
    return info


def parse_openvpn_args(args):
    """Pick daemon name, config, log and dev out of an openvpn command line."""
    out = {"daemon": None, "config": None, "log": None, "dev": None}
    i = 1
    if len(args) > 1 and not args[1].startswith("-"):
        out["config"] = args[1]       # `openvpn client.conf` shorthand
        i = 2
    while i < len(args):
        arg = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if arg == "--daemon" and nxt is not None and not nxt.startswith("--"):
            out["daemon"] = nxt
            i += 2
        elif arg == "--config" and nxt is not None:
            out["config"] = nxt
            i += 2
        elif arg in ("--log", "--log-append") and nxt is not None:
            out["log"] = nxt
            i += 2
        elif arg == "--dev" and nxt is not None:
            out["dev"] = nxt
            i += 2
        else:
            i += 1
    return out


def list_processes(proc_root="/proc", comms=("openvpn",)):
    """All running openvpn (version 2) processes, read from /proc."""
    procs = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return procs
    for entry in entries:
        if not entry.isdigit():
            continue
        base = os.path.join(proc_root, entry)
        try:
            with open(os.path.join(base, "comm"), "r", errors="ignore") as fh:
                if fh.read().strip() not in comms:
                    continue
            with open(os.path.join(base, "cmdline"), "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        args = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        if args:
            procs.append(Proc(int(entry), args))
    return sorted(procs, key=lambda p: p.pid)


def version_tuple(version):
    parts = re.findall(r"\d+", version or "")
    return tuple(int(p) for p in parts[:3])


def supports_allow_compression(version):
    """--allow-compression exists from OpenVPN 2.5; older versions allow it anyway."""
    parsed = version_tuple(version)
    return not parsed or parsed >= (2, 5)


def _call_interactive(cmd):
    try:
        return subprocess.call(cmd)
    except OSError:
        return None


@dataclass
class _Job:
    file: str
    name: str
    compression: str
    cwd: str


# ----------------------------------------------------------------- backend
class OpenVPN2Backend:
    key = "openvpn2"
    label = "OpenVPN 2"

    def __init__(self, command="openvpn", state_dir=None, version=None,
                 run=run_command, launch=run_detached, interactive=_call_interactive,
                 sleep=time.sleep, proc_root="/proc", uid=None):
        self.command = command
        self.version = version
        self.dir = Path(state_dir if state_dir is not None else ".") / "openvpn2"
        self._run = run
        self._launch = launch
        self._interactive = interactive
        self._sleep = sleep
        self._proc_root = proc_root
        self._uid = os.geteuid() if uid is None and hasattr(os, "geteuid") else uid
        self._comms = ("openvpn", os.path.basename(command))
        self._pending = {}
        self._privileged = False

    # -------------------------------------------------------- plumbing
    @property
    def _sudo(self):
        return [] if self._uid == 0 else ["sudo", "-n"]

    def _call(self, cmd, timeout=15):
        result = self._run(cmd, timeout=timeout)
        if result is None:
            raise BackendError("could not run '%s'" % cmd[0 if cmd[0] != "sudo" else 2])
        return result

    def _ensure_privileges(self):
        """Make sure sudo will not prompt later (`sudo -n` is used afterwards)."""
        if self._privileged or self._uid == 0:
            return
        probe = self._run(["sudo", "-n", "true"], timeout=10)
        if probe is None or probe[0] != 0:
            if self._interactive(["sudo", "-v"]) != 0:
                raise BackendError(
                    "administrator rights are needed for OpenVPN 2 and sudo did not "
                    "grant them",
                    hints=["Check your sudo password, or run ovpnx as root.",
                           "OpenVPN 3 does not need root: use backend = openvpn3."])
        self._privileged = True

    def _log_path(self, name):
        return str(self.dir / (name + ".log"))

    def _fresh_log(self, name):
        """Create an empty log owned by the current user (root writes into it)."""
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._log_path(name)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        return path

    @staticmethod
    def _read_log(path):
        try:
            with open(path, "rb") as fh:
                size = fh.seek(0, 2)
                fh.seek(max(0, size - LOG_TAIL_BYTES), 0)
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def _drop_log(self, name):
        try:
            os.unlink(self._log_path(name))
        except OSError:
            pass

    def _processes(self):
        return list_processes(self._proc_root, self._comms)

    def _find_process(self, name):
        for proc in self._processes():
            if parse_openvpn_args(proc.args)["daemon"] == name:
                return proc
        return None

    def _process_by_pid(self, pid):
        for proc in self._processes():
            if proc.pid == pid:
                return proc
        return None

    def _owner(self, pid):
        try:
            import pwd
            return pwd.getpwuid(os.stat(os.path.join(self._proc_root, str(pid))).st_uid).pw_name
        except (ImportError, KeyError, OSError):
            return ""

    # -------------------------------------------------------- sessions
    def _session(self, proc):
        opts = parse_openvpn_args(proc.args)
        name = opts["daemon"] or (
            Path(opts["config"]).stem if opts["config"] else "openvpn-pid-%d" % proc.pid)
        session = Session(path="pid:%d" % proc.pid, config_name=name, pid=proc.pid,
                          owner=self._owner(proc.pid), backend=self.key)
        ours = name.startswith(CONFIG_PREFIX) and opts["log"] == self._log_path(name)
        if ours:
            info = parse_log(self._read_log(opts["log"]))
            session.status = _STATUS_TEXT[info.state]
            session.device = info.device
            session.connected_to = info.connected_to
            session.created = info.created
        else:
            session.status = UNKNOWN_STATUS
            session.device = opts["dev"] if opts["dev"] and not opts["dev"].lower() in (
                "tun", "tap") else ""
        return session

    def list_sessions(self):
        return [self._session(p) for p in self._processes()]

    # --------------------------------------------------------- configs
    def prepare_config(self, profile_file, name, compression, auth=None):
        if auth == "prompt":
            raise BackendError(
                "this profile asks for a username and password, which the OpenVPN 2 "
                "backend does not support yet",
                hints=["Use OpenVPN 3 for this profile (backend = openvpn3).",
                       "Or put the credentials in a private file (chmod 600) and "
                       "reference it with 'auth-user-pass <file>' in a copy of the profile."])
        path = os.path.abspath(str(profile_file))
        if not os.path.isfile(path):
            raise BackendError("profile file not found: %s" % path)
        if self._find_process(name):
            raise BackendError("a session named %s is already running" % name)
        handle = HANDLE_PREFIX + name
        self._pending[handle] = _Job(path, name, compression, os.path.dirname(path))
        return handle

    def remove_config(self, name=None, path=None):
        if path and path.startswith(HANDLE_PREFIX):
            name = path[len(HANDLE_PREFIX):]
            self._pending.pop(path, None)
        if name and name.startswith(CONFIG_PREFIX):
            self._drop_log(name)

    def cleanup_session(self, session):
        if session.managed:
            self.remove_config(name=session.config_name)

    def list_managed_configs(self):
        try:
            names = [p.stem for p in self.dir.glob("*.log")]
        except OSError:
            return []
        return sorted(n for n in names if n.startswith(CONFIG_PREFIX))

    # ------------------------------------------------------ connection
    def start(self, config_path, timeout, interactive=False):
        job = self._pending.pop(config_path, None)
        if job is None:
            raise BackendError("internal error: the profile was not prepared")
        self._ensure_privileges()
        log_path = self._fresh_log(job.name)

        cmd = self._sudo + [self.command, "--config", job.file, "--cd", job.cwd,
                            "--daemon", job.name, "--log", log_path, "--verb", "3"]
        if job.compression != "none" and supports_allow_compression(self.version):
            cmd += ["--allow-compression", job.compression]

        result = self._launch(cmd, timeout=30)
        if result is None:
            self._drop_log(job.name)
            raise BackendError("could not run '%s'" % self.command)
        rc, out = result
        if rc != 0:
            text = out + "\n" + self._read_log(log_path)
            self._drop_log(job.name)
            raise BackendError("openvpn failed to start", output=text)
        return self._wait_connected(job.name, log_path, timeout)

    def _wait_connected(self, name, log_path, timeout):
        for _ in range(max(1, int(timeout / POLL))):
            proc = self._find_process(name)
            text = self._read_log(log_path)
            info = parse_log(text)
            if proc is not None and info.state == "connected":
                return self._session(proc)
            if info.state == "failed" or proc is None:
                self._abort(name, proc, log_path)
                raise BackendError(info.error or "the connection failed", output=text)
            self._sleep(POLL)
        text = self._read_log(log_path)
        self._abort(name, self._find_process(name), log_path)
        raise BackendError("the connection did not come up within %d seconds" % timeout,
                           output=text)

    def _abort(self, name, proc, log_path):
        """Stop a half-started session and discard its log (it may hold tokens)."""
        if proc is not None:
            try:
                self._call(self._sudo + ["kill", "-s", "TERM", str(proc.pid)])
            except BackendError:
                pass
        self._drop_log(name)

    def disconnect(self, session):
        pid = session.pid
        if not pid or pid <= 1:
            raise BackendError("the session has no valid process id")
        if self._process_by_pid(pid) is None:
            return                              # already gone
        self._ensure_privileges()
        rc, out = self._call(self._sudo + ["kill", "-s", "TERM", str(pid)])
        if rc != 0 and self._process_by_pid(pid) is not None:
            raise BackendError("could not stop OpenVPN (pid %d)" % pid, output=out)
        for _ in range(20):
            if self._process_by_pid(pid) is None:
                return
            self._sleep(POLL)
        raise BackendError("OpenVPN (pid %d) did not exit after SIGTERM" % pid,
                           hints=["Force it with: sudo kill -9 %d" % pid])
