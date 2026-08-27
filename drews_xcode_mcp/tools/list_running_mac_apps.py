#!/usr/bin/env python3
"""list_running_mac_apps tool - List running macOS applications"""

from drews_xcode_mcp.server import mcp, TOOL_READONLY
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.exceptions import XCodeMCPError
from drews_xcode_mcp.utils.applescript import show_notification, run_applescript, show_result_notification, show_error_notification


@mcp.tool(annotations=TOOL_READONLY)
@describe_parameters_from_docstring
@apply_config
def list_running_mac_apps() -> str:
    """
    List all currently running macOS applications.

    Returns:
        A formatted list of running applications with their name, bundle ID,
        and status flags (frontmost/visible/hidden).
    """
    show_notification("Drew's Xcode MCP", message="Listing running macOS applications")

    try:
        # AppleScript joins records with US (ASCII 0x1f) instead of the default
        # ", " so app names containing a comma or comma-space parse correctly.
        script = '''
        set recordSep to (ASCII character 31)
        tell application "System Events"
            set appList to {}
            set runningApps to every application process

            repeat with anApp in runningApps
                set appName to name of anApp
                set appBundleID to bundle identifier of anApp
                set appPID to unix id of anApp
                set appFrontmost to frontmost of anApp
                set appVisible to visible of anApp

                -- Format as tab-separated values for easy parsing
                set appInfo to appName & tab & appBundleID & tab & appPID & tab & appFrontmost & tab & appVisible
                set end of appList to appInfo
            end repeat

            set AppleScript's text item delimiters to recordSep
            set joinedOutput to appList as string
            set AppleScript's text item delimiters to ""
            return joinedOutput
        end tell
        '''

        success, output = run_applescript(script)

        if not success:
            show_error_notification("Failed to list running apps", output)
            raise XCodeMCPError(f"Failed to list running apps: {output}")

        apps = []
        lines = output.strip().split('\x1f')

        for line in lines:
            if not line.strip():
                continue

            parts = line.split('\t')
            if len(parts) >= 5:
                app_name = parts[0]
                bundle_id = parts[1] if parts[1] != 'missing value' else 'N/A'
                pid = parts[2]
                is_frontmost = parts[3] == 'true'
                is_visible = parts[4] == 'true'

                apps.append({
                    'name': app_name,
                    'bundle_id': bundle_id,
                    'pid': pid,
                    'is_frontmost': is_frontmost,
                    'is_visible': is_visible,
                    'is_hidden': not is_visible
                })

        # Sort by name for consistent output
        apps.sort(key=lambda x: x['name'].lower())

        if not apps:
            show_result_notification("No running applications found")
            return "No running applications found"

        # Format output
        show_result_notification(f"Found {len(apps)} running app{'s' if len(apps) != 1 else ''}")
        output_lines = [f"Found {len(apps)} running application(s):", ""]

        for app in apps:
            status_flags = []
            if app['is_frontmost']:
                status_flags.append("FRONTMOST")
            if app['is_visible']:
                status_flags.append("VISIBLE")
            if app['is_hidden']:
                status_flags.append("HIDDEN")
            status = f" [{', '.join(status_flags)}]" if status_flags else ""

            output_lines.append(f"• {app['name']}{status}")
            output_lines.append(f"  Bundle ID: {app['bundle_id']}")
            output_lines.append(f"  PID: {app['pid']}")
            output_lines.append("")

        return "\n".join(output_lines)

    except Exception as e:
        if isinstance(e, XCodeMCPError):
            # XCodeMCPError already has error notification from line 50
            raise
        show_error_notification("Error listing applications", str(e))
        raise XCodeMCPError(f"Error listing applications: {e}")
