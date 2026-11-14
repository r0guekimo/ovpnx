"""Config loading and validation (INI, standard library only)."""
import configparser
import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

APP = "ovpnx"
BACKENDS = ("auto", "openvpn3", "openvpn2")
COMPRESSION = ("none", "asym", "yes")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_GENERAL_KEYS = {"backend", "allow_multiple", "connect_timeout"}
_PLATFORM_KEYS = {"display_name", "profile_dir", "compression", "default_profile",
                  "expected_routes"}

DEFAULT_CONFIG = """\
; ovpnx config. For a guided setup run:  ovpnx setup
;
; Add one section per lab platform. NAME becomes the command prefix
; (NAME-start / NAME-stop). Remove the leading ';' to enable the example.
;
; [platform:mylab]
; display_name = My Lab
; profile_dir = ~/path/to/folder/with/ovpn/files
; default_profile = profile-name      ; optional
; compression = none                  ; none | asym | yes (see README)
;
; Optional global settings:
; [general]
; backend = auto                      ; auto | openvpn3 | openvpn2
; allow_multiple = false
; connect_timeout = 60
"""

HEADER = "; ovpnx config. Edit freely, check it with: ovpnx config\n"


def tilde(path, home=None):
    """Write a path with ~ when it lives under the home folder."""
    home = home or os.path.expanduser("~")
    path = str(path)
    if path == home:
        return "~"
    if path.startswith(home.rstrip("/") + "/"):
        return "~/" + path[len(home.rstrip("/")) + 1:]
    return path


def render_platform(key, display_name, profile_dir, default_profile=None,
                    compression="none", home=None):
    """Return the text of one [platform:KEY] section (only non-default values)."""
    lines = ["[platform:%s]" % key,
             "display_name = %s" % display_name,
             "profile_dir = %s" % tilde(profile_dir, home)]
    if default_profile:
        lines.append("default_profile = %s" % default_profile)
    if compression != "none":
        lines.append("compression = %s" % compression)
    return "\n".join(lines) + "\n"


class ConfigError(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class ConfigNotFound(ConfigError):
    pass


@dataclass
class Platform:
    key: str
    display_name: str
    profile_dir: Path
    compression: str = "none"
    default_profile: Optional[str] = None
    expected_routes: List[str] = field(default_factory=list)


@dataclass
class Settings:
    backend: str = "auto"
    allow_multiple: bool = False
    connect_timeout: int = 60
    platforms: Dict[str, Platform] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


# ------------------------------------------------------------------ paths
def _home(env):
    return env.get("HOME") or str(Path.home())


def _xdg(env, var, fallback):
    base = env.get(var)
    if base and os.path.isabs(base):
        return Path(base)
    return Path(_home(env)) / fallback


def config_dir(environ=None):
    env = os.environ if environ is None else environ
    return _xdg(env, "XDG_CONFIG_HOME", ".config") / APP


def state_dir(environ=None):
    env = os.environ if environ is None else environ
    return _xdg(env, "XDG_STATE_HOME", ".local/state") / APP


def config_path(override=None, environ=None):
    env = os.environ if environ is None else environ
    chosen = override or env.get("OVPNX_CONFIG")
    if chosen:
        return Path(os.path.expanduser(chosen))
    return config_dir(env) / "config.ini"


# ---------------------------------------------------------------- parsing
def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def parse_config(text, source="<config>"):
    parser = configparser.ConfigParser(
        interpolation=None, inline_comment_prefixes=(";",), delimiters=("=",)
    )
    try:
        parser.read_string(text, source)
    except configparser.Error as exc:
        raise ConfigError([" ".join(str(exc).split())])

    problems, warnings = [], []
    backend, allow_multiple, connect_timeout = "auto", False, 60

    if parser.has_section("general"):
        general = parser["general"]
        for key in general:
            if key not in _GENERAL_KEYS:
                warnings.append("[general] unknown option '%s' (ignored)" % key)
        backend = _unquote(general.get("backend", "auto")).lower()
        if backend not in BACKENDS:
            problems.append(
                "[general] backend must be one of: %s" % ", ".join(BACKENDS)
            )
        try:
            allow_multiple = general.getboolean("allow_multiple", fallback=False)
        except ValueError:
            problems.append("[general] allow_multiple must be true or false")
        raw_timeout = _unquote(general.get("connect_timeout", "60"))
        try:
            connect_timeout = int(raw_timeout)
            if not 5 <= connect_timeout <= 600:
                raise ValueError
        except ValueError:
            problems.append("[general] connect_timeout must be a number from 5 to 600")

    platforms = {}
    for section in parser.sections():
        if section == "general":
            continue
        if not section.lower().startswith("platform:"):
            warnings.append("unknown section [%s] (ignored)" % section)
            continue
        label = "[%s]" % section
        key = section.split(":", 1)[1].strip().lower()
        if not _NAME_RE.match(key):
            problems.append(
                "%s: platform name must be 1-32 characters: a-z, 0-9, '-' or '_'"
                % label
            )
            continue
        if key in platforms:
            problems.append("%s: platform '%s' is defined twice" % (label, key))
            continue

        opts = parser[section]
        for opt in opts:
            if opt not in _PLATFORM_KEYS:
                warnings.append("%s unknown option '%s' (ignored)" % (label, opt))

        raw_dir = _unquote(opts.get("profile_dir", ""))
        if not raw_dir:
            problems.append("%s: profile_dir is required" % label)
            continue
        expanded = os.path.expanduser(os.path.expandvars(raw_dir))
        if not os.path.isabs(expanded):
            problems.append(
                "%s: profile_dir must be an absolute path or start with ~" % label
            )
            continue

        compression = _unquote(opts.get("compression", "none")).lower()
        if compression not in COMPRESSION:
            problems.append(
                "%s: compression must be one of: %s" % (label, ", ".join(COMPRESSION))
            )
            continue

        default = _unquote(opts.get("default_profile", ""))
        if default.lower().endswith(".ovpn"):
            default = default[:-5]

        routes, bad_route = [], None
        for item in _unquote(opts.get("expected_routes", "")).replace(";", ",").split(","):
            item = item.strip()
            if not item:
                continue
            try:
                net = ipaddress.ip_network(item, strict=False)
                if net.version != 4:
                    raise ValueError
                routes.append(str(net))
            except ValueError:
                bad_route = item
                break
        if bad_route is not None:
            problems.append("%s: expected_routes has an invalid IPv4 network: %s"
                            % (label, bad_route))
            continue

        platforms[key] = Platform(
            key=key,
            display_name=_unquote(opts.get("display_name", "")) or key.upper(),
            profile_dir=Path(expanded),
            compression=compression,
            default_profile=default or None,
            expected_routes=routes,
        )

    if not platforms and not problems:
        problems.append("no platforms configured (add a [platform:NAME] section)")
    if problems:
        raise ConfigError(problems)
    return Settings(backend=backend, allow_multiple=allow_multiple,
                    connect_timeout=connect_timeout, platforms=platforms,
                    warnings=warnings)


def load_config(path):
    path = Path(path)
    if not path.exists():
        raise ConfigNotFound(["config file not found: %s" % path])
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(["cannot read %s: %s" % (path, getattr(exc, "strerror", None) or exc)])
    return parse_config(text, str(path))


def write_default_config(path, force=False):
    """Write the commented default config. Raises FileExistsError if present."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w" if force else "x", encoding="utf-8") as fh:
        fh.write(DEFAULT_CONFIG)
    return path


def set_platform_option(text, key, option, value):
    """Return `text` with one option of [platform:KEY] set, changing nothing else.

    value=None removes the option. The section must exist (KeyError otherwise).
    Comments, other sections and the order of lines are kept as they are.
    """
    lines = text.split("\n")
    header = re.compile(r"^\s*\[\s*platform:\s*%s\s*\]\s*$" % re.escape(key), re.I)
    any_header = re.compile(r"^\s*\[")
    opt_line = re.compile(r"^\s*%s\s*=" % re.escape(option), re.I)
    any_opt = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*=")

    start = next((i for i, ln in enumerate(lines) if header.match(ln)), None)
    if start is None:
        raise KeyError(key)
    end = next((i for i in range(start + 1, len(lines)) if any_header.match(lines[i])),
               len(lines))

    existing = [i for i in range(start + 1, end) if opt_line.match(lines[i])]
    new_line = "%s = %s" % (option, value) if value is not None else None
    if existing:
        if new_line is None:
            for i in reversed(existing):
                del lines[i]
        else:
            lines[existing[0]] = new_line
            for i in reversed(existing[1:]):
                del lines[i]
    elif new_line is not None:
        last = start
        for i in range(start + 1, end):
            if any_opt.match(lines[i]):
                last = i
        lines.insert(last + 1, new_line)
    return "\n".join(lines)


def write_text_atomic(path, text):
    """Replace a file's contents without ever leaving it half-written."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    with open(str(tmp), "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(str(tmp), mode)
    os.replace(str(tmp), str(path))
