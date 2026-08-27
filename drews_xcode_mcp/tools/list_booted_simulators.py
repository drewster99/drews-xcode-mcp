#!/usr/bin/env python3
"""list_booted_simulators tool - List running iOS simulators"""

import subprocess

from drews_xcode_mcp.server import mcp, TOOL_READONLY
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.exceptions import XCodeMCPError
from drews_xcode_mcp.utils.applescript import show_result_notification, show_error_notification
from drews_xcode_mcp.utils.screenshot import _get_booted_simulators


@mcp.tool(annotations=TOOL_READONLY)
@describe_parameters_from_docstring
@apply_config
def list_booted_simulators() -> str:
    """
    List all currently booted iOS, iPadOS, tvOS, and watchOS simulators.

    Returns:
        A formatted list of booted simulators with their names, UDIDs, and OS versions.
        Returns "No booted simulators found" if none are running.
    """
    try:
        booted_simulators = _get_booted_simulators()

        if not booted_simulators:
            show_result_notification("No booted simulators")
            return "No booted simulators found"

        # Show notification with simulator summary
        count = len(booted_simulators)
        first_sim = booted_simulators[0]['name']
        if count == 1:
            show_result_notification(f"Found {first_sim}")
        else:
            details = f"{first_sim}"
            if count > 1:
                details += f"\n+{count - 1} more"
            show_result_notification(f"Found {count} simulators", details)

        # Format output
        output_lines = [f"Found {len(booted_simulators)} booted simulator(s):", ""]

        for sim in booted_simulators:
            output_lines.append(f"• {sim['name']}")
            output_lines.append(f"  UDID: {sim['udid']}")
            output_lines.append(f"  OS: {sim['os']}")
            output_lines.append("")

        return "\n".join(output_lines)

    except subprocess.TimeoutExpired:
        error_msg = "Timeout listing simulators"
        show_error_notification(error_msg)
        raise XCodeMCPError("Timeout while listing simulators")
    except Exception as e:
        if isinstance(e, XCodeMCPError):
            raise
        error_msg = "Error listing simulators"
        show_error_notification(error_msg, str(e))
        raise XCodeMCPError(f"Error listing simulators: {e}")
