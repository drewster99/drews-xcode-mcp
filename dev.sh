#!/bin/bash

# Development script for drews-xcode-mcp
# This script sets up the environment and runs the MCP inspector for testing
#
# To connect a specific release or beta to the MCP Inspector, do like this
# (use the same pinned inspector version as INSPECTOR_PACKAGE below — running it
# unpinned picks up the 2.x inspector, which does not work against this server):
#
#     npx @modelcontextprotocol/inspector@<pin> uvx drews-xcode-mcp==1.3.0b3
#
#
set -e  # Exit on error

# Pinned, not @latest. Inspector 2.0.0 (2026-07-28) is built on the MCP SDK 2.x
# line (@modelcontextprotocol/core|client|server 2.0.0-beta.5) while this server
# runs on Python mcp 1.x, and it requires node >=22.19.0, which npx will warn
# about and then install anyway. 1.0.1 is the v1-latest release and depends on
# @modelcontextprotocol/sdk ^1.25.2, matching our server. To pick up v1 fixes,
# check `npm view @modelcontextprotocol/inspector dist-tags` and bump this.
INSPECTOR_PACKAGE="@modelcontextprotocol/inspector@1.0.1"

echo "🔧 Starting drews-xcode-mcp development environment..."
echo ""

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Set up venv and install dev dependencies
source "$SCRIPT_DIR/_venv-setup.sh"
echo ""

echo "📥 Installing development dependencies..."
# -e . brings in mcp[cli] with the version cap from pyproject.toml; do not
# install mcp separately here or an unpinned resolve could pull in mcp 2.x.
pip install -q -e .
echo ""

# Check if npx is available (comes with Node.js)
if ! command -v npx &> /dev/null; then
    echo "❌ Error: npx is not installed"
    echo "Please install Node.js first: brew install node"
    exit 1
fi

# Set allowed folders to $HOME
export XCODEMCP_ALLOWED_FOLDERS="$HOME"

# Display environment info
echo "✅ Environment ready!"
echo ""
echo "📋 Configuration:"
echo "   Python: $(which python)"
echo "   Python version: $(python --version)"
echo "   venv: $VENV_DIR"
echo "   Allowed folders: ${XCODEMCP_ALLOWED_FOLDERS}"
echo "   Server path: ${SCRIPT_DIR}/drews_xcode_mcp/__main__.py"
echo ""
echo "🚀 Starting MCP Inspector..."
echo "   The inspector will open in your browser at http://localhost:5173"
echo ""
echo "   Press Ctrl+C to stop"
echo ""

#
echo "If you need to run the inspector to a published PyPi beta:"
echo "   npx ${INSPECTOR_PACKAGE} uvx drews-xcode-mcp==1.3.0b3   <-- beta version"
echo ""
echo ""
echo "If you need to test with Claude, do this:"
echo "   claude mcp remove xcode-mcp-local-dev-server"
echo "   claude mcp add --transport stdio --scope user xcode-mcp-local-dev-server `pwd`/run_local_for_claude.sh"
echo ""

# Run the MCP inspector
npx "$INSPECTOR_PACKAGE" python -m drews_xcode_mcp
