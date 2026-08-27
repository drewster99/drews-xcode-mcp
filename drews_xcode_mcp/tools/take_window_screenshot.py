#!/usr/bin/env python3
"""take_window_screenshot tool - Screenshot macOS windows"""

import os
import subprocess

from drews_xcode_mcp.server import mcp, TOOL_READONLY
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.exceptions import XCodeMCPError
from drews_xcode_mcp.utils.applescript import show_result_notification, show_error_notification
from drews_xcode_mcp.utils.screenshot import _get_all_windows, get_screenshot_path

# Cap on how many windows a single title-substring query will screenshot. A
# broad substring can match many windows; without a cap that's slow and a
# privacy footgun. Mirrors take_app_screenshot's behavior.
MAX_WINDOW_MATCHES = 5


@mcp.tool(annotations=TOOL_READONLY)
@describe_parameters_from_docstring
@apply_config
def take_window_screenshot(window_id_or_name: str) -> str:
    """
    Take a screenshot of a window by ID or name (case-insensitive substring match).
    Window IDs can be obtained by calling `list_mac_app_windows`, or you can simply
    pass a partial (or complete) window title, like "News" for the News app.
    If multiple windows match the provided name, screenshots will be taken for up to
    the first 5 of them.

    Note: Only on-screen windows can be found by name.

    Args:
        window_id_or_name: Window ID number or partial window title to match.

    Returns:
        Path(s) to saved screenshot file(s), one per line if multiple matches.

    Raises:
        XCodeMCPError: If no matching windows found or screenshot fails.
    """
    try:
        # First, get all windows
        windows_data = _get_all_windows()

        matches = []

        # Try to interpret as window ID first
        try:
            target_id = int(window_id_or_name)
            for app_name, windows in windows_data.items():
                for window in windows:
                    if window['id'] == target_id:
                        matches.append((window['id'], window['title'], app_name))
                        break
                if matches:
                    break
        except ValueError:
            # Not a number, search by title substring (case-insensitive)
            search_term = window_id_or_name.lower()
            for app_name, windows in windows_data.items():
                for window in windows:
                    if search_term in window['title'].lower():
                        matches.append((window['id'], window['title'], app_name))

        if not matches:
            error_msg = f"Window not found: {window_id_or_name}"
            show_error_notification(error_msg)
            raise XCodeMCPError(f"No windows found matching '{window_id_or_name}'")

        # Cap the number of matches we screenshot (see MAX_WINDOW_MATCHES).
        total_matches = len(matches)
        matches = matches[:MAX_WINDOW_MATCHES]

        # Take screenshots
        screenshot_paths = []

        for window_id, window_title, app_name in matches:
            screenshot_path = get_screenshot_path("window")

            # Take the screenshot using screencapture (-x flag disables sound)
            result = subprocess.run(
                ['screencapture', '-x', '-l', str(window_id), screenshot_path],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode != 0:
                show_error_notification("Failed to capture window", f"Window {window_id}")
                raise XCodeMCPError(f"Failed to capture window {window_id}: {result.stderr}")

            # Verify file was created
            if not os.path.exists(screenshot_path):
                error_msg = "Screenshot failed"
                show_error_notification(error_msg, f"Window {window_id}")
                raise XCodeMCPError(f"Screenshot file was not created for window {window_id}")

            screenshot_paths.append(screenshot_path)

        # Show success notification
        if len(matches) == 1:
            window_title = matches[0][1]
            show_result_notification(f'Screenshotting "{window_title}"')
        else:
            show_result_notification(f"Screenshotting {len(matches)} windows")

        result = "\n".join(screenshot_paths)
        if total_matches > len(matches):
            result += (
                f"\n\n[Matched {total_matches} windows; captured the first "
                f"{len(matches)}. Use a more specific window title or a window ID.]"
            )
        return result

    except Exception as e:
        if isinstance(e, XCodeMCPError):
            if "not found" not in str(e).lower():
                show_error_notification("Screenshot failed", str(e))
            raise
        error_msg = "Screenshot failed"
        show_error_notification(error_msg, str(e))
        raise XCodeMCPError(f"Error taking window screenshot: {e}")
