#!/usr/bin/env bash
set -eu

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv"
PYTHON="${PYTHON:-python3}"

print_install_command() {
    local tool="$1"
    case "$(uname -s)" in
        Darwin)
            if command -v brew >/dev/null 2>&1; then
                printf '%s\n' "Install $tool with: brew install $tool"
            else
                printf '%s\n' "Install Homebrew from https://brew.sh, then run: brew install $tool"
            fi
            ;;
        Linux)
            if command -v apt-get >/dev/null 2>&1; then
                printf '%s\n' "Install $tool with: sudo apt-get install $tool"
            elif command -v dnf >/dev/null 2>&1; then
                printf '%s\n' "Install $tool with: sudo dnf install $tool"
            elif command -v pacman >/dev/null 2>&1; then
                printf '%s\n' "Install $tool with: sudo pacman -S $tool"
            elif command -v zypper >/dev/null 2>&1; then
                printf '%s\n' "Install $tool with: sudo zypper install $tool"
            else
                printf '%s\n' "Install $tool with your Linux distribution's package manager."
            fi
            ;;
        *)
            printf '%s\n' "Install $tool with your operating system's package manager."
            ;;
    esac
}

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    printf '%s\n' "Python 3 is required. Install Python 3.10+ and run this again." >&2
    exit 1
fi

if [ -d "$VENV" ] && [ ! -x "$VENV/bin/python" ]; then
    printf '%s\n' "Repairing the existing virtual environment..."
    "$PYTHON" -m venv --clear "$VENV"
else
    "$PYTHON" -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$ROOT"

mkdir -p "$HOME/.local/bin"
ln -sfn "$VENV/bin/jmlcli" "$HOME/.local/bin/jmlcli"

printf '%s\n' "Installed jmlcli. Launch it with: jmlcli"
case ":${PATH}:" in
    *":$HOME/.local/bin:"*) ;;
    *) printf '%s\n' "Add ~/.local/bin to PATH, then open a new terminal before running jmlcli." ;;
esac

if ! "$VENV/bin/python" -c 'import mpv' >/dev/null 2>&1; then
    printf '%s\n' "Warning: libmpv is unavailable; install it before playing audio."
    print_install_command mpv
fi
