"""Command line interface for ovpnx."""
import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path

from . import __version__
from . import config as cfg
from . import netinfo
from .backends import select_backend
from .backends.base import BackendError, failure_hints, make_config_name, split_config_name
from .env import detect_environment, running_in_wsl
from .log import Console, redact, setup_logging
from .profiles import ProfileError, find_candidate_dirs, find_profile, scan_platform
from .state import clear_state, load_state, save_state

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_TODO = 0, 1, 2, 3

log = logging.getLogger("ovpnx")
_SHORTCUT_RE = re.compile(r"^([a-z0-9][a-z0-9_-]*)-(start|stop)$")


def _split_global_options(args):
    """Split leading global options (--config FILE, -v) from the rest."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--config" and i + 1 < len(args):
            i += 2
        elif arg.startswith("--config=") or arg in ("-v", "--verbose"):
            i += 1
        else:
            break
    return list(args[:i]), list(args[i:])


def expand_shortcut(prog, args):
    """Map 'htb-start eu' to ['connect','htb','eu'] and 'htb-stop' to ['disconnect'].

    Global options such as --config or -v stay in front of the sub-command, so
    'htb-start -v eu' works. Returns None when prog is not a shortcut name.
    """
    name = os.path.basename(prog)
    if name.endswith(".py"):
        name = name[:-3]
    match = _SHORTCUT_RE.match(name)
    if not match:
        return None
    platform, action = match.groups()
    head, rest = _split_global_options(list(args))
    if action == "start":
        return head + ["connect", platform] + rest
    return head + ["disconnect"] + rest


# ------------------------------------------------------------ helpers
def _load(args, con):
    path = cfg.config_path(args.config)
    try:
        settings = cfg.load_config(path)
    except cfg.ConfigNotFound:
        con.err_("No config file at %s" % path)
        con.info("Create one with: ovpnx config --init")
        return None
    except cfg.ConfigError as exc:
        con.err_("Invalid config: %s" % path)
        for problem in exc.problems:
            con.plain("    - %s" % problem)
        return None
    for warning in settings.warnings:
        con.warn(warning)
    return settings


def _settings_quiet(args):
    """Config for commands that also work without one (status, disconnect)."""
    try:
        return cfg.load_config(cfg.config_path(args.config))
    except cfg.ConfigError:
        return None


def _report(con, exc, extra_hints=()):
    con.err_(str(exc))
    for hint in list(exc.hints) + list(extra_hints):
        con.info(hint)
    if exc.output:
        lines = [ln for ln in exc.output.splitlines() if ln.strip()][-8:]
        for line in lines:
            con.plain("    | %s" % redact(line))


def _get_backend(preference, con):
    env = detect_environment()
    try:
        return select_backend(preference, env), env
    except BackendError as exc:
        _report(con, exc)
        return None, env


def _backend_version(backend, env):
    info = env.backends.get(backend.key)
    return " %s" % info.version if info and info.version else ""


def _describe(session, settings, state):
    """Return (platform display name or None, profile or config name)."""
    plat_key = profile = None
    if state and state.get("session_path") == session.path:
        plat_key, profile = state.get("platform"), state.get("profile")
    elif session.managed:
        keys = list(settings.platforms) if settings else []
        split = split_config_name(session.config_name, keys)
        if split:
            plat_key, profile = split
    if plat_key:
        if settings and plat_key in settings.platforms:
            return settings.platforms[plat_key].display_name, profile
        return plat_key, profile
    return None, session.config_name


def _wait_for_ip(device, tries=5):
    for attempt in range(tries):
        ip = netinfo.interface_ipv4(device)
        if ip or attempt == tries - 1:
            return ip
        time.sleep(1)
    return None


def _teardown(backend, name):
    """Best effort: end any session using config `name`, then remove the config."""
    try:
        for session in backend.list_sessions():
            if session.config_name == name:
                backend.disconnect(session)
    except BackendError as exc:
        log.debug("teardown: %s", exc)
    try:
        backend.remove_config(name=name)
    except BackendError as exc:
        log.debug("teardown: %s", exc)


# ------------------------------------------------------------ commands
def cmd_env(args, con):
    env = detect_environment()
    log.debug("environment: %s", env)
    problems = 0

    con.ok("System: %s (%s)" % (env.kind, env.distro))
    if not env.is_linux:
        con.warn("ovpnx only supports Linux (including WSL2).")
        problems += 1
    if env.is_wsl and env.wsl_version == 1:
        con.warn("WSL1 has no TUN device, so VPNs cannot work. Upgrade to WSL2.")
        problems += 1
    if env.is_wsl and env.wsl_networking:
        con.ok("WSL networking mode: %s" % env.wsl_networking)

    if env.systemd:
        con.ok("systemd: running")
    else:
        con.warn("systemd: not running (OpenVPN 3 usually needs it).")
        if env.is_wsl:
            con.plain("      Enable it in /etc/wsl.conf:  [boot]  systemd=true")
            con.plain("      then run 'wsl --shutdown' from Windows.")

    who = "root" if env.is_root else "regular user"
    con.ok("Privileges: %s, sudo %s" % (who, "available" if env.has_sudo else "not found"))
    con.ok("Python: %s" % env.python)

    if env.has_ip:
        con.ok("'ip' command: found")
    else:
        con.warn("'ip' command not found (install iproute2); diagnostics will be limited.")

    for key, label in (("openvpn3", "OpenVPN 3"), ("openvpn2", "OpenVPN 2")):
        backend = env.backends.get(key)
        if backend:
            con.ok("%s: %s (%s)" % (label, backend.version or "version unknown", backend.path))
        else:
            con.warn("%s: not installed" % label)
    if not env.backends:
        con.err_("No OpenVPN found. Install one, e.g.: sudo apt install openvpn")
        problems += 1
    return EXIT_ERROR if problems else EXIT_OK


def cmd_config(args, con):
    path = cfg.config_path(args.config)
    if args.path:
        con.plain(str(path))
        return EXIT_OK

    if args.init:
        try:
            cfg.write_default_config(path, force=args.force)
        except FileExistsError:
            con.err_("Config already exists: %s" % path)
            con.info("Use --force to overwrite it.")
            return EXIT_ERROR
        except OSError as exc:
            con.err_("Could not write %s: %s" % (path, exc.strerror or exc))
            return EXIT_ERROR
        con.ok("Created %s" % path)
        home = os.environ.get("HOME") or str(Path.home())
        found = find_candidate_dirs(home, wsl=running_in_wsl())
        if found:
            con.info("Folders that already contain .ovpn files (use as profile_dir):")
            for directory, count in found:
                con.plain("      %s  (%d file%s)" % (directory, count, "" if count == 1 else "s"))
        con.info("Edit the file, then run: ovpnx list")
        return EXIT_OK

    con.info("Config file: %s" % path)
    con.info("State dir:   %s" % cfg.state_dir())
    settings = _load(args, con)
    if settings is None:
        return EXIT_ERROR
    con.ok("backend = %s" % settings.backend)
    con.ok("allow_multiple = %s" % str(settings.allow_multiple).lower())
    con.ok("connect_timeout = %d" % settings.connect_timeout)
    for plat in settings.platforms.values():
        con.ok("platform %s (%s)" % (plat.key, plat.display_name))
        con.plain("      profile_dir:     %s" % plat.profile_dir)
        con.plain("      compression:     %s" % plat.compression)
        con.plain("      default_profile: %s" % (plat.default_profile or "-"))
    return EXIT_OK


def cmd_list(args, con):
    settings = _load(args, con)
    if settings is None:
        return EXIT_ERROR
    selected = list(settings.platforms.values())
    if args.platform:
        key = args.platform.lower()
        if key not in settings.platforms:
            con.err_("Unknown platform '%s'. Configured: %s"
                     % (args.platform, ", ".join(settings.platforms)))
            return EXIT_ERROR
        selected = [settings.platforms[key]]

    failed = False
    for plat in selected:
        scan = scan_platform(plat)
        log.debug("scanned %s: %d profiles, error=%s", plat.key, len(scan.profiles), scan.error)
        con.info("%s (%s) - %s" % (plat.display_name, plat.key, plat.profile_dir))
        if scan.error:
            con.warn(scan.error)
            failed = True
        elif not scan.profiles:
            con.warn("no .ovpn files found")
        for prof in scan.profiles:
            tags = []
            if plat.default_profile and prof.name.lower() == plat.default_profile.lower():
                tags.append("default")
            if prof.auth == "prompt":
                tags.append("asks for login")
            elif prof.auth == "file":
                tags.append("credentials file")
            elif prof.auth == "inline":
                tags.append("inline credentials")
            suffix = "  (%s)" % ", ".join(tags) if tags else ""
            con.plain("    %s%s" % (prof.name, suffix))
            if args.paths:
                con.plain("      %s" % prof.path)
        con.plain()
    return EXIT_ERROR if failed else EXIT_OK


def cmd_connect(args, con):
    settings = _load(args, con)
    if settings is None:
        return EXIT_ERROR
    key = args.platform.lower()
    if key not in settings.platforms:
        con.err_("Unknown platform '%s'. Configured: %s"
                 % (args.platform, ", ".join(settings.platforms)))
        return EXIT_ERROR
    plat = settings.platforms[key]

    scan = scan_platform(plat)
    if scan.error:
        con.err_(scan.error)
        return EXIT_ERROR
    try:
        profile = find_profile(plat, scan.profiles, args.profile)
    except ProfileError as exc:
        con.err_(str(exc))
        return EXIT_ERROR

    con.ok("Platform: %s" % plat.display_name)
    con.ok("Profile: %s" % profile.name)
    backend, env = _get_backend(settings.backend, con)
    if backend is None:
        return EXIT_ERROR
    con.ok("OpenVPN backend: %s%s" % (backend.label, _backend_version(backend, env)))

    name = make_config_name(plat.key, profile.name)
    try:
        sessions = backend.list_sessions()
    except BackendError as exc:
        _report(con, exc)
        return EXIT_ERROR

    for session in sessions:
        if session.config_name == name and session.connected:
            con.ok("Already connected to this profile.")
            con.ok("Interface: %s" % (session.device or "unknown"))
            return EXIT_OK
    if sessions and not settings.allow_multiple:
        con.err_("A VPN session is already active:")
        state = load_state(cfg.state_dir())
        for s in sessions:
            disp, prof = _describe(s, settings, state)
            what = "%s / %s" % (disp, prof) if disp else prof
            con.plain("    %s on %s (%s)" % (what, s.device or "?", s.status or "?"))
        con.info("Run 'ovpnx disconnect' first (add --all for sessions not started by "
                 "ovpnx), or set allow_multiple = true.")
        return EXIT_ERROR

    if plat.compression == "yes":
        con.warn("compression = yes weakens security; 'asym' is usually enough.")
    elif plat.compression == "asym":
        con.info("Compression policy: asym (receive-only)")

    con.info("Importing profile...")
    try:
        config_path = backend.prepare_config(profile.path, name, plat.compression)
    except BackendError as exc:
        _report(con, exc)
        return EXIT_ERROR

    con.info("Starting connection...")
    try:
        session = backend.start(
            config_path, settings.connect_timeout, interactive=(profile.auth == "prompt")
        )
    except KeyboardInterrupt:
        con.warn("Interrupted, cleaning up...")
        _teardown(backend, name)
        return 130
    except BackendError as exc:
        _teardown(backend, name)
        _report(con, exc, failure_hints(exc.output, plat.key, plat.compression))
        return EXIT_ERROR

    save_state(cfg.state_dir(), {
        "backend": backend.key, "platform": plat.key, "profile": profile.name,
        "config_name": name, "session_path": session.path,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    con.plain()
    con.ok("VPN connected")
    con.ok("Interface: %s" % (session.device or "unknown"))
    ip = _wait_for_ip(session.device)
    if ip:
        con.ok("VPN IP: %s" % ip)
    else:
        con.warn("No VPN IP found on %s yet (check with 'ip addr show %s')."
                 % (session.device or "the tunnel", session.device or ""))
    if session.connected_to:
        con.ok("Server: %s" % session.connected_to)
    con.plain()
    con.info("Connection established successfully.")
    return EXIT_OK


def cmd_disconnect(args, con):
    settings = _settings_quiet(args)
    backend, env = _get_backend(settings.backend if settings else "auto", con)
    if backend is None:
        return EXIT_ERROR
    try:
        sessions = backend.list_sessions()
    except BackendError as exc:
        _report(con, exc)
        return EXIT_ERROR

    state = load_state(cfg.state_dir())
    managed = [s for s in sessions if s.managed]
    targets = sessions if args.all else managed
    if not targets:
        if sessions:
            con.warn("No session started by ovpnx. Found %d started elsewhere:" % len(sessions))
            for s in sessions:
                con.plain("    %s on %s" % (s.config_name or s.path, s.device or "?"))
            con.info("Use 'ovpnx disconnect --all' to disconnect them too.")
            return EXIT_ERROR
        clear_state(cfg.state_dir())
        con.info("No active VPN session.")
        return EXIT_OK

    failed = False
    for session in targets:
        disp, prof = _describe(session, settings, state)
        label = "%s / %s" % (disp, prof) if disp else prof
        try:
            backend.disconnect(session)
        except BackendError as exc:
            _report(con, exc)
            failed = True
            continue
        con.ok("Disconnected: %s" % label)
        if session.managed:
            try:
                backend.remove_config(name=session.config_name)
            except BackendError as exc:
                con.warn("Could not remove imported config %s: %s"
                         % (session.config_name, exc))
    if not failed:
        clear_state(cfg.state_dir())
    return EXIT_ERROR if failed else EXIT_OK


def cmd_status(args, con):
    settings = _settings_quiet(args)
    backend, env = _get_backend(settings.backend if settings else "auto", con)
    if backend is None:
        return EXIT_ERROR
    try:
        sessions = backend.list_sessions()
    except BackendError as exc:
        _report(con, exc)
        return EXIT_ERROR

    state = load_state(cfg.state_dir())
    if not sessions:
        if state:
            log.debug("clearing stale state")
            clear_state(cfg.state_dir())
        con.info("Not connected.")
        return EXIT_ERROR   # exit status 0 means "connected", handy in scripts

    for i, session in enumerate(sessions):
        if i:
            con.plain()
        disp, prof = _describe(session, settings, state)
        if disp:
            con.ok("Platform: %s" % disp)
        elif not session.managed:
            con.warn("This session was not started by ovpnx.")
        con.ok("Profile: %s" % prof)
        con.ok("OpenVPN backend: %s%s" % (backend.label, _backend_version(backend, env)))
        (con.ok if session.connected else con.warn)("Status: %s" % (session.status or "unknown"))
        con.ok("Interface: %s" % (session.device or "unknown"))
        ip = netinfo.interface_ipv4(session.device)
        if ip:
            con.ok("VPN IP: %s" % ip)
        if session.connected_to:
            con.ok("Server: %s" % session.connected_to)
        if session.created:
            con.ok("Started: %s" % session.created)
    return EXIT_OK


def cmd_clean(args, con):
    settings = _settings_quiet(args)
    backend, env = _get_backend(settings.backend if settings else "auto", con)
    if backend is None:
        return EXIT_ERROR
    try:
        in_use = {s.config_name for s in backend.list_sessions()}
        names = backend.list_managed_configs()
    except BackendError as exc:
        _report(con, exc)
        return EXIT_ERROR
    removed = 0
    failed = False
    for name in names:
        if name in in_use:
            con.info("Keeping %s (session active)" % name)
            continue
        try:
            backend.remove_config(name=name)
            con.ok("Removed %s" % name)
            removed += 1
        except BackendError as exc:
            _report(con, exc)
            failed = True
    if not names:
        con.info("Nothing to clean.")
    elif removed:
        con.info("Removed %d imported config%s." % (removed, "" if removed == 1 else "s"))
    return EXIT_ERROR if failed else EXIT_OK


def cmd_diagnose(args, con):
    con.warn("'diagnose' is not implemented yet (roadmap step 4).")
    return EXIT_TODO


# ------------------------------------------------------------ parser/main
def build_parser():
    p = argparse.ArgumentParser(
        prog="ovpnx",
        description="Simple VPN manager for cybersecurity labs (HTB, THM, ...).",
    )
    p.add_argument("--version", action="version", version="ovpnx " + __version__)
    p.add_argument("-v", "--verbose", action="store_true",
                   help="also print debug log lines to stderr")
    p.add_argument("--config", metavar="FILE",
                   help="config file (default: $OVPNX_CONFIG or ~/.config/ovpnx/config.ini)")
    sub = p.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    s = sub.add_parser("list", help="list discovered profiles")
    s.add_argument("platform", nargs="?", help="only this platform")
    s.add_argument("--paths", action="store_true", help="also show file paths")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("config", help="show or create the config file")
    s.add_argument("--init", action="store_true", help="create a default config file")
    s.add_argument("--force", action="store_true", help="with --init: overwrite")
    s.add_argument("--path", action="store_true", help="print the config file path")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("env", help="show detected environment and OpenVPN backends")
    s.set_defaults(func=cmd_env)

    s = sub.add_parser("connect", help="connect to a profile")
    s.add_argument("platform")
    s.add_argument("profile", nargs="?", help="profile name (or unique prefix)")
    s.set_defaults(func=cmd_connect)

    s = sub.add_parser("disconnect", help="disconnect the active VPN")
    s.add_argument("--all", action="store_true",
                   help="also disconnect sessions not started by ovpnx")
    s.set_defaults(func=cmd_disconnect)

    s = sub.add_parser("status", help="show the connection (exit 0 = connected)")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("clean", help="remove configs imported by ovpnx that are not in use")
    s.set_defaults(func=cmd_clean)

    s = sub.add_parser("diagnose", help="run connectivity diagnostics (coming soon)")
    s.set_defaults(func=cmd_diagnose)
    return p


def main(argv=None, prog=None):
    prog = prog or os.path.basename(sys.argv[0])
    args_in = list(sys.argv[1:] if argv is None else argv)
    expanded = expand_shortcut(prog, args_in)
    if expanded is not None:
        args_in = expanded

    parser = build_parser()
    args = parser.parse_args(args_in)
    con = Console()
    setup_logging(cfg.state_dir(), args.verbose)
    log.debug("ovpnx %s, command=%s", __version__, args.command)
    try:
        return args.func(args, con)
    except KeyboardInterrupt:
        con.warn("Interrupted.")
        return 130
