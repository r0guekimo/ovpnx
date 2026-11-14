#!/bin/sh
# Install ovpnx for the current user. Never uses sudo.
set -eu

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[-] python3 not found. Install Python 3.8 or newer first." >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
    echo "[-] Python 3.8 or newer is required (found: $(python3 -V 2>&1))." >&2
    exit 1
fi

if command -v pipx >/dev/null 2>&1; then
    echo "[*] Installing with pipx..."
    pipx install --force .
elif python3 -m pip --version >/dev/null 2>&1; then
    echo "[*] pipx not found; installing with pip --user..."
    python3 -m pip install --user --upgrade . 2>/dev/null \
        || python3 -m pip install --user --upgrade --break-system-packages .
else
    echo "[-] Neither pipx nor pip found." >&2
    echo "    Debian/Kali/Ubuntu: sudo apt install pipx   then run ./install.sh again." >&2
    exit 1
fi

# Make sure the command is reachable.
if ! command -v ovpnx >/dev/null 2>&1; then
    echo "[!] 'ovpnx' is not on your PATH yet."
    echo "    Run: pipx ensurepath   (or add ~/.local/bin to PATH), then open a new terminal."
    exit 0
fi

echo "[+] Installed: $(ovpnx --version)"
echo
ovpnx env || true
echo
echo "[*] Next steps:"
echo "      ovpnx setup                  guided setup: finds your .ovpn files, creates the config"
echo "      ovpnx list                   check your profiles are found"
echo "      ovpnx install-shortcuts      create <name>-start / <name>-stop commands"
echo "      ovpnx install-completion     TAB completion for bash/zsh"
