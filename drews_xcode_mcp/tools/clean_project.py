#!/usr/bin/env python3
"""clean_project tool - Clean an Xcode project"""

import os
from typing import Optional

from drews_xcode_mcp.server import mcp, TOOL_CLEAN
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_and_normalize_project_path
from drews_xcode_mcp.exceptions import XCodeMCPError
from drews_xcode_mcp.utils.applescript import (
    build_open_and_wait_applescript,
    build_wait_for_completion_applescript,
    is_action_timeout,
    resolve_build_timeout,
    format_timeout_duration,
    escape_applescript_string,
    run_applescript,
    show_result_notification,
    show_warning_notification,
    show_error_notification,
)


@mcp.tool(annotations=TOOL_CLEAN)
@describe_parameters_from_docstring
@apply_config
def clean_project(project_path: str, timeout: Optional[int] = None) -> str:
    """
    Clean the specified Xcode project or workspace.

    Args:
        project_path: Path to an Xcode project/workspace directory.
        timeout: Maximum seconds to wait for the clean to complete. If not
            provided, defaults to 600. Must be a positive integer.

    Returns:
        Output message
    """
    # Validate and normalize path
    normalized_path = validate_and_normalize_project_path(project_path, "Cleaning")
    escaped_path = escape_applescript_string(normalized_path)
    effective_timeout = resolve_build_timeout(timeout)

    # `clean workspaceDoc` returns a scheme-action-result that completes
    # asynchronously (same as build/test), so capture it and poll `completed`
    # rather than assuming the command blocks. This bounds the wait by
    # `effective_timeout` and avoids reporting success before the clean has
    # actually finished.
    script = build_open_and_wait_applescript(escaped_path) + (
        '    set actionResult to clean workspaceDoc\n'
        + build_wait_for_completion_applescript("actionResult", effective_timeout, action_name="Clean")
        + '    return "Clean completed successfully"\n'
        'end tell\n'
    )

    # The script polls inside AppleScript for up to effective_timeout; the
    # subprocess timeout must exceed that, plus a buffer for workspace load.
    success, output = run_applescript(script, timeout=effective_timeout + 60)

    project_name = os.path.basename(normalized_path)

    if success:
        show_result_notification("Clean completed", project_name)
        return output

    # The AppleScript poll loop raises (rather than returning) on timeout, so a
    # timeout surfaces here as a failed subprocess. Detect it by the helper's
    # error number and report it as a clean-specific timeout instead of leaking
    # the wait-helper's wording or treating it as a hard failure.
    if is_action_timeout(output):
        duration = format_timeout_duration(effective_timeout)
        show_warning_notification(f"Clean timeout ({duration})")
        return f"⏳ Clean did not complete within {duration}"

    show_error_notification("Clean failed", project_name)
    raise XCodeMCPError(f"Clean failed: {output}")
