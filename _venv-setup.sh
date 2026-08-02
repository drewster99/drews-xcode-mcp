#!/bin/bash
# Shared venv setup — sourced by deploy.sh, deploy-beta.sh, dev.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -z "$SCRIPT_DIR" ]; then
    echo "❌ Could not resolve the directory containing _venv-setup.sh"
    exit 1
fi
VENV_DIR="$SCRIPT_DIR/venv"

# This checkout is reachable under two paths (~/cursor/drews-xcode-mcp is a
# symlink to ~/Documents/ncc_source/cursor/drews-xcode-mcp), so the same venv
# has two spellings. Every path comparison below resolves to the physical path
# first, otherwise running a script through the symlink would look like a moved
# venv and rebuild a perfectly good one.
#
# Prints nothing for a path that does not exist. Always succeeds, so callers can
# assign the result under 'set -e' without a '|| true' on every line. CDPATH is
# cleared because a CDPATH set in the environment can make cd print its target.
resolve_physical_path() {
    if [ -n "$1" ] && [ -d "$1" ]; then
        (CDPATH= cd -P "$1" 2>/dev/null && pwd -P)
    fi
    return 0
}

# Find python3 (preferred) or python
if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
else
    echo "❌ python3/python not found in PATH"
    exit 1
fi
echo "✅ Using $PYTHON_CMD: $(command -v $PYTHON_CMD)"

# An existing venv goes bad in two ways that are easy to miss. The base
# interpreter it symlinks to can disappear (Homebrew/conda upgrade), which is
# loud. Or the checkout gets moved or renamed, which is silent and worse:
# bin/activate hardcodes the old VIRTUAL_ENV path, so sourcing it prepends a
# nonexistent directory to PATH and every pip install afterwards lands in
# whatever python is next on PATH — often conda base — while still exiting 0.
# Note that "$VENV_DIR/bin/python -c 'import sys'" does NOT detect the move:
# bin/python is an absolute symlink to the base interpreter and sys.prefix is
# derived from the executable's location, so it keeps working after a move.
if [ -d "$VENV_DIR" ]; then
    venv_problem=""
    if [ ! -f "$VENV_DIR/pyvenv.cfg" ]; then
        echo "❌ $VENV_DIR exists but is not a virtualenv (no pyvenv.cfg)"
        echo "   Refusing to delete it. Move it aside and re-run."
        exit 1
    elif ! "$VENV_DIR/bin/python" -c "import sys" &>/dev/null; then
        venv_problem="its python does not run (base interpreter gone, or bin/ damaged)"
    else
        # Sourcing activate in a subshell tests what activation actually does,
        # rather than depending on how a given Python version formats the script.
        activated_venv_dir="$(. "$VENV_DIR/bin/activate" >/dev/null 2>&1 && printf '%s' "$VIRTUAL_ENV")" || true
        if [ "$(resolve_physical_path "$activated_venv_dir")" != "$(resolve_physical_path "$VENV_DIR")" ]; then
            venv_problem="it was moved or renamed (activate points at ${activated_venv_dir:-an unset path})"
        fi
    fi

    if [ -n "$venv_problem" ]; then
        echo "⚠️  Existing venv is unusable — $venv_problem. Recreating..."
        rm -rf "$VENV_DIR"
    fi
fi

# Create venv if needed
if [ ! -d "$VENV_DIR" ]; then
    echo "📦 Creating venv at $VENV_DIR..."
    $PYTHON_CMD -m venv "$VENV_DIR"
fi

# Activate
source "$VENV_DIR/bin/activate"

# Fail loudly if activation did not take. Callers immediately pip install into
# what they assume is the venv; a wrong VIRTUAL_ENV or a python resolved from
# outside the venv would install to the wrong environment and still exit 0.
active_python="$(command -v python)" || true
expected_venv="$(resolve_physical_path "$VENV_DIR")"
expected_bin="$(resolve_physical_path "$VENV_DIR/bin")"
actual_venv="$(resolve_physical_path "${VIRTUAL_ENV:-}")"
actual_bin="$(resolve_physical_path "$(dirname "${active_python:-/nonexistent/none}")")"
# The empty checks matter: resolve_physical_path prints nothing for a path that
# does not exist, and without them two missing paths would compare equal and the
# assertion would pass on exactly the broken setup it exists to catch.
if [ -z "$expected_venv" ] || [ -z "$expected_bin" ] ||
   [ "$actual_venv" != "$expected_venv" ] ||
   [ "$actual_bin" != "$expected_bin" ]; then
    echo "❌ venv activation did not take effect"
    echo "   expected venv: $VENV_DIR"
    echo "   VIRTUAL_ENV:   ${VIRTUAL_ENV:-<unset>}"
    echo "   python:        ${active_python:-<none>}"
    exit 1
fi

pip install -q --upgrade pip
echo "✅ venv activated: $(which python)"
