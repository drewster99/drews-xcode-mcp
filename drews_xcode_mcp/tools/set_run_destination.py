#!/usr/bin/env python3
"""set_run_destination tool - Set the active run destination in Xcode"""

import json
import os
import time
from typing import Optional, Tuple

from drews_xcode_mcp.server import mcp, TOOL_MUTATING_IDEMPOTENT
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_and_normalize_project_path
from drews_xcode_mcp.exceptions import InvalidParameterError, XCodeMCPError
from drews_xcode_mcp.utils.xcodebuild_query import (
    RunDestinationIdentifier,
    read_workspace_run_state,
    select_active_run_destination,
)
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
#
# Measured against Xcode 26.5, that write consistently lands 4.99-5.23s after
# the destination is set, so the timeout has to clear 5s by a wide margin or an
# ordinary success would report itself as unconfirmed. The wait returns as soon
# as the write appears, so the headroom costs nothing when it is not needed.
CONFIRMATION_POLL_INTERVAL_SECONDS = 0.25
CONFIRMATION_TIMEOUT_SECONDS = 15.0

# Each state read gets the remaining budget as its timeout so a stalled read
# cannot keep the poll going indefinitely, but never less than this: a read
# starved of time reports "could not be read" for what is only a short budget,
# which would turn the last attempt of every unconfirmed wait into a misleading
# diagnosis. The floor is why an unconfirmed wait can run a little past
# CONFIRMATION_TIMEOUT_SECONDS -- the last read still gets its full floor.
MINIMUM_STATE_READ_SECONDS = 2.0

# Prefixes of the report the AppleScript returns, one field per line.
_SCHEME_PREFIX = "SCHEME:"
_MATCH_PREFIX = "MATCH:"


def _wait_for_stored_destination(
    project_path: str,
    destination_id: str,
    scheme: str,
) -> Tuple[bool, str]:
    """
    Poll Xcode's workspace state until it stores destination_id for scheme.

    `scheme` is the scheme Xcode has selected, whose key the write lands under;
    when it is "" (Xcode would not report it) the state's own notion of the
    active scheme is used instead. Returns (confirmed, detail), where detail
    says what was actually observed if the destination never showed up — never
    a guess at why.
    """
    deadline = time.monotonic() + CONFIRMATION_TIMEOUT_SECONDS
    target = destination_id.casefold()

    while True:
        remaining_seconds = deadline - time.monotonic()
        observed, detail = _read_stored_destination(
            project_path, scheme, max(remaining_seconds, MINIMUM_STATE_READ_SECONDS)
        )
        if observed is not None and observed.id.casefold() == target:
            return True, ""
        if time.monotonic() >= deadline:
            return False, detail
        time.sleep(CONFIRMATION_POLL_INTERVAL_SECONDS)


def _read_stored_destination(
    project_path: str,
    scheme: str,
    timeout_seconds: float,
) -> Tuple[Optional[RunDestinationIdentifier], str]:
    """
    Read the destination Xcode has stored for a scheme.

    Returns (destination, detail): the stored destination and a description of
    it, or None and the reason it could not be read. Never raises — a
    confirmation that cannot be made must not turn an already-applied
    destination change into a tool failure.
    """
    try:
        state = read_workspace_run_state(project_path, timeout_seconds=timeout_seconds)
        if scheme:
            selected_scheme = scheme
            destination = state.destinations.get(scheme)
        else:
            selected_scheme, destination = select_active_run_destination(state, project_path)
    except Exception as error:
        # Deliberately broad: this helper only reports whether a change that has
        # already been applied is visible yet, so nothing it does may fail the
        # tool. The reason travels to the caller in the note instead.
        return None, f"Xcode's workspace state could not be read: {error}"

    if destination is None:
        return None, f"Xcode's workspace state has no run destination for scheme '{selected_scheme}'"

    return destination, (
        f"Xcode's workspace state still reports '{destination.identifier}' "
        f"for scheme '{selected_scheme}'"
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
        destination_id: The destination identifier (UDID) to select. This must be the
            'id' field from list_run_destinations output, for example
            "E1A98967-DE69-4DF3-8ED4-8715BA6F566C". The destination's 'name' cannot be
            used here: only the UDID is matched.

    Returns:
        JSON with the name and id of the destination that was set, the scheme it
        was set for, and 'active_destination_confirmed': true once Xcode's
        on-disk workspace state reports it (polled for up to 15 seconds), or
        false with a 'note' saying what was observed instead — in which case
        get_active_run_destination may still report the old destination.
        A Mac reports one device identifier for several destinations ("My Mac",
        "My Mac (Designed for iPad)", Mac Catalyst); when the id matches more
        than one the first is selected and all are listed under
        'matched_destinations'.
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
    set matchedNames to {{}}

    repeat with d in dests
        try
            set devId to device identifier of (device of d)
            if devId is equal to targetDeviceId then
                if foundDest is missing value then
                    set foundDest to d
                end if
                set end of matchedNames to name of d
            end if
        end try
    end repeat

    if foundDest is missing value then
        error "No run destination found with identifier: " & targetDeviceId
    end if

    set active run destination of workspaceDoc to foundDest

    -- The write lands under the selected scheme's key in the workspace state,
    -- so report which scheme that is. Xcode can refuse this while busy.
    set activeSchemeName to ""
    try
        set activeSchemeName to name of active scheme of workspaceDoc
    end try

    set report to "{_SCHEME_PREFIX}" & activeSchemeName
    repeat with matchedName in matchedNames
        set report to report & linefeed & "{_MATCH_PREFIX}" & (contents of matchedName)
    end repeat
    return report
end tell
'''

    success, output = run_applescript(script)

    if not success:
        show_error_notification(f"Failed to set destination", output)
        raise XCodeMCPError(f"Failed to set run destination: {output}")

    active_scheme = ""
    matched_names = []
    for line in output.splitlines():
        if line.startswith(_SCHEME_PREFIX):
            active_scheme = line[len(_SCHEME_PREFIX):].strip()
        elif line.startswith(_MATCH_PREFIX):
            matched_names.append(line[len(_MATCH_PREFIX):].strip())

    # The destination is set at this point; a report we cannot read back costs
    # only the friendly name, so fall back to the id the caller already knows.
    dest_name = matched_names[0] if matched_names else destination_id.strip()
    show_result_notification(f"Destination: {dest_name}", project_name)

    normalized_dest_id = destination_id.strip()
    confirmed, detail = _wait_for_stored_destination(
        normalized_path, normalized_dest_id, active_scheme
    )

    result = {
        "name": dest_name,
        "id": normalized_dest_id,
        "active_destination_confirmed": confirmed,
    }
    if active_scheme:
        result["scheme"] = active_scheme
    if len(matched_names) > 1:
        result["matched_destinations"] = matched_names
    if not confirmed:
        result["note"] = (
            f"Xcode accepted the destination, but it was not observable in Xcode's "
            f"workspace state within {CONFIRMATION_TIMEOUT_SECONDS:g} seconds: {detail}."
        )

    return json.dumps(result, indent=2)
