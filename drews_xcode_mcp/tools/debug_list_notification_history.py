#!/usr/bin/env python3
"""debug_list_notification_history tool - List all notifications that have been posted"""

import os
import subprocess
import sys

from drews_xcode_mcp.server import mcp, TOOL_READONLY
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.utils.applescript import get_notification_history
from drews_xcode_mcp.utils.paths import DEBUG_DIR

# Stable path under the per-user cache directory so repeated invocations
# overwrite a single file instead of leaking files on each call, and the
# contents are not world-readable like /tmp.
NOTIFICATION_HISTORY_FILE = os.path.join(DEBUG_DIR, "notification-history.txt")


@mcp.tool(annotations=TOOL_READONLY)
@describe_parameters_from_docstring
@apply_config
def debug_list_notification_history() -> str:
    """
    List all notifications that have been posted since the server started.
    This is a debugging tool to help understand notification behavior.

    Returns:
        A formatted list of all notifications with timestamps, titles, messages, and subtitles.
    """
    history = get_notification_history()

    if not history:
        result = "No notifications have been posted yet."
    else:
        lines = [f"Notification History ({len(history)} notification{'s' if len(history) != 1 else ''}):", ""]

        for i, notif in enumerate(history, 1):
            lines.append(f"{i}. [{notif['timestamp']}]")
            lines.append(f"   Title: {notif['title']}")
            if notif['subtitle']:
                lines.append(f"   Subtitle: {notif['subtitle']}")
            if notif['message']:
                lines.append(f"   Message: {notif['message']}")
            lines.append(f"   Sound: {notif['sound']}")
            lines.append("")

        result = "\n".join(lines)

    # Also show in TextEdit (scrollable). Write to a stable path so repeated
    # invocations don't leak temp files.
    try:
        os.makedirs(os.path.dirname(NOTIFICATION_HISTORY_FILE), exist_ok=True)
        with open(NOTIFICATION_HISTORY_FILE, 'w') as f:
            f.write(result)
    except OSError as e:
        print(f"warn: failed to write {NOTIFICATION_HISTORY_FILE}: {e}", file=sys.stderr)
        return result

    try:
        subprocess.run(
            ['open', '-a', 'TextEdit', NOTIFICATION_HISTORY_FILE],
            capture_output=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        print("warn: `open -a TextEdit` timed out", file=sys.stderr)
    except FileNotFoundError:
        print("warn: `open` binary not found on PATH", file=sys.stderr)

    return result
