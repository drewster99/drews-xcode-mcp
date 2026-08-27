#!/usr/bin/env python3
"""take_app_screenshot tool - Screenshot all windows of an app"""

import os
import subprocess

from drews_xcode_mcp.server import mcp, TOOL_READONLY
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.exceptions import XCodeMCPError
from drews_xcode_mcp.utils.applescript import show_result_notification, show_error_notification
from drews_xcode_mcp.utils.screenshot import _get_all_windows, get_screenshot_path


@mcp.tool(annotations=TOOL_READONLY)
@describe_parameters_from_docstring
@apply_config
def take_app_screenshot(app_name: str) -> str:
    """
    Take screenshots of all windows for an app (case-insensitive substring match).
    If the app has more than one window, screenshots will be taken for up to 5 of them.

    Note: Only apps with at least one on-screen window can be found by this tool.

    Args:
        app_name: Full or partial app name to match.

    Returns:
        Path(s) to saved screenshot file(s), one per line (max 5 windows).
        If multiple apps match, returns an error with the full window list.

    Raises:
        XCodeMCPError: If no matching app found, multiple apps match, or screenshot fails.
    """
    try:
        # Get all windows
        windows_data = _get_all_windows()

        # Find matching apps (case-insensitive substring match)
        search_term = app_name.lower()
        matching_apps = {}

        for app, windows in windows_data.items():
            if search_term in app.lower():
                matching_apps[app] = windows

        if not matching_apps:
            error_msg = f"App not found: {app_name}"
            show_error_notification(error_msg)
            raise XCodeMCPError(f"No apps found matching '{app_name}'")

        # If multiple apps match, return error with window list
        if len(matching_apps) > 1:
            show_error_notification("Multiple apps match", f"{len(matching_apps)} apps match '{app_name}'")
            output_lines = [f"Multiple apps match '{app_name}'. Please be more specific:"]
            output_lines.append("")

            total_windows = sum(len(windows) for windows in matching_apps.values())
            output_lines.append(f"Found {total_windows} window(s) across {len(matching_apps)} matching application(s):")
            output_lines.append("")

            for app, windows in sorted(matching_apps.items()):
                for window in windows:
                    output_lines.append(f"Window ID {window['id']} - \"{window['title']}\" - App PID {window['pid']} - \"{app}\"")

            raise XCodeMCPError("\n".join(output_lines))

        # Single app matched - take screenshots of all its windows (max 5)
        app_matched = list(matching_apps.keys())[0]
        windows = matching_apps[app_matched]

        if not windows:
            show_error_notification("No visible windows", app_matched)
            raise XCodeMCPError(f"App '{app_matched}' has no visible windows")

        # Limit to 5 windows
        windows = windows[:5]

        # Take screenshots
        screenshot_paths = []

        for window in windows:
            screenshot_path = get_screenshot_path("app")

            # Take the screenshot using screencapture (-x flag disables sound)
            result = subprocess.run(
                ['screencapture', '-x', '-l', str(window['id']), screenshot_path],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode != 0:
                show_error_notification("Failed to capture window", f"Window {window['id']}")
                raise XCodeMCPError(f"Failed to capture window {window['id']}: {result.stderr}")

            # Verify file was created
            if not os.path.exists(screenshot_path):
                error_msg = "Screenshot failed"
                show_error_notification(error_msg, f"Window {window['id']}")
                raise XCodeMCPError(f"Screenshot file was not created for window {window['id']}")

            screenshot_paths.append(screenshot_path)

        # Show success notification
        show_result_notification(f'Screenshotting "{app_matched}"')

        return "\n".join(screenshot_paths)

    except Exception as e:
        if isinstance(e, XCodeMCPError):
            if "not found" not in str(e).lower() and "Multiple apps" not in str(e):
                show_error_notification("Screenshot failed", str(e))
            raise
        error_msg = "Screenshot failed"
        show_error_notification(error_msg, str(e))
        raise XCodeMCPError(f"Error taking app screenshot: {e}")
