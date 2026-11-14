# ovpnx

A small, dependency-free CLI that makes connecting to cybersecurity lab VPNs
(Hack The Box, TryHackMe, and others) simple and reliable on native Linux and
WSL2.

> **Status: beta (v0.6).** Everything below works with OpenVPN 3 and OpenVPN 2 and has
> been tested on Kali under WSL2 and on a native Kali VM. Feedback and bug reports welcome.

## Why

Switching between lab VPNs means re-importing `.ovpn` files, remembering
OpenVPN flags, fixing errors such as
`Compression Error: server pushed compression settings that are not allowed`,
and manually checking `tun0` and routes when something fails. ovpnx wraps that
workflow in a few commands and explains what went wrong.

It is **not** a general-purpose VPN client.

## Quick start

```bash
git clone https://github.com/<you>/ovpnx.git && cd ovpnx

# run without installing
PYTHONPATH=src python3 -m ovpnx env

# or install as a command (recommended on Kali/Debian: pipx)
./install.sh            # wraps `pipx install .`, no sudo needed
ovpnx env
```

```bash
ovpnx env                # what was detected: WSL/native, systemd, OpenVPN 2/3
ovpnx setup              # guided: finds your .ovpn files, you choose the platforms
ovpnx list               # list profiles per platform
ovpnx connect <platform> # connect (see Usage)
```

## Setup

`ovpnx setup` has no built-in platforms: you decide everything.

1. It searches your home folder (and the Windows user folders on WSL2) for
   folders that contain `.ovpn` files and lists them.
2. You pick the folders that are lab platforms (or type a path yourself).
3. For each one you choose a short name (it becomes `<name>-start` and
   `<name>-stop`), a display name, an optional default profile and the
   compression policy. Every question has a suggested answer; press Enter to accept it.
4. It shows a summary, asks before writing, and offers to create the shortcuts.

Run `ovpnx setup` again at any time to add platforms; your existing file and
comments are kept. Only values you chose are written, so the file stays short.
`ovpnx config --init` writes a commented template instead if you prefer to edit by hand.

## Usage

```bash
ovpnx connect htb machines_eu-4   # connect to a profile of platform 'htb' (your names)
ovpnx status                      # exit status 0 = connected, 1 = not connected
ovpnx disconnect                  # ends ovpnx sessions and removes their imports
ovpnx disconnect --all            # also sessions you started some other way
ovpnx clean                       # remove leftover imports made by ovpnx
ovpnx diagnose                    # health check of the active VPN
ovpnx diagnose 10.129.1.1         # ...plus a test of one target machine
ovpnx diagnose 10.129.1.1 --ports 22,80,8080
```

### Shortcuts

```bash
ovpnx install-shortcuts           # creates ~/.local/bin/<platform>-start and -stop
htb-start eu                      # same as: ovpnx connect htb eu
htb-stop                          # same as: ovpnx disconnect
ovpnx install-shortcuts --remove  # delete the ones ovpnx created
```

One pair is created per platform in your config (re-run after adding a platform).
Existing files that ovpnx did not create are never overwritten or deleted. If
`~/.local/bin` is not on your `PATH`, the command tells you how to add it.
Options such as `-v` or `--config FILE` go right after the command name:
`htb-start -v eu`.

### What `diagnose` checks

In order, each failure comes with the probable cause:

1. the session is connected
2. the tunnel interface exists and is up (`state UNKNOWN` is normal for tunnels;
   the `UP` and `LOWER_UP` flags are what count)
3. a VPN IP is assigned
4. routes were pushed through the tunnel (split or full tunnel is reported;
   a missing default route is normal for lab platforms)
5. the VPN gateway answers ping (a warning only, because some networks filter ICMP)
6. DNS resolves a public name (a warning only; lab machines are reached by IP)

With a target (IP or hostname) it also checks that the route to the target goes
through the VPN (nothing is sent otherwise), pings it, and probes a few TCP
ports. A refused port still proves the machine is alive, so a machine that
filters ICMP is not reported as down. The verdict separates "the VPN gateway
says host unreachable" (usually: machine not spawned, or wrong VPN server for
that lab) from "no response at all" (inconclusive: still booting, or filtered).

The exit status is 0 unless a check failed.

Profile names can be abbreviated when unambiguous: a prefix (`machines`) or any unique part of the name (`eu`, `us-2`) works.
If `default_profile` is set, the profile name can be left out.

Convenience wrappers are planned as real executables (step 6). Until then the
same thing works by symlinking `ovpnx` under another name:

```bash
ln -s "$(command -v ovpnx)" ~/.local/bin/htb-start
ln -s "$(command -v ovpnx)" ~/.local/bin/htb-stop
htb-start machines_eu-4       # = ovpnx connect htb machines_eu-4
htb-stop                      # = ovpnx disconnect
```

### Backends

`backend = auto` (the default) uses OpenVPN 3 when it is installed and its
services respond, and otherwise OpenVPN 2. Force one with `backend = openvpn3`
or `backend = openvpn2`. `ovpnx env` shows which one would be used.

**OpenVPN 3:** the `.ovpn` file is imported read-only under the name
`ovpnx-<platform>-<profile>` (tagged `ovpnx`), the platform's compression
policy is applied to the imported copy, a session is started, and the import
is removed again on disconnect. Configs you imported yourself are never touched.
No root needed.

**OpenVPN 2:** needs root to create the tunnel, so ovpnx uses `sudo` for exactly
two things, starting `openvpn` and stopping it, and never runs as root itself
(it asks for your sudo password once if needed). The daemon is started as
`openvpn --daemon ovpnx-<platform>-<profile>` with its log in
`~/.local/state/ovpnx/openvpn2/` (private, owned by you); progress is read from
that log, which is deleted on disconnect because it can contain session tokens.
`--allow-compression` is only passed on OpenVPN 2.5 and newer.
Limitation: profiles that prompt for a username and password are refused on this
backend (use OpenVPN 3, or reference a private credentials file from a copy of
the profile).

**One VPN at a time:** ovpnx sees tunnels from both backends, including any
`openvpn` process you started by hand or via systemd, and refuses a second
connection unless `allow_multiple = true`.

Example output:

```text
[+] Platform: Hack The Box
[+] Profile: machines_eu-4
[+] OpenVPN backend: OpenVPN 3
[*] Starting connection...

[+] VPN connected
[+] Interface: tun0
[+] VPN IP: 10.10.16.153
[+] Route check: Passed

[*] Connection established successfully.
```

## Features

- Multiple profiles per platform, selected by name or unique prefix *(done)*
- Platforms defined in config, so adding one needs no code changes *(done)*
- Environment detection: native Linux, VM, WSL1/WSL2, systemd *(done)*
- OpenVPN 3 and OpenVPN 2 backends: connect, disconnect, status, clean *(done)*
- One VPN at a time by default, including sessions started outside ovpnx *(done)*
- Per-platform compression policy, never applied blindly; failures explain the fix *(done)*
- Diagnostics: interface, IP, routes, gateway, DNS and per-target reachability *(done)*
- `install-shortcuts` creates `htb-start` / `htb-stop` style commands *(done)*
- Original `.ovpn` files are never modified *(enforced and tested)*

## Requirements

- Python 3.8+ (standard library only)
- OpenVPN 3 (`openvpn3`) and/or OpenVPN 2 (`openvpn`, 2.4 or newer)
- `sudo` (or run as root) if you use OpenVPN 2
- `iproute2` (`ip` command)

## Installation

```bash
# Debian / Kali / Ubuntu
sudo apt install pipx openvpn iproute2 && pipx ensurepath
# Fedora:  sudo dnf install pipx openvpn iproute
# Arch:    sudo pacman -S python-pipx openvpn iproute2

git clone https://github.com/<you>/ovpnx.git && cd ovpnx
./install.sh
```

OpenVPN 3 is optional and needs no root to connect; installation steps are at
<https://community.openvpn.net/openvpn/wiki/OpenVPN3Linux>. Without it, ovpnx
uses OpenVPN 2 (`openvpn`) and asks for your `sudo` password when connecting.

## Troubleshooting

**`Compression Error: server pushed compression settings that are not allowed`**
Hack The Box servers push compression that OpenVPN 3 rejects by default. Make
sure the platform has `compression = asym` in the config; ovpnx then applies
the setting to its own imported copy and never edits your `.ovpn` file.

**Connected, but a machine says "Host Unreachable"**
Run `ovpnx diagnose <machine-ip>`. If the VPN checks pass and the gateway
reports the host unreachable, the VPN is fine: the machine is usually not
spawned yet, or you are connected to a VPN server that cannot reach that lab.
Spawn it, or connect with a profile for the right region or lab.

**"No usable backend" or OpenVPN 3 does not answer (WSL2)**
OpenVPN 3 needs systemd. Enable it in `/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

then run `wsl --shutdown` from Windows and reopen the distro. Alternatively
install OpenVPN 2 and set `backend = openvpn2`.

**WSL2 notes**
- Only the Linux distribution is tunneled. Windows apps and browsers are not.
  Use the tools (and a browser) inside the distro.
- WSL1 has no TUN device and cannot work; upgrade to WSL2.
- Both NAT and mirrored networking modes should work; `ovpnx env` shows which
  one was detected.
- If connecting fails with a TUN error, check that `/dev/net/tun` exists.

**A login prompt profile with OpenVPN 2**
Profiles with a bare `auth-user-pass` line are only supported by OpenVPN 3
(it prompts you). With OpenVPN 2, ovpnx refuses them rather than store a password.

**`htb-start: command not found`**
Run `ovpnx install-shortcuts`, and make sure `~/.local/bin` is on your `PATH`.

**Anything else:** `ovpnx -v <command>` prints debug lines, and
`~/.local/state/ovpnx/logs/ovpnx.log` keeps a redacted log.

## Configuration

`~/.config/ovpnx/config.ini` (created by `ovpnx setup`)

```ini
[platform:mylab]
display_name = My Lab
profile_dir = ~/path/to/ovpn/files
default_profile = my-profile
compression = none        ; none | asym | yes
expected_routes =         ; optional, e.g. 10.129.0.0/16, 10.10.10.0/24

[general]                 ; optional section, these are the defaults
backend = auto            ; auto | openvpn3 | openvpn2
allow_multiple = false
connect_timeout = 60      ; seconds, 5-600
```

- The part after `platform:` becomes the shortcut prefix (`mylab-start`).
- `compression = none` passes nothing. `asym` accepts compressed data from the
  server but never compresses what you send. `yes` is not recommended.
- `expected_routes` (optional) lists networks that `ovpnx diagnose` verifies are
  routed through the VPN. Leave it empty if you are unsure; different servers
  push different ranges.
- Quotes around paths are optional. Typos in option names produce warnings.
- On WSL2 the profile directory may be a Windows path, for example
  `/mnt/c/Users/<you>/Desktop/VPN Profiles/HTB`.
- Override the file location with `--config FILE` or `OVPNX_CONFIG`.

State and logs live in `~/.local/state/ovpnx/` (respects `XDG_*` variables).

## Architecture

```text
src/ovpnx/
├── cli.py            argparse, commands, argv[0] dispatch for htb-start etc.
├── config.py         load and validate the INI config, XDG paths
├── env.py            native / WSL, systemd, privileges, installed backends
├── profiles.py       discover .ovpn files, select by name, never modify
├── log.py            console output, secret redaction, private log file
├── state.py          small JSON state, always verified against reality
├── runner.py         run a command: live echo (login prompts) or detached (daemons)
├── netinfo.py        interface IP lookup via `ip`
├── wizard.py         guided setup (ovpnx setup)
├── shortcuts.py      create/remove the <platform>-start/-stop launchers
├── diagnostics.py    ordered checks with pass/warn/fail plus a hint, target test
└── backends/
    ├── __init__.py   backend selection (auto / openvpn3 / openvpn2)
    ├── base.py       Session type, naming, failure hints
    ├── openvpn3.py   OpenVPN 3 backend and output parsers
    └── openvpn2.py   OpenVPN 2 backend: daemon, log parsing, /proc discovery
tests/                unittest, standard library only
```

## Security principles

- No passwords stored. The backend prompts when `auth-user-pass` is required.
- No keys, certificates or credentials in output or logs: every log line goes
  through a redaction filter, and the log file is created with mode 600.
- Terminal output strips control characters, so odd file names cannot inject
  escape sequences.
- Original `.ovpn` files are preserved untouched. They are only read to hash
  them and to notice the `auth-user-pass` directive.
- The tool itself never runs as root; `sudo` is used only for the single
  OpenVPN 2 invocation that needs it.
- TLS verification and other protections are never silently disabled.

## Development

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

## Roadmap

1. ~~Environment detection, config, profile listing~~
2. ~~OpenVPN 3 backend (connect, disconnect, status, clean) and state tracking~~
3. ~~Compression policy and failure hints~~ (verified with a real HTB connection)
4. ~~Diagnostics~~
5. ~~OpenVPN 2 backend~~
6. ~~Shortcuts (`install-shortcuts`), install script, troubleshooting docs~~
7. ~~Guided setup~~, live testing on native Linux ~~(done)~~
8. WSL2 mirrored mode, THM profiles, v1.0.0

## License

MIT
