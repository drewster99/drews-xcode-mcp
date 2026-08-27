#!/usr/bin/env python3
"""list_mac_app_windows tool - List macOS application windows"""

import os
import tempfile
import subprocess

from drews_xcode_mcp.server import mcp, TOOL_READONLY
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.exceptions import XCodeMCPError
from drews_xcode_mcp.utils.applescript import show_result_notification, show_error_notification


@mcp.tool(annotations=TOOL_READONLY)
@describe_parameters_from_docstring
@apply_config
def list_mac_app_windows() -> str:
    """
    List all on-screen macOS application windows with their CGWindow IDs.
    These window IDs can be used to capture screenshots of a given window
    or app with `take_app_screenshot` or `take_window_screenshot`.

    Returns:
        A formatted list of windows grouped by application, including window IDs
        that can be used with `take_window_screenshot`.
    """
    try:
        # Use Swift to get window information via CoreGraphics
        swift_code = '''
import Cocoa
import CoreGraphics

// Get all on-screen windows
let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
guard let windowList = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else {
    print("ERROR: Failed to get window list")
    exit(1)
}

// Group windows by app and filter out system UI elements
var appWindows: [String: [(id: Int, title: String, pid: Int)]] = [:]

for window in windowList {
    let windowID = window[kCGWindowNumber as String] as? Int ?? 0
    let appName = window[kCGWindowOwnerName as String] as? String ?? "Unknown"
    let windowTitle = window[kCGWindowName as String] as? String ?? ""
    let windowLayer = window[kCGWindowLayer as String] as? Int ?? 0
    let ownerPID = window[kCGWindowOwnerPID as String] as? Int ?? 0

    // Skip menu bar items and system UI (layer 0 is normal windows)
    // Also skip windows without titles
    if windowLayer == 0 && !windowTitle.isEmpty {
        if appWindows[appName] == nil {
            appWindows[appName] = []
        }
        appWindows[appName]?.append((id: windowID, title: windowTitle, pid: ownerPID))
    }
}

// Output as structured format for parsing
for (app, windows) in appWindows.sorted(by: { $0.key < $1.key }) {
    print("APP:\\(app)")
    for window in windows {
        print("WINDOW:\\(window.id)\\t\\(window.pid)\\t\\(window.title)")
    }
}
'''

        # Use a TemporaryDirectory so cleanup is guaranteed by the OS even
        # if we exit the context on an unhandled error (SIGKILL still leaks,
        # but normal errors no longer require finally bookkeeping).
        with tempfile.TemporaryDirectory(prefix='xcode-mcp-swift-') as tmpdir:
            temp_file = os.path.join(tmpdir, 'list_windows.swift')
            with open(temp_file, 'w') as f:
                f.write(swift_code)

            result = subprocess.run(
                ['swift', temp_file],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode != 0:
                show_error_notification("Failed to get window list", result.stderr)
                raise XCodeMCPError(f"Failed to get window list: {result.stderr}")

            output = result.stdout

        # Check for error
        if output.startswith("ERROR:"):
            error_msg = output.replace("ERROR: ", "")
            show_error_notification("Failed to get window list", error_msg)
            raise XCodeMCPError(error_msg)

        # Parse the output
        apps_with_windows = {}
        current_app = None

        for line in output.strip().split('\n'):
            if line.startswith('APP:'):
                current_app = line[4:]
                apps_with_windows[current_app] = []
            elif line.startswith('WINDOW:') and current_app:
                parts = line[7:].split('\t', 2)
                if len(parts) >= 3:
                    try:
                        window_id = int(parts[0])
                        pid = int(parts[1])
                    except ValueError:
                        # Swift emitter always produces ints; skip any
                        # malformed line rather than poison the list.
                        continue
                    title = parts[2]
                    apps_with_windows[current_app].append({
                        'id': window_id,
                        'pid': pid,
                        'title': title
                    })

        if not apps_with_windows:
            show_result_notification("No visible windows found")
            return "No visible windows found"

        # Format output - one line per window
        output_lines = []
        total_windows = sum(len(windows) for windows in apps_with_windows.values())
        show_result_notification(f"Found {total_windows} window{'s' if total_windows != 1 else ''}", f"{len(apps_with_windows)} app{'s' if len(apps_with_windows) != 1 else ''}")
        output_lines.append(f"Found {total_windows} window(s) across {len(apps_with_windows)} application(s):")
        output_lines.append("")

        # Sort windows by app name for consistent output
        for app_name, windows in sorted(apps_with_windows.items()):
            for window in windows:
                output_lines.append(f"Window ID {window['id']} - \"{window['title']}\" - App PID {window['pid']} - \"{app_name}\"")

        output_lines.append("")
        output_lines.append("Use with `take_window_screenshot`.")

        return "\n".join(output_lines)

    except Exception as e:
        if isinstance(e, XCodeMCPError):
            # XCodeMCPError already has error notification from line 83
            raise
        show_error_notification("Error listing windows", str(e))
        raise XCodeMCPError(f"Error listing windows: {e}")
