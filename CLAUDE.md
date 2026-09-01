# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is Drew's Xcode MCP Server (PyPI package `drews-xcode-mcp`, module `drews_xcode_mcp`) - a Model Context Protocol (MCP) server that enables AI assistants to interact with Xcode projects. It provides tools for building, running, and managing Xcode projects/workspaces programmatically through AppleScript.

The package was originally published as `xcode-mcp-server`. That PyPI name now hosts a compatibility shim (see `shim/`) that depends on `drews-xcode-mcp` with an exact version pin and forwards the old `xcode-mcp-server` command to it. `deploy.sh` and `deploy-beta.sh` publish both packages in lockstep; the shim's version and pin are rewritten automatically at deploy time. The settings directory is `~/.drews-xcode-mcp`; on first run, a pre-rename `~/.xcode-mcp-server` directory is adopted via rename (see `ConfigManager._migrate_legacy_config_dir`). The cache directory deliberately keeps the old name (`~/Library/Caches/xcode-mcp-server`) — it holds only transient logs/screenshots, and log paths already handed out in prior tool results should stay valid. When launched through the legacy name, the shim sets `XCODE_MCP_LEGACY_PACKAGE_NAME`, and the server adds migration guidance to its instructions, the `version` tool, and a `migrate_to_drews_xcode_mcp` MCP prompt.

## Development Commands

### Local Development
```bash
# Quick start with dev script (sets up venv and runs MCP Inspector)
./dev.sh

# Manual setup: Test the server locally with MCP Inspector
export XCODEMCP_ALLOWED_FOLDERS=/Users/username/Projects
mcp dev drews_xcode_mcp/__main__.py

# Run the server directly
python -m drews_xcode_mcp
```

### Testing in-progress changes through the real MCP path

Two MCP servers are registered against this repo:

- **`drews-xcode-mcp`** — the *deployed* build (unpinned `uvx drews-xcode-mcp`,
  registered at user scope). It runs the published PyPI release, NOT your
  working tree. Editing source here does nothing to it until you `./deploy.sh`
  and the server respawns. Unpinned uvx re-resolves "latest" on spawn using
  uv's HTTP-cached view of the PyPI index (max-age 600s), so a respawn within
  ~10 minutes of a deploy can still launch the previous version.
- **`xcode-mcp-local-dev-server`** — runs your **live local source** via
  `run_local_for_claude.sh` (`cd` into the repo, then `python -m drews_xcode_mcp`).
  Use this to test uncommitted changes without deploying. (Note: the canonical
  repo path `~/cursor/drews-xcode-mcp` and `~/Documents/ncc_source/cursor/drews-xcode-mcp`
  are the same directory via symlink.)

**Critical freshness rule:** a Claude instance spawns the dev-server process once,
when it first connects, and Python imports each module once per process. So the
dev-server bound to a *running* Claude session is **frozen at that session's
start** — editing source afterward does NOT update it. Per-tool-call does not
re-import; only a brand-new server process picks up edits.

To test the latest source after editing, pick one:

1. **Spawn a fresh Claude instance from bash** (best for end-to-end MCP-path
   testing — each invocation starts its own dev-server process = current source):
   ```bash
   claude -p "Call mcp__xcode-mcp-local-dev-server__list_project_tests with
   project_path=... and report the result verbatim." \
     --allowedTools "mcp__xcode-mcp-local-dev-server__list_project_tests"
   ```
2. **Call the tool function directly via `python3 -c`** (fastest for iterating on
   tool logic; imports fresh source each run, but bypasses FastMCP/serialization):
   ```bash
   python3 -c "
   import drews_xcode_mcp.security as sec
   sec.ALLOWED_FOLDERS = {'/path/allowed'}
   from drews_xcode_mcp.tools.<tool_module> import <tool_fn>
   fn = getattr(<tool_fn>, 'fn', <tool_fn>)   # unwrap the FastMCP tool
   print(fn('/path/to/Project.xcodeproj'))
   "
   ```

Do NOT trust the dev-server tools exposed in your *current* session to reflect
edits you just made — they reflect the source as of when this session connected.

**Confirming the running server matches your edits.** When running from a source
checkout, the `version` tool appends a source fingerprint, e.g.
`Drew's Xcode MCP Server (drews-xcode-mcp) version 1.3.14b1 (dev source e5f07eab)`. The hash is a SHA-256
of all `.py` files in the package, computed at import — so it identifies the
source the process actually loaded. Deployed (uvx) builds have no `.git` and omit
the suffix. To verify a server is running your current code, compare its reported
hash to the on-disk hash:
```bash
python3 -c "
import hashlib, os
d = hashlib.sha256()
for root, dirs, files in os.walk('drews_xcode_mcp'):
    dirs[:] = sorted(x for x in dirs if x != '__pycache__')
    for n in sorted(files):
        if n.endswith('.py'):
            with open(os.path.join(root, n), 'rb') as h: d.update(h.read())
print(d.hexdigest()[:8])
"
```
If the `version` tool's hash differs from this, the server is running stale code
(start a fresh Claude instance). If they match, it is your current source.

### Testing
```bash
# Run tests using the test runner framework
python tests/test_basic.py      # Basic functionality tests
python tests/test_build.py      # Build operation tests
python tests/test_runner.py     # Test infrastructure

# Tests automatically:
# - Set up isolated working directory (test_projects/working/)
# - Copy template projects from test_projects/templates/
# - Configure ALLOWED_FOLDERS to working directory
# - Clean up after execution
```

### Build and Deploy
```bash
# Deploy to PyPI (increments version, builds, and uploads)
./deploy.sh

# Manual version increment
hatch version patch  # 1.2.0 -> 1.2.1
hatch version minor  # 1.2.0 -> 1.3.0
hatch version major  # 1.2.0 -> 2.0.0

# Build distribution manually
python -m build

# Install locally for testing
pip install -e .
```

### Testing with uvx
```bash
# Run the published version
uvx drews-xcode-mcp

# Run with specific allowed folders
XCODEMCP_ALLOWED_FOLDERS=/path/to/projects uvx drews-xcode-mcp
```

## Architecture

### Core Components

1. **Main Entry Point** (`drews_xcode_mcp/__init__.py`)
   - Handles command-line argument parsing
   - Manages allowed folder configuration from environment and CLI args
   - Validates security settings before server startup

2. **MCP Server Implementation** (`drews_xcode_mcp/server.py` and `drews_xcode_mcp/tools/`)
   - Built with FastMCP framework
   - Implements the MCP tools for Xcode interaction (one module per tool under `drews_xcode_mcp/tools/`; the module list there is authoritative):
     - **Project discovery**: `version`, `get_xcode_projects`, `create_project`
     - **File system**: `get_directory_tree`, `get_directory_listing`
     - **Build operations**: `get_project_schemes`, `build_project`, `clean_project`, `stop_project`, `get_build_errors`, `get_build_results`
     - **Run destinations**: `list_run_destinations`, `set_run_destination`, `get_active_run_destination`
     - **Runtime**: `run_project_with_user_interaction`, `run_project_until_terminated`, `run_project_unmonitored`, `get_runtime_output`
     - **Testing**: `list_project_tests`, `run_project_tests`, `get_latest_test_results`
     - **Screenshots**: `take_xcode_screenshot`, `take_simulator_screenshot`, `take_window_screenshot`, `take_app_screenshot`
     - **System info**: `list_booted_simulators`, `list_running_mac_apps`, `list_mac_app_windows`
     - **Debug**: `debug_list_notification_history`

### Security Model

The server implements path-based security:
- **ALLOWED_FOLDERS**: Set of validated absolute paths where access is permitted
- Paths are validated for: absolute paths, existence, directory type, no '..' components
- Default to $HOME if no folders specified
- Every tool call validates the project path against allowed folders

### AppleScript Integration

All Xcode interactions use AppleScript via `osascript`:
- Opens projects/workspaces in Xcode
- Waits for workspace loading (60-second timeout)
- Handles build/run/clean operations
- Extracts build errors from Xcode's UI

### Error Handling

Custom exception hierarchy:
- `XCodeMCPError`: Base exception class
- `AccessDeniedError`: Path access violations
- `InvalidParameterError`: Invalid input parameters

### Tool Argument Contract

Two rules hold for every tool, enforced centrally rather than per-tool. Both
exist because a caller that gets an argument name wrong must be told, not
quietly given a default.

**Unknown arguments are rejected.** FastMCP builds each tool's argument model
from its signature, and pydantic's default `extra="ignore"` silently discards a
key matching no parameter — so a misspelled optional argument was
indistinguishable from omitting it, and the tool ran with the parameter's
default. `strict_tool_arguments.enforce_strict_tool_arguments()` sets
`extra="forbid"` on `ArgModelBase` and is called from `server.py` before any
`@mcp.tool()` registration (the tools modules import `server`, so that ordering
is automatic). Every published schema therefore carries
`additionalProperties: false`. The function reaches into `mcp` internals, so it
raises if `ArgModelBase` moves and re-verifies by building a probe model and
confirming it actually rejects an undeclared key — the protection can never
lapse silently on an `mcp` or `pydantic` upgrade.

**Parameter descriptions come from the docstring.** The `Args:` block is the
single source; `docstring_parameters.describe_parameters_from_docstring`
(applied to every tool, between `@mcp.tool()` and `@apply_config`) parses it and
attaches `Field(description=...)` so the text reaches the JSON Schema. Without
it a parameter published only an auto-derived title such as `"Udid"`, leaving
the machine-readable contract silent about what the argument means. Do NOT
restate a description in an `Annotated[..., Field(description=...)]` — that
creates a second copy to drift; write it in the docstring only. An explicit
`Field(description=...)` is honored and left alone if a tool ever needs to
override. A parameter absent from the `Args:` block raises at import, so the
server will not start while a parameter is undocumented.

### Configuration Overrides Are Absolute

`apply_config` (`config_manager.py`) applies `parameter_overrides` from
`~/.drews-xcode-mcp/*.json` on top of whatever the caller passed. This is
deliberate and is NOT to be "fixed": a configured override is a hard user
override, and nothing overrides it -- not a caller-supplied argument, not a
tool default. Do not add a "only fill in omitted parameters" precedence rule.

The consequence for tests: a tool's own parameter validation cannot be
exercised through `apply_config`, because an override replaces the value under
test and the guard never runs. Worse, the outcome then depends on whoever's
config is on the machine, so the test is not hermetic. Tests that assert a
tool rejects a bad argument must call the undecorated function
(`XcodeMCPTestRunner.run_tool_bypassing_config`), which is the only thing that
sees the caller's value unmodified.

## Key Implementation Details

- **Notifications**: Optional macOS notifications for tool invocations (--show-notifications flag)
- **Scheme Handling**: Active scheme detection with fallback to scheme list
- **Build Output**: Captures first 25 lines of build errors for concise feedback
- **Path Normalization**: Removes trailing slashes, validates absolute paths
- **Spotlight Integration**: Uses `mdfind` for efficient project discovery across allowed folders

## Version Management

Version is stored in `drews_xcode_mcp/__init__.py` and managed by hatch. The `deploy.sh` script automatically increments the patch version, but you can manually control it with:
```bash
hatch version patch  # Increment patch version
hatch version minor  # Increment minor version
hatch version major  # Increment major version
```

## Important Implementation Details

### Internal Helper Functions
- **`get_frontmost_project()`**: Not exposed as MCP tool; retrieves the currently open Xcode project path from frontmost window
- **`extract_console_logs_from_xcresult()`**: Parses .xcresult bundles to extract runtime console output as structured JSON
- **`extract_build_errors_and_warnings()`**: Filters build logs to show only errors/warnings as structured JSON (configurable via `include_warnings` parameter)
- **`extract_test_results_from_xcresult()`**: Parses .xcresult test bundles to extract concise test results with failure details
- **`wait_for_xcresult_after_timestamp()`**: Polls for new .xcresult files after a run starts, with timeout
- **`find_xcresult_for_project()`**: Locates the most recent .xcresult bundle for a project (runtime logs)
- **`find_xcresult_bundle()`**: Locates the most recent .xcresult bundle for a project (test logs)
- **`validate_and_normalize_project_path()`**: Ensures paths are absolute, exist, and are allowed by security policy
- **`escape_applescript_string()`**: Properly escapes strings for safe AppleScript execution
- **`utils/scheme_action.py`**: Reads and interprets Xcode scheme action results. `SchemeActionStatus` mirrors Xcode's `scheme action result status` enumeration; `build_action_result_report_applescript()` / `build_action_result_report_tail_applescript()` emit the status + error message + build log in one round trip; `parse_action_result_report()` parses it; `describe_build_failure()` decides whether a *run* action's failure was a build failure; `report_build_failure()` formats a known-failed *build* action

### Operational Behavior
- **Scheme Selection**: When no scheme is specified in `build_project`, the active scheme is used automatically
- **Debug Output**: Server prints debug information to stderr for troubleshooting
- **Workspace Loading**: All operations wait for Xcode workspace to fully load before proceeding (60-second timeout)
- **Build Log Filtering**: Build failures return structured JSON with errors/warnings (up to 25 lines) and full log path
- **Run Outcome Detection**: `run` in Xcode means build-and-run, so a run whose build fails still reports `completed`. All three run tools therefore read `status of <scheme action result>`, never `completed` alone. `run_project_with_user_interaction` and `run_project_until_terminated` inspect the action's build log after it completes and return build errors instead of runtime logs when the build failed; `run_project_unmonitored` returns before the app exits and so can never observe its run action's outcome, so it builds explicitly first and only dispatches `run` if that build succeeded. A failure the build log cannot explain (app crashed, nonzero exit) keeps the runtime logs and attaches a `scheme_action` block to the JSON rather than discarding them
- **Test Result Filtering**: Test results return structured JSON with summary and only failed test details; passing tests are counted but not detailed
- **Warning Control**: Global `BUILD_WARNINGS_ENABLED` and `BUILD_WARNINGS_FORCED` flags control warning display; can be overridden per-tool with `include_warnings` parameter
- **Notifications**: Optional macOS notifications via `osascript` (controlled by `NOTIFICATIONS_ENABLED` global flag)

### xcresult Management
The server relies heavily on Xcode's .xcresult bundles for extracting build, runtime, and test information:
- **Runtime logs**: Located in `~/Library/Developer/Xcode/DerivedData/*/Logs/Launch/`
- **Test results**: Located in `~/Library/Developer/Xcode/DerivedData/*/Logs/Test/`
- Parsed using `xcrun xcresulttool` with appropriate subcommands
- Timestamped checking prevents reading stale results
- **Runtime output**: Console logs extracted from `actionsInvocationRecord` → `actions` → `actionResult` → `logRef` paths, returned as structured JSON with errors/warnings prioritized
- **Console output location differs by Xcode version**: `xcresulttool get log --type console` reads the bundle's `ConsoleLogSection`. Xcode 27 (bundle format 3.60) moved runtime console output to a new `ConsoleSessionSection` and left that section a stub, so the command succeeds while returning zero items — runs appeared to produce no output. `extract_console_logs_from_xcresult` therefore falls back to reading `consoleSessionRef` via the deprecated `xcresulttool get object --legacy` when the supported command yields nothing. The supported path stays primary, so this stops being used automatically if a later Xcode repopulates `ConsoleLogSection`. See "Console Output Moved in Xcode 27" in TODO.md
- **Test results**: Test tree parsed recursively to extract pass/fail counts and failure details, returned as concise structured JSON (10-50x smaller than raw xcresult output)
- **Build errors**: Filtered using regex patterns, returned as structured JSON with full log path for deep analysis