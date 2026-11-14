"""Tiny JSON state file.

The state is only a hint about what ovpnx started. It holds no secrets and is
always cross-checked against the real backend sessions before being trusted.
"""
import json
import os
from pathlib import Path

STATE_FILE = "state.json"
_FIELDS = ("backend", "platform", "profile", "config_name", "session_path", "started")


def state_path(state_dir):
    return Path(state_dir) / STATE_FILE


def load_state(state_dir):
    """Return the stored dict, or None if missing or unreadable."""
    try:
        with open(str(state_path(state_dir)), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {k: v for k, v in data.items() if k in _FIELDS and isinstance(v, str)}


def save_state(state_dir, data):
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = state_path(state_dir)
    tmp = final.with_suffix(".tmp")
    payload = {k: str(v) for k, v in data.items() if k in _FIELDS}
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(str(tmp), str(final))


def clear_state(state_dir):
    try:
        os.unlink(str(state_path(state_dir)))
    except FileNotFoundError:
        pass
    except OSError:
        pass
