# Contributing to ovpnx

Thanks for helping out. ovpnx is a small, dependency-free tool, so a few rules keep it that way.

## Setup

```bash
git clone https://github.com/r0guekimo/ovpnx.git
cd ovpnx
PYTHONPATH=src python3 -m unittest discover -s tests -t . < /dev/null
```

Closing stdin (`< /dev/null`) makes any accidental real `input()` call fail instead of hanging.

## Guidelines

- Python standard library only, and it must work on Python 3.8+.
- Tests must be hermetic: never call the real `openvpn3`, `sudo` or `ip`.
- Never modify a user's `.ovpn` file, store passwords, or log keys and credentials.
- Platforms are configuration, not code. Don't hardcode HTB/THM specifics.
- Real command output makes the best test fixture (see `tests/fixtures/`).

## Pull requests

1. Branch from `main`.
2. Keep each PR small and focused.
3. Add or update tests, and make sure the suite passes.
4. Update `CHANGELOG.md` for user-visible changes.

## Reporting bugs

Run `ovpnx env` and `ovpnx diagnose`, then paste the output (redact anything private) in a new issue.