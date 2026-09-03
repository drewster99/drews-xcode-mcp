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
from drews_xcode_mcp.utils.applescript import show_result_notification
from drews_xcode_mcp.utils.xcodebuild_query import read_active_run_destination


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

    Note: Xcode flushes this state to disk lazily. set_run_destination waits for
    that write and reports 'active_destination_confirmed': true when it lands, so
    a call made after a confirmed set reflects the new destination. If that field
    came back false, this may still report the previous destination briefly.

    Args:
        project_path: Path to an Xcode project (.xcodeproj) or workspace (.xcworkspace).

    Returns:
        JSON with the active destination's name, id, scheme and 'sdk' (the SDK
        Xcode records it under, e.g. "iphonesimulator" — not the same vocabulary
        as the 'platform' field of list_run_destinations), plus 'architecture',
        'sdk_variant' ("macos" for My Mac, "iosmac" for Mac Catalyst; "My Mac
        (Designed for iPad)" carries none, being the Mac's id under the iphoneos
        platform) and 'os' when known, and 'identifier', the raw stored value.
        Raises an error if the active destination cannot be determined (e.g. the
        project has never been opened in Xcode).
    """
    normalized_path = validate_and_normalize_project_path(project_path, "Getting active destination for")
    project_name = os.path.basename(normalized_path)

    scheme, destination = read_active_run_destination(normalized_path)

    # Try to get a friendly name and OS version
    name, os_version = _lookup_simulator_info(destination.id)
    if not name:
        name = destination.id

    result = {
        "name": name,
        "id": destination.id,
        "scheme": scheme,
        "sdk": destination.sdk,
    }
    if destination.sdk_variant:
        result["sdk_variant"] = destination.sdk_variant
    if destination.architecture:
        result["architecture"] = destination.architecture
    if os_version:
        result["os"] = os_version
    result["identifier"] = destination.identifier

    show_result_notification(f"Active: {name}", project_name)
    return json.dumps(result, indent=2)
