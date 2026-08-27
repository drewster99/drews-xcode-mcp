#!/usr/bin/env python3
"""set_run_destination tool - Set the active run destination in Xcode"""

import json
import os

from drews_xcode_mcp.server import mcp, TOOL_MUTATING_IDEMPOTENT
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_and_normalize_project_path
from drews_xcode_mcp.exceptions import InvalidParameterError, XCodeMCPError
from drews_xcode_mcp.utils.applescript import (
    build_open_and_wait_applescript,
    escape_applescript_string,
    run_applescript,
    show_notification,
    show_result_notification,
    show_error_notification,
)


@mcp.tool(annotations=TOOL_MUTATING_IDEMPOTENT)
@describe_parameters_from_docstring
@apply_config
def set_run_destination(
    project_path: str,
    destination_id: str,
) -> str:
    """
    Set the active run destination (device or simulator) in Xcode.

    Use list_run_destinations to get available destination IDs, then pass the
    desired 'id' value here to select it. Subsequent build and run operations
    will target this destination.

    Args:
        project_path: Path to an Xcode project (.xcodeproj) or workspace (.xcworkspace).
        destination_id: The destination identifier to select. This is the 'id' field
            from list_run_destinations output (e.g. a simulator UDID or device UDID).

    Returns:
        JSON with the name and id of the destination that was set.
    """
    if not destination_id or not destination_id.strip():
        raise InvalidParameterError("destination_id cannot be empty")

    normalized_path = validate_and_normalize_project_path(project_path, "Setting destination for")
    escaped_path = escape_applescript_string(normalized_path)
    escaped_dest_id = escape_applescript_string(destination_id.strip())
    project_name = os.path.basename(normalized_path)

    show_notification("Setting Destination", project_name, destination_id)

    script = build_open_and_wait_applescript(escaped_path) + f'''
    set targetDeviceId to "{escaped_dest_id}"
    set dests to run destinations of workspaceDoc
    set foundDest to missing value
    set foundName to ""

    repeat with d in dests
        try
            set devId to device identifier of (device of d)
            if devId is equal to targetDeviceId then
                set foundDest to d
                set foundName to name of d
                exit repeat
            end if
        end try
    end repeat

    if foundDest is missing value then
        error "No run destination found with identifier: " & targetDeviceId
    end if

    set active run destination of workspaceDoc to foundDest
    return foundName
end tell
'''

    success, output = run_applescript(script)

    if not success:
        show_error_notification(f"Failed to set destination", output)
        raise XCodeMCPError(f"Failed to set run destination: {output}")

    dest_name = output.strip()
    show_result_notification(f"Destination: {dest_name}", project_name)

    return json.dumps({
        "name": dest_name,
        "id": destination_id.strip(),
    }, indent=2)
