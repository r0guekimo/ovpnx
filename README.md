# ovpnx

A small, dependency-free CLI that makes connecting to cybersecurity lab VPNs
(Hack The Box, TryHackMe, and others) simple and reliable on native Linux and
WSL2.

> **Status: early development (roadmap steps 1-2 done).**
> Working now (OpenVPN 3): `env`, `config`, `list`, `connect`, `disconnect`,
> `status`, `clean`. Coming next: compression error handling polish,
> `diagnose`, the OpenVPN 2 backend, and `install-shortcuts`.

## Why

Switching between lab VPNs means re-importing `.ovpn` files, remembering
OpenVPN flags, fixing errors such as
`Compression Error: server pushed compression settings that are not allowed`,
and manually checking `tun0` and routes when something fails. ovpnx wraps that
workflow in a few commands and explains what went wrong.

It is **not** a general-purpose VPN client.

## Quick start (development)

```bash
git clone https://github.com/<you>/ovpnx.git && cd ovpnx

# run without installing
PYTHONPATH=src python3 -m ovpnx env

# or install as a command (recommended on Kali/Debian: pipx)
pipx install --editable .
ovpnx env
```

```bash
ovpnx env                # what was detected: WSL/native, systemd, OpenVPN 2/3
ovpnx config --init      # create ~/.config/ovpnx/config.ini
ovpnx config             # show the loaded settings
ovpnx list               # list profiles per platform
ovpnx list htb --paths
```

## Usage

```bash
ovpnx connect htb machines_eu-4   # import (if needed), connect, report
ovpnx status                      # exit status 0 = connected, 1 = not connected
ovpnx disconnect                  # ends ovpnx sessions and removes their imports
ovpnx disconnect --all            # also sessions you started some other way
ovpnx clean                       # remove leftover imports made by ovpnx
ovpnx diagnose                    # coming in step 4
```

Profile names can be abbreviated when unambiguous (`ovpnx connect htb machines`).
If `default_profile` is set, the profile name can be left out.

Convenience wrappers are planned as real executables (step 6). Until then the
same thing works by symlinking `ovpnx` under another name:

```bash
ln -s "$(command -v ovpnx)" ~/.local/bin/htb-start
ln -s "$(command -v ovpnx)" ~/.local/bin/htb-stop
htb-start machines_eu-4       # = ovpnx connect htb machines_eu-4
htb-stop                      # = ovpnx disconnect
```

How it works with OpenVPN 3: the `.ovpn` file is imported read-only under the
name `ovpnx-<platform>-<profile>` (tagged `ovpnx`), the platform's compression
policy is applied to the imported copy, a session is started, and the import
is removed again on disconnect. Configs you imported yourself are never touched.

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
- OpenVPN 3 backend: connect, disconnect, status, clean *(done)*
- One VPN at a time by default, including sessions started outside ovpnx *(done)*
- Per-platform compression policy, never applied blindly; failures explain the fix *(done)*
- OpenVPN 2 backend *(planned, step 5)*
- Post-connect diagnostics: routes, gateway, DNS *(planned, step 4)*
- Original `.ovpn` files are never modified *(enforced and tested)*

## Requirements

- Python 3.8+ (standard library only)
- OpenVPN 3 (`openvpn3`) and/or OpenVPN 2 (`openvpn`)
- `iproute2` (`ip` command)

## Configuration

`~/.config/ovpnx/config.ini` (create it with `ovpnx config --init`)

```ini
[general]
backend = auto            ; auto | openvpn3 | openvpn2
allow_multiple = false
connect_timeout = 60      ; seconds, 5-600

[platform:htb]
display_name = Hack The Box
profile_dir = ~/vpn-profiles/HTB
compression = asym        ; none | asym | yes
default_profile = starting-point

[platform:thm]
display_name = TryHackMe
profile_dir = ~/vpn-profiles/THM
compression = none
```

- The part after `platform:` becomes the shortcut prefix (`htb-start`).
- `compression = none` passes nothing. `asym` accepts compressed data from the
  server but never compresses what you send. `yes` is not recommended.
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
├── runner.py         run a command, optionally echoing output live (login prompts)
├── netinfo.py        interface IP lookup via `ip`
├── diagnostics.py    (step 4) ordered checks with pass/warn/fail plus a hint
└── backends/
    ├── __init__.py   backend selection (auto / openvpn3 / openvpn2)
    ├── base.py       Session type, naming, failure hints
    ├── openvpn3.py   OpenVPN 3 backend and output parsers
    └── openvpn2.py   (step 5)
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
3. Compression policy: polish after real-world testing
4. Diagnostics
5. OpenVPN 2 backend
6. Shortcuts (`install-shortcuts`), install script, docs, testing on WSL2 and native Linux

## License

MIT
