#!/usr/bin/env python3
"""take_xcode_screenshot tool - Screenshot Xcode window"""

import os
import sys
import subprocess

from drews_xcode_mcp.server import mcp, TOOL_READONLY
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_and_normalize_project_path
from drews_xcode_mcp.exceptions import XCodeMCPError
from drews_xcode_mcp.utils.applescript import (
    escape_applescript_string,
    run_applescript,
    show_result_notification,
    show_error_notification,
)
from drews_xcode_mcp.utils.screenshot import get_screenshot_path


@mcp.tool(annotations=TOOL_READONLY)
@describe_parameters_from_docstring
@apply_config
def take_xcode_screenshot(project_path: str) -> str:
    """
    Take a screenshot of the Xcode window for the specified project.

    Args:
        project_path: Path to an Xcode project/workspace directory.

    Returns:
        The file path to the saved screenshot.

    Raises:
        XCodeMCPError: If Xcode window is not found or screenshot fails.
    """
    # Validate and normalize path
    normalized_path = validate_and_normalize_project_path(project_path, "Taking Xcode screenshot for")
    escaped_path = escape_applescript_string(normalized_path)

    try:
        # Get the workspace name (used as window title in Xcode)
        workspace_name = os.path.basename(normalized_path)
        escaped_workspace_name = escape_applescript_string(workspace_name)

        # Get the window ID via AppleScript
        script = f'''
        tell application "Xcode"
            -- First, try to find the window by exact path match
            repeat with w in windows
                try
                    if path of document of w is "{escaped_path}" then
                        return id of w
                    end if
                end try
            end repeat

            -- If not found by path, try by name (less reliable but fallback)
            try
                return id of window "{escaped_workspace_name}"
            on error
                error "No Xcode window found for project: {escaped_workspace_name}"
            end try
        end tell
        '''

        success, window_id = run_applescript(script)
        if not success:
            show_error_notification("Failed to get Xcode window", window_id)
            raise XCodeMCPError(f"Failed to get Xcode window: {window_id}")

        window_id = window_id.strip()
        if not window_id:
            show_error_notification("No Xcode window found", workspace_name)
            raise XCodeMCPError(f"No Xcode window found for project: {workspace_name}")

        print(f"Found Xcode window with ID: {window_id}", file=sys.stderr)

        screenshot_path = get_screenshot_path("xcode")

        print(f"Taking screenshot of Xcode window for '{workspace_name}'", file=sys.stderr)

        # Capture the screenshot using screencapture
        result = subprocess.run(
            ["screencapture", "-l", window_id, "-x", "-o", screenshot_path],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            show_error_notification("Failed to capture screenshot", result.stderr)
            raise XCodeMCPError(f"Failed to capture screenshot: {result.stderr}")

        # Verify the file was created
        if not os.path.exists(screenshot_path):
            error_msg = "Screenshot failed"
            show_error_notification(error_msg, "File not created")
            raise XCodeMCPError("Screenshot file was not created")

        print(f"Screenshot saved to: {screenshot_path}", file=sys.stderr)
        show_result_notification(f"Screenshotting Xcode {workspace_name}")
        return screenshot_path

    except subprocess.TimeoutExpired:
        error_msg = "Screenshot timeout"
        show_error_notification(error_msg)
        raise XCodeMCPError("Timeout while taking screenshot")
    except Exception as e:
        if isinstance(e, XCodeMCPError):
            if "not found" not in str(e).lower():
                show_error_notification("Screenshot failed", str(e))
            raise
        error_msg = "Screenshot failed"
        show_error_notification(error_msg, str(e))
        raise XCodeMCPError(f"Error taking Xcode screenshot: {e}")
