"""Config loading and validation (INI, standard library only)."""
import configparser
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

APP = "ovpnx"
BACKENDS = ("auto", "openvpn3", "openvpn2")
COMPRESSION = ("none", "asym", "yes")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_GENERAL_KEYS = {"backend", "allow_multiple"}
_PLATFORM_KEYS = {"display_name", "profile_dir", "compression", "default_profile"}

DEFAULT_CONFIG = """\
; ovpnx configuration. Lines starting with ';' or '#' are comments.

[general]
; backend: auto | openvpn3 | openvpn2
;   auto prefers OpenVPN 3 when installed and working, else OpenVPN 2.
backend = auto
; allow_multiple: false refuses to start a second VPN while one is active.
allow_multiple = false

; One [platform:NAME] section per lab platform. NAME becomes the command
; prefix (htb -> htb-start / htb-stop). Add more sections for other platforms.

[platform:htb]
display_name = Hack The Box
; Directory holding your .ovpn files. On WSL this may be a Windows path such
; as /mnt/c/Users/<you>/Desktop/VPN Profiles/HTB
profile_dir = ~/vpn-profiles/HTB
; compression: none | asym | yes
;   none = pass nothing (OpenVPN default, safest)
;   asym = accept compressed data from the server, never compress what you send
;   yes  = full compression both ways (not recommended, weakens security)
compression = asym
; default_profile = starting-point

[platform:thm]
display_name = TryHackMe
profile_dir = ~/vpn-profiles/THM
compression = none
; default_profile = eu
"""


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


@dataclass
class Settings:
    backend: str = "auto"
    allow_multiple: bool = False
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
    backend, allow_multiple = "auto", False

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

        platforms[key] = Platform(
            key=key,
            display_name=_unquote(opts.get("display_name", "")) or key.upper(),
            profile_dir=Path(expanded),
            compression=compression,
            default_profile=default or None,
        )

    if not platforms and not problems:
        problems.append("no platforms configured (add a [platform:NAME] section)")
    if problems:
        raise ConfigError(problems)
    return Settings(backend, allow_multiple, platforms, warnings)


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
