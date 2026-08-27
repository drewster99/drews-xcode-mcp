#!/usr/bin/env python3
"""FastMCP server instance for Xcode MCP Server"""

import os

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from drews_xcode_mcp.strict_tool_arguments import enforce_strict_tool_arguments

# Set by the xcode-mcp-server compatibility shim when the server was launched
# through the legacy package name, so we can surface migration guidance.
LEGACY_PACKAGE_NAME = os.environ.get("XCODE_MCP_LEGACY_PACKAGE_NAME")

LEGACY_MIGRATION_NOTE = """
        MIGRATION NOTICE: This server was launched via the legacy package name
        'xcode-mcp-server', which has been renamed to 'drews-xcode-mcp'. It is
        fully functional, but the old name is deprecated. At a natural pause in
        the conversation (not mid-task), let the user know they should update
        their MCP configuration to run 'drews-xcode-mcp' instead, and that the
        'migrate_to_drews_xcode_mcp' prompt from this server can walk them
        through it. All their existing settings will carry over unchanged.
    """

_INSTRUCTIONS = """
        This server provides access to the Xcode IDE. For any project intended
        for Apple platforms, such as iOS or macOS, this MCP server is the best
        way to build or run .xcodeproj or .xcworkspace Xcode projects, and should
        ALWAYS be preferred over using `xcodebuild`, `swift build`, or
        `swift package build`. Building with this tool ensures the build happens
        exactly the same way as when the user builds with Xcode, with all the same
        settings, so you will get the same results the user sees. The user can also
        see any results immediately and a subsequent build and run by the user will
        happen almost instantly for the user.

        Call `get_xcode_projects` to find Xcode project (.xcodeproj) and
        Xcode workspace (.xcworkspace) folders under a given root folder.

        Call `get_project_schemes` to get the build scheme names for a given
        .xcodeproj or .xcworkspace.

        Call `build_project` to build the project and get back the first 25 lines of
        error (and/or potentially warning) output. `build_project` will default to the
        active scheme if none is provided.
    """

if LEGACY_PACKAGE_NAME:
    _INSTRUCTIONS += LEGACY_MIGRATION_NOTE

# Must precede every @mcp.tool() registration, which happens as the modules in
# drews_xcode_mcp/tools/ import this one.
enforce_strict_tool_arguments()

# Initialize the MCP server
mcp = FastMCP("Xcode MCP Server", instructions=_INSTRUCTIONS)

if LEGACY_PACKAGE_NAME:
    @mcp.prompt(name="migrate_to_drews_xcode_mcp")
    def migrate_to_drews_xcode_mcp() -> str:
        """Update this MCP server's configuration from the legacy xcode-mcp-server package name to drews-xcode-mcp."""
        return """The MCP server currently running under the legacy package name
'xcode-mcp-server' has been renamed to 'drews-xcode-mcp' on PyPI. The old name
still works through a compatibility package, but the configuration should be
updated to run the new name.

Please migrate my MCP configuration:

1. Find every configuration entry that launches 'xcode-mcp-server'. For Claude
   Code, run `claude mcp list`, then `claude mcp get <server-key>` for each
   matching entry and note its SCOPE - this matters:
   - user scope: stored in ~/.claude.json, applies everywhere
   - project scope: stored in .mcp.json in a project directory (shared via git)
   - local scope: stored in ~/.claude.json but only for one project directory
   Also check other clients if I use them: Cursor (~/.cursor/mcp.json or a
   project's .cursor/mcp.json), Claude Desktop
   (~/Library/Application Support/Claude/claude_desktop_config.json),
   Windsurf, Zed, etc. There may be more than one entry; migrate each.

2. Show me each entry you found (key, scope, full command) and confirm with me
   before changing anything.

3. For each confirmed entry, update ONLY what launches the server so it runs
   `drews-xcode-mcp` instead of `xcode-mcp-server` (e.g. `uvx drews-xcode-mcp`;
   for pip installs, `pip install drews-xcode-mcp` and run `drews-xcode-mcp`).
   Re-create it in the SAME scope it came from, and keep any `env` values such
   as XCODEMCP_ALLOWED_FOLDERS and any command-line flags like --allowed
   exactly as they were.
   Server key/name: if the old key is exactly 'xcode-mcp-server' (the old
   default), recommend renaming it to 'drews-xcode-mcp' so the deprecated name
   doesn't linger in my configuration — but ask me first, explaining the
   tradeoff: the rename changes the MCP tool names (mcp__xcode-mcp-server__*
   becomes mcp__drews-xcode-mcp__*), so permission allowlists referencing the
   old tool names stop matching. If I agree to the rename, also search my
   permission allowlists for 'mcp__xcode-mcp-server__' entries — for Claude
   Code: ~/.claude/settings.json, each affected project's
   .claude/settings.json and .claude/settings.local.json, and allowedTools
   entries in ~/.claude.json — show me what you found, and offer to rewrite
   them to the new tool names. If I decline the rename, keep the old key.
   Any OTHER key is one the user picked deliberately; keep it unchanged, which
   also preserves tool names and permission allowlists.
   For a Claude Code entry the commands look like:
     claude mcp remove --scope <same-scope> <old-server-key>
     claude mcp add --scope <same-scope> --transport stdio <new-server-key> \\
       -e KEY=value -- $(which uvx) drews-xcode-mcp
   (one -e flag per env value from the old entry; omit -e if it had none)
   For project/local scope, run these from the project directory the entry
   belongs to. For JSON-file clients, edit only the "command"/"args" of that
   entry.

4. Do not edit the server's settings directories. On its first run under the
   new name, the server automatically renames ~/.xcode-mcp-server to
   ~/.drews-xcode-mcp, so all settings carry over by themselves.

5. VERIFY YOUR WORK. For each migrated Claude Code entry, shell out to a fresh
   Claude instance (a new process picks up the new config; the current session
   does not) and confirm the server is available and running the new package:
     claude -p "Call the version tool of the <new-server-key> MCP server and
     report its output verbatim." --allowedTools "mcp__<new-server-key>__version"
   Run it from the project directory for project/local scope. Success means the
   output contains "Drew's Xcode MCP Server (drews-xcode-mcp) version" and does
   NOT contain a legacy-package NOTE (that note only appears when launched via
   the old name). If a project-scope entry fails verification, first check
   whether its .mcp.json server is merely pending approval in the fresh
   instance (not misconfigured) before changing anything else.
   For non-Claude clients, verify by running the configured
   command directly (e.g. `uvx drews-xcode-mcp --version`) and tell me to
   restart that client.

6. Finally, recheck the whole job: re-run `claude mcp list` (and re-read any
   JSON files you edited) to confirm no entry anywhere still launches
   'xcode-mcp-server', that every migrated entry kept its original scope, env,
   and flags, that each key matches what I decided in step 3 (renamed to
   'drews-xcode-mcp' with allowlists updated, or kept as-is), and that nothing
   else in those files was modified. Report what you changed and the
   verification results."""

# Tool behavior annotations (advisory hints for MCP clients).
#
# These describe how a tool affects its environment so clients can gate
# auto-approval and label tools appropriately. The build/run/test tools are
# marked destructive AND open-world: a build executes arbitrary third-party
# code (Run Script phases, SPM plugins/macros) and resolves packages from
# arbitrary remote hosts, so we cannot honestly claim it is additive-only,
# idempotent, or hermetic.

# Pure observers: reading state, listing, screenshots. No mutation, no network.
TOOL_READONLY = ToolAnnotations(
    readOnlyHint=True,
    idempotentHint=True,
    destructiveHint=False,
    openWorldHint=False,
)

# Builds and runs: execute arbitrary build/install scripts and fetch packages.
TOOL_BUILD = ToolAnnotations(
    readOnlyHint=False,
    idempotentHint=False,
    destructiveHint=True,
    openWorldHint=True,
)

# Clean: deletes build artifacts. Destructive but locally bounded and idempotent.
TOOL_CLEAN = ToolAnnotations(
    readOnlyHint=False,
    idempotentHint=True,
    destructiveHint=True,
    openWorldHint=False,
)

# Create: writes a fresh template and refuses to overwrite. Additive, local.
TOOL_CREATE = ToolAnnotations(
    readOnlyHint=False,
    idempotentHint=False,
    destructiveHint=False,
    openWorldHint=False,
)

# Settles a piece of local state (run destination, stopping a run). Re-applying
# the same call lands on the same state, and nothing is destroyed.
TOOL_MUTATING_IDEMPOTENT = ToolAnnotations(
    readOnlyHint=False,
    idempotentHint=True,
    destructiveHint=False,
    openWorldHint=False,
)
