"""Command line interface for ovpnx."""
import argparse
import logging
import os
import re
import sys
from pathlib import Path

from . import __version__
from . import config as cfg
from .env import detect_environment, running_in_wsl
from .log import Console, setup_logging
from .profiles import ProfileError, find_candidate_dirs, scan_platform  # noqa: F401

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_TODO = 0, 1, 2, 3

log = logging.getLogger("ovpnx")
_SHORTCUT_RE = re.compile(r"^([a-z0-9][a-z0-9_-]*)-(start|stop)$")


def expand_shortcut(prog, args):
    """Map 'htb-start eu' to ['connect','htb','eu'] and 'htb-stop' to ['disconnect'].

    Returns None when the program name is not a shortcut.
    """
    name = os.path.basename(prog)
    if name.endswith(".py"):
        name = name[:-3]
    match = _SHORTCUT_RE.match(name)
    if not match:
        return None
    platform, action = match.groups()
    if action == "start":
        return ["connect", platform] + list(args)
    return ["disconnect"] + list(args)


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


def _todo(con, what, step):
    con.warn("'%s' is not implemented yet (roadmap step %d)." % (what, step))
    return EXIT_TODO


# ------------------------------------------------------------ commands
def cmd_env(args, con):
    env = detect_environment()
    log.debug("environment: %s", env)
    problems = 0

    where = "%s (%s)" % (env.kind, env.distro)
    con.ok("System: %s" % where)
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
    return _todo(con, "connect", 2)


def cmd_disconnect(args, con):
    return _todo(con, "disconnect", 2)


def cmd_status(args, con):
    return _todo(con, "status", 2)


def cmd_diagnose(args, con):
    return _todo(con, "diagnose", 4)


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

    s = sub.add_parser("connect", help="connect to a profile (coming soon)")
    s.add_argument("platform")
    s.add_argument("profile", nargs="?")
    s.set_defaults(func=cmd_connect)

    for name, func, text in (
        ("disconnect", cmd_disconnect, "disconnect the active VPN (coming soon)"),
        ("status", cmd_status, "show connection status (coming soon)"),
        ("diagnose", cmd_diagnose, "run connectivity diagnostics (coming soon)"),
    ):
        s = sub.add_parser(name, help=text)
        s.set_defaults(func=func)
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
