# Changelog

## 0.5.0
- `install-shortcuts`: creates `<platform>-start` / `<platform>-stop` launchers
  (never overwrites files it did not create; `--remove`, `--dir`).
- `install.sh` (pipx, falls back to pip --user; no sudo).
- README: installation per distro, troubleshooting, WSL2 notes.
- GitHub Actions workflow running the unit tests on Python 3.8-3.13.
- Profile names match on any unique part of the name (`htb-start eu`), not only a prefix.

## 0.4.0
- OpenVPN 2 backend and cross-backend session view (simulated tests only so far).

## 0.3.0
- Diagnostics: `diagnose` and `diagnose <target>`.

## 0.2.0
- OpenVPN 3 backend, compression policy, failure hints, state tracking.

## 0.1.0
- Environment detection, config, profile listing.
