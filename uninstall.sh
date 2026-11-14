#!/bin/sh
# Uninstall ovpnx for the current user. Never uses sudo.
#
#   ./uninstall.sh            ask before deleting your config and logs
#   ./uninstall.sh --purge    also delete config and logs without asking
#   ./uninstall.sh --keep     keep config and logs without asking
#
# Never touched: your .ovpn profile files and this source folder.
set -eu

MODE=ask
for arg in "$@"; do
    case "$arg" in
        --purge) MODE=purge ;;
        --keep) MODE=keep ;;
        -h|--help)
            sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "[-] Unknown option: $arg (try --help)" >&2; exit 1 ;;
    esac
done

ok()   { echo "[+] $*"; }
info() { echo "[*] $*"; }
warn() { echo "[!] $*"; }

if command -v ovpnx >/dev/null 2>&1; then
    # Leave nothing running or imported behind.
    info "Disconnecting any VPN started by ovpnx..."
    ovpnx disconnect >/dev/null 2>&1 || true
    ovpnx clean >/dev/null 2>&1 || true
    info "Removing shortcut commands and TAB completion..."
    ovpnx install-shortcuts --remove || true
    ovpnx install-completion --remove || true
else
    warn "'ovpnx' is not on PATH, so the shortcuts and completion lines cannot be"
    warn "removed automatically. Delete ~/.local/bin/<name>-start and <name>-stop yourself,"
    warn "and the '# >>> ovpnx completion >>>' block in ~/.bashrc or ~/.zshrc."
fi

info "Uninstalling the ovpnx package..."
if command -v pipx >/dev/null 2>&1 && pipx list --short 2>/dev/null | grep -q '^ovpnx '; then
    pipx uninstall ovpnx
    ok "Removed the package (pipx)"
elif python3 -m pip show ovpnx >/dev/null 2>&1; then
    python3 -m pip uninstall -y ovpnx 2>/dev/null \
        || python3 -m pip uninstall -y --break-system-packages ovpnx
    ok "Removed the package (pip)"
else
    info "The ovpnx package was not installed."
fi

# Config and logs: only the exact ovpnx folders, never anything else.
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/ovpnx"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/ovpnx"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/ovpnx"

remove_dir() {
    case "$1" in
        */ovpnx) [ -d "$1" ] && rm -rf -- "$1" && ok "Deleted $1" ;;
        *) warn "Refusing to delete unexpected path: $1" ;;
    esac
    return 0
}

found=""
for d in "$CONFIG_DIR" "$STATE_DIR" "$DATA_DIR"; do
    [ -d "$d" ] && found="$found $d"
done

if [ -n "$found" ]; then
    case "$MODE" in
        purge) answer=y ;;
        keep) answer=n ;;
        *)
            echo
            echo "Your ovpnx settings and logs are still on disk:"
            for d in $found; do echo "    $d"; done
            if [ -t 0 ]; then
                printf "Delete them too? [y/N]: "
                read -r answer || answer=n
            else
                answer=n
            fi ;;
    esac
    case "$answer" in
        y|Y|yes|YES) for d in $found; do remove_dir "$d"; done ;;
        *) info "Kept your settings and logs." ;;
    esac
fi

echo
ok "ovpnx is uninstalled. Your .ovpn profile files were not touched."
info "Open a new terminal so the removed commands disappear from your shell."
