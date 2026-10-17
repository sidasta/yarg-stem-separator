#!/usr/bin/env bash
# Linux launcher: sets up Python and the packages for your graphics card (NVIDIA, AMD or
# CPU-only) on first run, then opens the YARG Stem Separator in your browser.
# If uv isn't installed, a private copy is downloaded into ./tools; nothing is installed
# system-wide. Set STEMSEP_DEVICE=nvidia, amd or cpu to override the hardware choice.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
HERE="$PWD"

find_uv() {
    [ -n "${UV:-}" ] && return 0
    if command -v uv >/dev/null 2>&1; then
        UV="$(command -v uv)"; export UV; return 0
    fi
    # No uv installed: use a private copy, and keep everything it downloads inside this folder.
    export UV="$HERE/tools/uv"
    export UV_CACHE_DIR="$HERE/tools/uv-cache"
    export UV_PYTHON_INSTALL_DIR="$HERE/tools/python"
    export UV_PYTHON_PREFERENCE=only-managed
    [ -x "$UV" ] && return 0
    local arch
    case "$(uname -m)" in
        x86_64|amd64) arch=x86_64 ;;
        aarch64|arm64) arch=aarch64 ;;
        *) echo "Unsupported CPU type $(uname -m); install uv yourself: https://docs.astral.sh/uv/"; return 1 ;;
    esac
    local name="uv-$arch-unknown-linux-gnu"
    local url="https://github.com/astral-sh/uv/releases/latest/download/$name.tar.gz"
    local tmp; tmp="$(mktemp -d)"
    echo "Downloading uv (the tool that installs Python for this app) into the tools folder..."
    if ! curl -fsSL "$url" -o "$tmp/uv.tar.gz" || ! curl -fsSL "$url.sha256" -o "$tmp/uv.sha256"; then
        rm -rf "$tmp"
        echo "Could not download uv. Install it yourself from https://docs.astral.sh/uv/ and run this again."
        return 1
    fi
    if [ "$(cut -d' ' -f1 "$tmp/uv.sha256")" != "$(sha256sum "$tmp/uv.tar.gz" | cut -d' ' -f1)" ]; then
        rm -rf "$tmp"; echo "uv download is corrupted (checksum mismatch)."; return 1
    fi
    tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
    mkdir -p "$HERE/tools"
    cp "$tmp/$name/uv" "$UV"
    chmod +x "$UV"
    rm -rf "$tmp"
}

# A setup that no longer starts (e.g. the folder was moved) is rebuilt from the download cache.
if [ -e .venv/bin/python ] && ! .venv/bin/python -c "" >/dev/null 2>&1; then
    echo "The Python setup in this folder no longer works; rebuilding it..."
    rm -rf .venv
fi
if [ ! -e .venv/bin/python ]; then
    find_uv
    echo "Setting up Python. The first time, this downloads a few GB and takes several minutes."
    "$UV" venv --python 3.12 .venv
fi
# The setup script writes this marker only after everything installed successfully.
if [ ! -e .venv/stemsep-backend.json ]; then
    find_uv
    .venv/bin/python scripts/setup_env.py
    echo
    echo "Setup complete."
fi

exec .venv/bin/python review.py --open "$@"
