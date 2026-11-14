# ovpnx

A small, dependency-free CLI that makes connecting to cybersecurity lab VPNs
(Hack The Box, TryHackMe, and others) simple and reliable on native Linux and
WSL2.

> **Status: design phase.** The architecture below is agreed; implementation
> is being built incrementally. Nothing here is usable yet.

## Why

Switching between lab VPNs means re-importing `.ovpn` files, remembering
OpenVPN flags, fixing errors such as
`Compression Error: server pushed compression settings that are not allowed`,
and manually checking `tun0` and routes when something fails. ovpnx wraps that
workflow in a few commands and explains what went wrong.

It is **not** a general-purpose VPN client.

## Planned usage

```bash
ovpnx list                      # show discovered profiles
ovpnx connect htb machines_eu-4
ovpnx status
ovpnx diagnose
ovpnx disconnect
ovpnx config

# convenience wrappers (real executables, not shell aliases)
htb-start machines_eu-4
htb-stop
thm-start eu
```

Example output:

```text
[+] Platform: Hack The Box
[+] Profile: machines_eu-4
[+] OpenVPN backend: OpenVPN 3
[+] Starting connection...

[+] VPN connected
[+] Interface: tun0
[+] VPN IP: 10.10.16.153
[+] Route check: Passed

[*] Connection established successfully.
```

## Features (planned)

- Multiple profiles per platform, selected by name
- Platforms defined in config, so adding one needs no code changes
- OpenVPN 3 and OpenVPN 2 backends, auto-detected
- Per-platform compression policy (never applied blindly)
- Post-connect diagnostics: session, interface, IP, routes, gateway, DNS
- Native Linux, VMs and WSL2 (auto-detected)
- Original `.ovpn` files are never modified

## Requirements

- Python 3.8+ (standard library only)
- OpenVPN 3 (`openvpn3`) and/or OpenVPN 2 (`openvpn`)
- `iproute2` (`ip` command)

## Configuration

`~/.config/ovpnx/config.ini`

```ini
[general]
backend = auto            ; auto | openvpn3 | openvpn2
allow_multiple = false

[platform:htb]
profile_dir = ~/vpn-profiles/HTB
compression = asym        ; none | asym | yes
default_profile = starting-point

[platform:thm]
profile_dir = ~/vpn-profiles/THM
compression = none
```

On WSL2 the profile directory may point at a Windows-mounted path, for example
`/mnt/c/Users/<you>/Desktop/VPN Profiles/HTB`.

State and logs live in `~/.local/state/ovpnx/`.

## Architecture

```text
src/ovpnx/
├── cli.py            argparse, commands, argv[0] dispatch for htb-start etc.
├── config.py         load and validate config
├── env.py            native / WSL2 / VM, systemd, privileges, backends
├── profiles.py       discover .ovpn files, hash them, never modify
├── state.py          small JSON state file, always verified against reality
├── diagnostics.py    ordered checks with pass/warn/fail plus a hint
├── log.py            redaction filter, secrets never reach logs
└── backends/
    ├── base.py       connect / disconnect / status / list_sessions
    ├── openvpn3.py
    └── openvpn2.py
```

## Security principles

- No passwords stored. The backend prompts when `auth-user-pass` is required.
- No keys, certificates or credentials in output or logs.
- Original `.ovpn` files are preserved untouched.
- The tool itself never runs as root; `sudo` is used only for the single
  OpenVPN 2 invocation that needs it.
- TLS verification and other protections are never silently disabled.
- Compression is only enabled (as `asym`, receive-only) when the platform
  config says so.

## Roadmap

1. Environment detection, config, profile listing
2. OpenVPN 3 backend (connect, disconnect, status)
3. Compression policy and error detection
4. Diagnostics
5. OpenVPN 2 backend
6. Shortcuts, install script, docs, testing on WSL2 and native Linux

## License

MIT
