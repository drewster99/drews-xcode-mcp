#!/usr/bin/env python3
"""get_active_run_destination tool - Get the currently active run destination"""

import json
import os
import subprocess
import sys

from drews_xcode_mcp.server import mcp, TOOL_READONLY
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_and_normalize_project_path
from drews_xcode_mcp.exceptions import XCodeMCPError
from drews_xcode_mcp.utils.applescript import show_result_notification
from drews_xcode_mcp.utils.xcodebuild_query import (
    get_active_scheme,
    find_xcuserstate,
    decode_active_destinations,
)


def _lookup_simulator_info(udid: str) -> tuple:
    """
    Look up a simulator name and OS version by UDID using xcrun simctl.
    Returns (name, os_version) or ("", "").
    """
    try:
        result = subprocess.run(
            ['xcrun', 'simctl', 'list', 'devices', udid],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        print(f"warn: simctl list devices timed out for {udid}", file=sys.stderr)
        return ("", "")
    except FileNotFoundError:
        print("warn: `xcrun` binary not found on PATH", file=sys.stderr)
        return ("", "")

    if result.returncode != 0:
        print(
            f"warn: simctl list devices exited {result.returncode}: "
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )
        return ("", "")

    current_os = ""
    for line in result.stdout.split('\n'):
        stripped = line.strip()
        # Track OS version from section headers like "-- iOS 26.4 --"
        if stripped.startswith('-- ') and stripped.endswith(' --'):
            current_os = stripped[3:-3]  # e.g. "iOS 26.4"
        elif udid in stripped:
            paren_idx = stripped.find('(')
            if paren_idx > 0:
                name = stripped[:paren_idx].strip()
                return (name, current_os)
    return ("", "")


@mcp.tool(annotations=TOOL_READONLY)
@describe_parameters_from_docstring
@apply_config
def get_active_run_destination(
    project_path: str,
) -> str:
    """
    Get the currently active run destination for a project.

    Returns the device or simulator that Xcode will use for the next build or
    run operation. This reads from Xcode's workspace state file without opening
    the project in Xcode.

    Note: After calling set_run_destination, Xcode may take several seconds to
    flush its state to disk. If called immediately after set_run_destination,
    this may return the previous destination.

    Args:
        project_path: Path to an Xcode project (.xcodeproj) or workspace (.xcworkspace).

    Returns:
        JSON with the active destination's name, platform, architecture, and id.
        Returns an error message if the active destination cannot be determined
        (e.g. the project has never been opened in Xcode).
    """
    normalized_path = validate_and_normalize_project_path(project_path, "Getting active destination for")
    project_name = os.path.basename(normalized_path)

    # Find the xcuserstate file
    xcuserstate = find_xcuserstate(normalized_path)
    if not xcuserstate:
        raise XCodeMCPError(
            "No workspace state file found. The project may not have been "
            "opened in Xcode yet."
        )

    # Decode the active destinations per scheme
    scheme_destinations = decode_active_destinations(xcuserstate)
    if not scheme_destinations:
        raise XCodeMCPError(
            "Could not determine active run destination. The project may not "
            "have been built or run yet."
        )

    # Determine which scheme's destination to report
    active_scheme = get_active_scheme(normalized_path)

    dest_string = None
    if active_scheme and active_scheme in scheme_destinations:
        dest_string = scheme_destinations[active_scheme]
    elif scheme_destinations:
        # Fall back to first scheme's destination
        active_scheme = next(iter(scheme_destinations))
        dest_string = scheme_destinations[active_scheme]

    if not dest_string:
        raise XCodeMCPError("No active run destination found in workspace state.")

    # Parse the destination string (format: "UDID_platform_arch"). Architecture
    # can itself contain underscores (e.g. watchOS uses `arm64_32`), so split
    # with maxsplit=2 to preserve the full architecture string.
    parts = dest_string.split('_', 2)
    if len(parts) < 3:
        raise XCodeMCPError(f"Unexpected destination format: {dest_string}")

    target_udid = parts[0]
    platform = parts[1]
    architecture = parts[2]

    # Try to get a friendly name and OS version
    name, os_version = _lookup_simulator_info(target_udid)
    if not name:
        name = target_udid

    result = {
        "name": name,
        "platform": platform,
        "architecture": architecture,
        "id": target_udid,
    }
    if os_version:
        result["os"] = os_version
    if active_scheme:
        result["scheme"] = active_scheme

    show_result_notification(f"Active: {name}", project_name)
    return json.dumps(result, indent=2)
