#!/usr/bin/env python3
"""set_run_destination tool - Set the active run destination in Xcode"""

import json
import os
import time

from drews_xcode_mcp.server import mcp, TOOL_MUTATING_IDEMPOTENT
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_and_normalize_project_path
from drews_xcode_mcp.exceptions import InvalidParameterError, XCodeMCPError
from drews_xcode_mcp.utils.xcodebuild_query import resolve_active_destination_id
from drews_xcode_mcp.utils.applescript import (
    build_open_and_wait_applescript,
    escape_applescript_string,
    run_applescript,
    show_notification,
    show_result_notification,
    show_error_notification,
)

# Xcode writes its workspace state to disk lazily, so get_active_run_destination
# (which reads that state) keeps reporting the previous destination for a short
# while after the destination is set. Wait for the write to land before
# returning, so the new destination is observable to whatever the caller does next.
ACTIVE_DESTINATION_POLL_INTERVAL_SECONDS = 0.25
ACTIVE_DESTINATION_CONFIRMATION_TIMEOUT_SECONDS = 5.0


def _wait_for_active_destination(project_path: str, destination_id: str) -> bool:
    """
    Poll Xcode's workspace state until it reports destination_id as active.

    Returns True once the state file agrees, or False if it still does not
    within ACTIVE_DESTINATION_CONFIRMATION_TIMEOUT_SECONDS.
    """
    deadline = time.monotonic() + ACTIVE_DESTINATION_CONFIRMATION_TIMEOUT_SECONDS
    target = destination_id.casefold()
    while True:
        active_id = resolve_active_destination_id(project_path)
        if active_id and active_id.casefold() == target:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(ACTIVE_DESTINATION_POLL_INTERVAL_SECONDS)


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
        destination_id: The destination identifier (UDID) to select. This must be the
            'id' field from list_run_destinations output, for example
            "E1A98967-DE69-4DF3-8ED4-8715BA6F566C". The destination's 'name' cannot be
            used here: only the UDID is matched.

    Returns:
        JSON with the name and id of the destination that was set, plus
        'active_destination_confirmed': true once Xcode's on-disk workspace
        state reports the new destination as active (waited on for up to 5
        seconds), or false if that write had not landed yet, in which case
        get_active_run_destination may briefly still report the old
        destination.
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

    normalized_dest_id = destination_id.strip()
    confirmed = _wait_for_active_destination(normalized_path, normalized_dest_id)

    result = {
        "name": dest_name,
        "id": normalized_dest_id,
        "active_destination_confirmed": confirmed,
    }
    if not confirmed:
        result["note"] = (
            "Xcode accepted the destination, but had not yet written it to its "
            f"workspace state after {ACTIVE_DESTINATION_CONFIRMATION_TIMEOUT_SECONDS:g} "
            "seconds. get_active_run_destination may still report the previous "
            "destination for a short while."
        )

    return json.dumps(result, indent=2)
