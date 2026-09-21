#!/bin/bash

# Deployment script for drews-xcode-mcp
# Builds and publishes the package to PyPI, together with a matching release of
# the legacy 'xcode-mcp-server' compatibility shim (see shim/) so existing
# configurations that run the old name keep getting current code.

# If you want to deploy a BETA, this script won't do it,
# but you can do it manually:
#
#   You can use pre-release version numbers following
#     https://peps.python.org/pep-0440/. PyPI will accept them, but pip won't
#     install them by default.
#
#     Pre-release version formats:
#     - 1.2.3b1 - beta 1
#     - 1.2.3a1 - alpha 1
#     - 1.2.3rc1 - release candidate 1
#
#     To publish a beta:
#     python -m hatch version 1.2.3b1  # Set beta version
#     python -m build
#     python -m twine upload dist/*
#
#     Users can install it with:
#     # Specific beta version (safest for testers)
#     pip install drews-xcode-mcp==1.2.3b1
#     uvx drews-xcode-mcp==1.2.3b1
#
#     Regular users doing pip install drews-xcode-mcp will get the latest
#     stable version and skip all pre-releases automatically.

set -e  # Exit on error

echo "🚀 Starting drews-xcode-mcp deployment..."
echo ""

# Pre-flight: ensure no unstaged changes
UNSTAGED=$(git diff --name-only)
if [ -n "$UNSTAGED" ]; then
    echo "❌ You have unstaged changes:"
    git diff --stat
    echo ""
    echo "Please stage ('git add') or commit your changes before deploying."
    exit 1
fi
echo "✅ No unstaged changes"
echo ""

/bin/echo -n "Hit enter to continue:"
read foo

# Set up venv and install deploy dependencies
source "$(dirname "${BASH_SOURCE[0]}")/_venv-setup.sh"
pip install -q hatch build twine
echo ""

# Create dist-archive directory if it doesn't exist
mkdir -p dist-archive

# Archive any existing dist files
if [ -d "dist" ] && [ "$(ls -A dist 2>/dev/null)" ]; then
    echo "📦 Archiving previous dist files..."
    mv dist/* dist-archive/
    echo ""
fi

# Clean dist directory
echo "🧹 Cleaning dist directory..."
rm -rf dist
mkdir -p dist
echo ""

# Increment version
echo "📝 Incrementing patch version..."
hatch version patch
NEW_VERSION=$(hatch version)
echo ""

# Keep the legacy compatibility shim in lockstep: same version, exact pin.
# The exact pin matters because uvx resolves dependencies once and caches the
# result - a floating pin would strand shim users on whatever was current.
echo "🔗 Syncing legacy xcode-mcp-server shim to $NEW_VERSION..."
sed -i '' -E "s|^version = \".*\"$|version = \"$NEW_VERSION\"|" shim/pyproject.toml
sed -i '' -E "s|drews-xcode-mcp==[^\"]*|drews-xcode-mcp==$NEW_VERSION|" shim/pyproject.toml
echo ""

# Build both packages
echo "🔨 Building drews-xcode-mcp..."
python -m build
echo ""
echo "🔨 Building legacy xcode-mcp-server shim..."
python -m build shim --outdir dist
echo ""

# Copy new build to archive
echo "💾 Copying new build to archive..."
cp dist/* dist-archive/
echo ""

# Upload to PyPI
echo "📤 Uploading to PyPI..."
if ! twine upload --skip-existing dist/*; then
    echo "❌ twine upload failed. Some files may already be on PyPI."
    echo "   Recover with: twine upload --skip-existing dist/*"
    echo "   Then verify both packages at $NEW_VERSION on PyPI, and commit the"
    echo "   version bump manually (do not re-run this script: the unstaged"
    echo "   version changes will fail the pre-flight check)."
    exit 1
fi
echo ""

# Verify on PyPI
echo "🔍 Verifying version $NEW_VERSION on PyPI..."
MAX_ATTEMPTS=5
for PACKAGE in drews-xcode-mcp xcode-mcp-server; do
    VERIFIED=false
    for (( ATTEMPT=1; ATTEMPT<=MAX_ATTEMPTS; ATTEMPT++ )); do
        if curl -sf "https://pypi.org/pypi/$PACKAGE/$NEW_VERSION/json" > /dev/null 2>&1; then
            VERIFIED=true
            break
        fi
        if [ "$ATTEMPT" -lt "$MAX_ATTEMPTS" ]; then
            echo "   $PACKAGE $NEW_VERSION not visible yet (attempt $ATTEMPT/$MAX_ATTEMPTS), retrying in 5s..."
            sleep 5
        fi
    done
    if [ "$VERIFIED" = true ]; then
        echo "✅ $PACKAGE $NEW_VERSION confirmed on PyPI"
    else
        echo "⚠️  Could not verify $PACKAGE $NEW_VERSION on PyPI."
        echo "Skipping auto-commit. Please verify manually and commit the version change."
        exit 1
    fi
done
echo ""

# Commit and push the version bump
echo "📝 Committing version bump..."
git add drews_xcode_mcp/__init__.py
git add shim/pyproject.toml
git add dist
git add dist-archive
git commit -m "v$NEW_VERSION"
git push
echo ""

echo "✅ Deployment complete! v$NEW_VERSION is live."
echo ""
echo "Test the deployed version with:"
echo ""
echo "    uvx drews-xcode-mcp"
echo ""
echo "And the legacy-name shim with:"
echo ""
echo "    uvx xcode-mcp-server==$NEW_VERSION"
echo ""
echo "Clear uv cache to have it picked up immediately:"
echo "    uv cache clean drews-xcode-mcp"
echo ""

exit 0
