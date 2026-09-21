#!/usr/bin/env python3
"""get_active_run_destination tool - Get the currently active run destination"""

import json
import os

from drews_xcode_mcp.server import mcp, TOOL_READONLY
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_and_normalize_project_path
from drews_xcode_mcp.utils.applescript import show_result_notification
from drews_xcode_mcp.utils.xcodebuild_query import lookup_simulator_info, read_active_run_destination


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

    Note: Xcode stores a destination per scheme, and both this tool and
    set_run_destination resolve the same selected scheme from Xcode's own
    workspace state. Xcode flushes that state lazily, so set_run_destination
    waits for the write and reports 'active_destination_confirmed': true when it
    lands; after a confirmed set this reports the new destination. If that field
    came back false, this may still report the previous one briefly.

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
    name, os_version = lookup_simulator_info(destination.id)
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
