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
from . import completion
from . import shortcuts
from . import wizard
from .backends import select_backend
from .backends.base import BackendError, failure_hints, make_config_name, split_config_name
from .diagnostics import (DEFAULT_PORTS, FAIL, INFO, PASS, WARN, Probe,
                          check_target, run_diagnostics, summarize)
from .env import detect_environment, running_in_wsl
from .log import Console, clean as clean_line, redact, setup_logging
from .profiles import ProfileError, find_profile, scan_platform
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
        con.info("Create one with: ovpnx setup")
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
        return select_backend(preference, env, cfg.state_dir()), env
    except BackendError as exc:
        _report(con, exc)
        return None, env


def _backend_version(backend, env):
    info = env.backends.get(backend.key)
    return " %s" % info.version if info and info.version else ""


def _identify(session, settings, state):
    """Return (platform key or None, profile name or config name)."""
    if state and state.get("session_path") == session.path and state.get("platform"):
        return state.get("platform"), state.get("profile")
    if session.managed:
        keys = list(settings.platforms) if settings else []
        split = split_config_name(session.config_name, keys)
        if split:
            return split
    return None, session.config_name


_LABELS = {"openvpn3": "OpenVPN 3", "openvpn2": "OpenVPN 2"}


def _session_backend_label(session, backend, env):
    key = session.backend or backend.key
    info = env.backends.get(key)
    version = " %s" % info.version if info and info.version else ""
    return "%s%s" % (_LABELS.get(key, key), version)


def _describe(session, settings, state):
    """Return (platform display name or None, profile or config name)."""
    plat_key, profile = _identify(session, settings, state)
    if plat_key:
        if settings and plat_key in settings.platforms:
            return settings.platforms[plat_key].display_name, profile
        return plat_key, profile
    return None, profile


def _pick_session(sessions, state):
    """The session to examine: the one in state, else a managed one, else any."""
    if state:
        for s in sessions:
            if s.path == state.get("session_path"):
                return s
    for s in sessions:
        if s.managed:
            return s
    return sessions[0]


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
    else:
        settings = _settings_quiet(args)
        preference = settings.backend if settings else "auto"
        try:
            chosen = select_backend(preference, env, cfg.state_dir())
            con.ok("Backend ovpnx will use (backend = %s): %s" % (preference, chosen.label))
        except BackendError as exc:
            con.warn("No usable backend (backend = %s): %s" % (preference, exc))
            for hint in exc.hints:
                con.plain("      - %s" % hint)
    return EXIT_ERROR if problems else EXIT_OK


def cmd_setup(args, con):
    if not sys.stdin.isatty():
        con.err_("'ovpnx setup' asks questions, so it needs an interactive terminal.")
        con.info("For a template to edit by hand: ovpnx config --init")
        return EXIT_ERROR
    path = cfg.config_path(args.config)
    home = os.environ.get("HOME") or str(Path.home())
    keys = wizard.run_setup(con, path, home, wsl=running_in_wsl())
    if not keys:
        return EXIT_ERROR
    con.plain()
    con.info("Check it with: ovpnx list")
    try:
        answer = input("Create the shortcut commands (%s) now? [Y/n]: "
                       % ", ".join(shortcuts.names_for(keys))).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
        con.plain()
    if answer.startswith("n"):
        con.info("Later, run: ovpnx install-shortcuts")
    else:
        code = cmd_install_shortcuts(argparse.Namespace(config=args.config, dir=None,
                                                        remove=False), con)
        if code:
            return code
    try:
        answer = input("Enable TAB completion (bash/zsh) for these commands? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
        con.plain()
    if answer.startswith("n"):
        con.info("Later, run: ovpnx install-completion")
        return EXIT_OK
    return cmd_install_completion(argparse.Namespace(config=args.config, shell=None,
                                                     remove=False, yes=True), con)


def cmd_config(args, con):
    path = cfg.config_path(args.config)
    if args.path:
        con.plain(str(path))
        return EXIT_OK

    if args.init:
        if sys.stdin.isatty() and not path.exists() and not args.template:
            return cmd_setup(args, con)
        try:
            cfg.write_default_config(path, force=args.force)
        except FileExistsError:
            con.err_("Config already exists: %s" % path)
            con.info("Use --force to overwrite it, or 'ovpnx setup' to add platforms.")
            return EXIT_ERROR
        except OSError as exc:
            con.err_("Could not write %s: %s" % (path, exc.strerror or exc))
            return EXIT_ERROR
        con.ok("Created %s (a template with no platforms yet)" % path)
        con.info("Edit it, or run the guided setup instead: ovpnx setup")
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
        if plat.expected_routes:
            con.plain("      expected_routes: %s" % ", ".join(plat.expected_routes))
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
        config_path = backend.prepare_config(profile.path, name, plat.compression,
                                             profile.auth)
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
                backend.cleanup_session(session)
            except BackendError as exc:
                con.warn("Could not clean up %s: %s" % (session.config_name, exc))
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
        con.ok("OpenVPN backend: %s" % _session_backend_label(session, backend, env))
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


def cmd_install_shortcuts(args, con):
    settings = _load(args, con)
    if settings is None:
        return EXIT_ERROR
    directory = Path(os.path.expanduser(args.dir)) if args.dir else shortcuts.default_dir()
    names = shortcuts.names_for(settings.platforms)

    if args.remove:
        removed, skipped = shortcuts.remove(directory, names)
        for name in removed:
            con.ok("Removed %s" % (directory / name))
        for name in skipped:
            con.warn("Kept %s (not created by ovpnx)" % (directory / name))
        if not removed and not skipped:
            con.info("No shortcuts found in %s" % directory)
        return EXIT_OK

    try:
        created, updated, skipped = shortcuts.install(directory, names)
    except OSError as exc:
        con.err_("Could not write to %s: %s" % (directory, exc.strerror or exc))
        return EXIT_ERROR
    for name in created:
        con.ok("Created %s" % (directory / name))
    for name in updated:
        con.ok("Updated %s" % (directory / name))
    for name in skipped:
        con.warn("Skipped %s: a file with that name exists and was not created by ovpnx"
                 % (directory / name))
    refreshed = completion.refresh_existing(completion.names_for(settings.platforms))
    if refreshed:
        con.ok("Updated TAB completion (%s)" % ", ".join(refreshed))
    if (created or updated) and not shortcuts.on_path(directory):
        con.warn("%s is not on your PATH." % directory)
        con.plain('      Add it with:  echo \'export PATH="%s:$PATH"\' >> ~/.bashrc' % directory)
        con.plain("      then open a new terminal (zsh users: ~/.zshrc).")
    if created or updated:
        first = sorted(settings.platforms)[0]
        con.info("Try: %s-start   (and %s-stop to disconnect)" % (first, first))
    return EXIT_ERROR if skipped and not (created or updated) else EXIT_OK


def _platform_keys_quiet(args):
    settings = _settings_quiet(args)
    return list(settings.platforms) if settings else []


def cmd_install_completion(args, con):
    shell = args.shell or completion.detect_shell()
    if shell not in completion.SHELLS:
        con.err_("Could not tell which shell you use (supported: bash, zsh).")
        con.info("Pick one with: ovpnx install-completion --shell bash|zsh")
        return EXIT_ERROR
    script = completion.script_path(shell)
    rc = completion.rc_path(shell)

    if args.remove:
        removed = False
        if script.exists():
            script.unlink()
            con.ok("Removed %s" % script)
            removed = True
        if completion.rc_remove(rc):
            con.ok("Removed the ovpnx lines from %s" % rc)
            removed = True
        if not removed:
            con.info("No %s completion was installed." % shell)
        return EXIT_OK

    names = completion.names_for(_platform_keys_quiet(args))
    try:
        completion.write_script(shell, names)
    except OSError as exc:
        con.err_("Could not write %s: %s" % (script, exc.strerror or exc))
        return EXIT_ERROR
    con.ok("Wrote %s completion for: %s" % (shell, ", ".join(names)))

    if completion.rc_has_block(rc):
        con.ok("%s already loads it." % rc)
    else:
        add = args.yes
        if not add and sys.stdin.isatty():
            try:
                add = input("Add a line to %s so it loads in every new terminal? [Y/n]: "
                            % rc).strip().lower() != "n"
            except (EOFError, KeyboardInterrupt):
                add = False
                con.plain()
        if add:
            try:
                completion.rc_add(rc, script)
            except OSError as exc:
                con.err_("Could not edit %s: %s" % (rc, exc.strerror or exc))
                return EXIT_ERROR
            con.ok("Added it to %s (between '# >>> ovpnx completion >>>' markers)" % rc)
        else:
            con.info("To enable it, add this line to %s:" % rc)
            con.plain("      . %s" % script)
    con.info("Open a new terminal (or run: . %s), then try: %s-start <TAB>"
             % (script, (_platform_keys_quiet(args) or ["name"])[0]))
    return EXIT_OK


def cmd__complete(args, con):
    """Hidden helper used by the shell completion scripts."""
    words = list(args.words)
    prog = words[0] if words else "ovpnx"
    typed = words[1:]
    expanded = expand_shortcut(prog, typed)
    if expanded is not None:
        typed = expanded

    def load(config):
        try:
            return cfg.load_config(cfg.config_path(config))
        except cfg.ConfigError:
            return None

    def profiles(plat):
        return [p.name for p in scan_platform(plat).profiles]

    try:
        out = completion.complete(typed, build_parser(), load, profiles)
    except Exception:           # completion must never print errors into the prompt
        out = []
    for line in out:
        print(clean_line(line))
    return EXIT_OK


_PRINTERS = {PASS: "ok", INFO: "info", WARN: "warn", FAIL: "err_"}


def _print_checks(con, checks):
    for check in checks:
        getattr(con, _PRINTERS[check.level])(check.message)
        for hint in check.hints:
            con.plain("      - %s" % hint)


def cmd_diagnose(args, con):
    settings = _settings_quiet(args)
    backend, env = _get_backend(settings.backend if settings else "auto", con)
    if backend is None:
        return EXIT_ERROR
    try:
        sessions = backend.list_sessions()
    except BackendError as exc:
        _report(con, exc)
        return EXIT_ERROR
    if not sessions:
        con.err_("No active VPN session, so there is nothing to diagnose.")
        con.info("Connect first, e.g.: ovpnx connect <platform> <profile>")
        return EXIT_ERROR

    state = load_state(cfg.state_dir())
    session = _pick_session(sessions, state)
    plat_key, profile = _identify(session, settings, state)
    disp, _ = _describe(session, settings, state)
    label = "%s / %s" % (disp, profile) if disp else profile
    if len(sessions) > 1:
        con.warn("%d sessions are active; checking %s" % (len(sessions), label))

    expected = []
    if settings and plat_key in settings.platforms:
        expected = settings.platforms[plat_key].expected_routes

    probe = Probe()
    checks = run_diagnostics(session, label, probe, expected)
    _print_checks(con, checks)
    fails, warns = summarize(checks)
    device_ok = not any(c.name == "interface" and c.level == FAIL for c in checks)

    con.plain()
    if fails:
        con.err_("%d problem%s found with the VPN connection." % (fails, "" if fails == 1 else "s"))
    elif warns:
        con.warn("The VPN connection works, with %d warning%s." % (warns, "" if warns == 1 else "s"))
    else:
        con.info("The VPN connection looks healthy.")

    if args.target:
        con.plain()
        con.info("Target: %s" % args.target)
        if not device_ok:
            con.warn("Skipping the target test: the VPN interface is not usable.")
        else:
            target_checks = check_target(args.target, session.device, probe,
                                         args.ports or DEFAULT_PORTS)
            _print_checks(con, target_checks)
            fails += summarize(target_checks)[0]
    else:
        if args.ports:
            con.warn("--ports only applies together with a target.")
        con.info("A working VPN does not guarantee that every machine is reachable. "
                 "Test one with: ovpnx diagnose <target-ip>")

    if env.is_wsl:
        con.info("WSL2: only this Linux distribution goes through the VPN; Windows "
                 "applications and browsers do not.")
    return EXIT_ERROR if fails else EXIT_OK


# ------------------------------------------------------------ parser/main
def _ports_arg(value):
    try:
        ports = [int(p) for p in value.split(",") if p.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError("ports must be numbers separated by commas")
    if not ports or any(not 1 <= p <= 65535 for p in ports) or len(ports) > 32:
        raise argparse.ArgumentTypeError("give 1-32 ports between 1 and 65535")
    return ports


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
    s.add_argument("--init", action="store_true",
                   help="create the config (guided on a terminal, else a template)")
    s.add_argument("--template", action="store_true",
                   help="with --init: write the template without questions")
    s.add_argument("--force", action="store_true", help="with --init: overwrite")
    s.add_argument("--path", action="store_true", help="print the config file path")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("setup", help="guided setup: find .ovpn files and choose platforms")
    s.set_defaults(func=cmd_setup)

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

    s = sub.add_parser("diagnose", help="check the VPN connection and optionally a target")
    s.add_argument("target", nargs="?", help="machine IP or hostname to test through the VPN")
    s.add_argument("--ports", type=_ports_arg, metavar="P1,P2",
                   help="TCP ports to probe on the target (default: %s)"
                   % ",".join(str(p) for p in DEFAULT_PORTS))
    s.set_defaults(func=cmd_diagnose)

    s = sub.add_parser("install-shortcuts",
                       help="create <platform>-start / <platform>-stop commands")
    s.add_argument("--dir", metavar="DIR", help="where to put them (default: ~/.local/bin)")
    s.add_argument("--remove", action="store_true", help="remove the shortcuts instead")
    s.set_defaults(func=cmd_install_shortcuts)

    s = sub.add_parser("install-completion",
                       help="enable TAB completion for ovpnx and the shortcut commands")
    s.add_argument("--shell", choices=list(completion.SHELLS),
                   help="bash or zsh (default: detected from $SHELL)")
    s.add_argument("--remove", action="store_true", help="remove the completion again")
    s.add_argument("--yes", action="store_true", help="edit the shell rc file without asking")
    s.set_defaults(func=cmd_install_completion)

    s = sub.add_parser("_complete")    # no help text: keeps it out of --help
    s.add_argument("words", nargs="*")
    s.set_defaults(func=cmd__complete)

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
    if args.command == "_complete":        # runs on every TAB: stay quiet and fast
        return args.func(args, con)
    setup_logging(cfg.state_dir(), args.verbose)
    log.debug("ovpnx %s, command=%s", __version__, args.command)
    try:
        return args.func(args, con)
    except KeyboardInterrupt:
        con.warn("Interrupted.")
        return 130
