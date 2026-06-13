#!/bin/sh
# OLYMPUS one-line installer.
#
#   curl -fsSL https://raw.githubusercontent.com/maadjiba24-afk/Olympus-/main/install.sh | sh
#
# What it does (and nothing more):
#   1. Creates a private Python environment in ~/.olympus/venv
#   2. Installs Olympus into it
#   3. Puts an `olympus` command on your PATH
# Then you just type `olympus` — it asks for your API key once and you chat
# in plain English. Uninstall anytime: rm -rf ~/.olympus ~/.local/bin/olympus

set -e

REPO="${OLYMPUS_REPO:-https://github.com/maadjiba24-afk/Olympus-}"
HOME_DIR="${OLYMPUS_HOME:-$HOME/.olympus}"
VENV="$HOME_DIR/venv"
BIN_DIR="$HOME/.local/bin"

say()  { printf '\033[1;33m⚡ %s\033[0m\n' "$1"; }
fail() { printf '\033[1;31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

# --- 1. find a suitable Python -------------------------------------------
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PY="$cand"; break
        fi
    fi
done
[ -n "$PY" ] || fail "Python 3.10+ is required. Install it first (e.g. 'sudo apt install python3' on Ubuntu, 'brew install python' on macOS), then re-run this command."

say "Installing OLYMPUS (using $($PY --version 2>&1))…"

# --- 2. private environment ----------------------------------------------
mkdir -p "$HOME_DIR"
if ! "$PY" -m venv "$VENV" 2>/dev/null; then
    # Debian/Ubuntu ship venv separately
    fail "Could not create a Python environment. On Ubuntu/Debian run: sudo apt install python3-venv  — then re-run this command."
fi

"$VENV/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
say "Downloading Olympus…"
"$VENV/bin/pip" install --quiet --upgrade "git+$REPO" \
    || fail "Install failed. Check your internet connection and try again."

# --- 3. the `olympus` command --------------------------------------------
mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/olympus" "$BIN_DIR/olympus"

case ":$PATH:" in
    *":$BIN_DIR:"*) ON_PATH=1 ;;
    *) ON_PATH=0 ;;
esac
if [ "$ON_PATH" = "0" ]; then
    LINE='export PATH="$HOME/.local/bin:$PATH"'
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
        if [ -f "$rc" ] && ! grep -qs '\.local/bin' "$rc"; then
            printf '\n# added by the OLYMPUS installer\n%s\n' "$LINE" >> "$rc"
        fi
    done
fi

echo
say "OLYMPUS installed."
echo
if [ "$ON_PATH" = "0" ]; then
    echo "   Open a NEW terminal (or run: export PATH=\"\$HOME/.local/bin:\$PATH\"),"
    echo "   then type:   olympus"
else
    echo "   Type:   olympus"
fi
echo
echo "   It will ask which API key you're bringing (Anthropic, OpenAI, or any"
echo "   compatible provider), save it securely, and drop you into chat —"
echo "   plain English from there. No bash, no pip."
echo
