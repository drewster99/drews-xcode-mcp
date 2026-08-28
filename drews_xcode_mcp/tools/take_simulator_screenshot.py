#!/usr/bin/env python3
"""take_simulator_screenshot tool - Screenshot iOS simulator"""

import os
import sys
import subprocess
from typing import Optional

from drews_xcode_mcp.server import mcp, TOOL_READONLY
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.exceptions import XCodeMCPError
from drews_xcode_mcp.utils.applescript import show_result_notification, show_error_notification
from drews_xcode_mcp.utils.screenshot import _get_booted_simulators, get_screenshot_path


def _describe_booted_simulators(booted_simulators) -> str:
    """Render booted simulators as one line each, so an error can name the choices."""
    return "\n".join(
        f"  • {sim['name']} ({sim['os']}) — {sim['udid']}" for sim in booted_simulators
    )


@mcp.tool(annotations=TOOL_READONLY)
@describe_parameters_from_docstring
@apply_config
def take_simulator_screenshot(udid: Optional[str] = None) -> str:
    """
    Take a screenshot of a booted iOS simulator.

    Args:
        udid: UDID (device identifier) of the simulator to screenshot, exactly as
              reported by `list_booted_simulators` (for example
              "D9A710C8-CEF1-4C5B-8CCA-0CB97DCABE2C"). May be omitted only when a
              single simulator is booted; when several are booted it is required,
              and the call fails listing the candidates rather than guessing.

    Returns:
        The file path to the saved screenshot.

    Raises:
        XCodeMCPError: If no booted simulators found, if `udid` was omitted while
            several simulators are booted, or if the screenshot fails.
    """
    try:
        target_udid = None
        target_name = "Unknown"

        if udid and udid.strip():
            # User specified a UDID - use it directly without checking booted list
            # xcrun simctl will fail appropriately if it's not booted
            target_udid = udid.strip()

            # Try to get the name for better logging (optional)
            try:
                booted_simulators = _get_booted_simulators()
                for sim in booted_simulators:
                    if sim['udid'] == target_udid:
                        target_name = sim['name']
                        break
            except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
                print(f"warn: friendly-name lookup for {target_udid} failed: {e}", file=sys.stderr)
        else:
            booted_simulators = _get_booted_simulators()

            if not booted_simulators:
                error_msg = "No booted simulators"
                show_error_notification(error_msg)
                raise XCodeMCPError("No booted simulators found")

            # Choosing among several would silently screenshot a device the caller
            # never named, and the caller cannot tell that from a correct result.
            if len(booted_simulators) > 1:
                error_msg = f"{len(booted_simulators)} simulators booted"
                show_error_notification(error_msg, "udid required")
                raise XCodeMCPError(
                    f"{len(booted_simulators)} simulators are booted, so 'udid' is required. "
                    "Pass the UDID of the one to screenshot:\n"
                    f"{_describe_booted_simulators(booted_simulators)}"
                )

            target_udid = booted_simulators[0]['udid']
            target_name = booted_simulators[0]['name']

        screenshot_path = get_screenshot_path("simulator")

        print(f"Taking screenshot of '{target_name}' (UDID: {target_udid})", file=sys.stderr)

        # Take the screenshot
        result = subprocess.run(
            ['xcrun', 'simctl', 'io', target_udid, 'screenshot', screenshot_path],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            error_msg = result.stderr.strip()
            # Provide more helpful error messages
            if 'Invalid device' in error_msg:
                show_error_notification("Invalid simulator UDID", target_udid)
                raise XCodeMCPError(f"Simulator with UDID '{target_udid}' does not exist")
            elif 'not booted' in error_msg.lower():
                show_error_notification("Simulator not booted", target_udid)
                raise XCodeMCPError(f"Simulator with UDID '{target_udid}' is not booted")
            else:
                show_error_notification("Failed to take screenshot", error_msg)
                raise XCodeMCPError(f"Failed to take screenshot: {error_msg}")

        # Verify the file was created
        if not os.path.exists(screenshot_path):
            error_msg = "Screenshot failed"
            show_error_notification(error_msg, "File not created")
            raise XCodeMCPError("Screenshot file was not created")

        print(f"Screenshot saved to: {screenshot_path}", file=sys.stderr)
        show_result_notification(f"Screenshotting {target_name}")
        return screenshot_path

    except subprocess.TimeoutExpired:
        error_msg = "Screenshot timeout"
        show_error_notification(error_msg)
        raise XCodeMCPError("Timeout while taking screenshot")
    except XCodeMCPError:
        # Every path that raises XCodeMCPError above already showed a specific
        # notification, so re-raise without notifying again — a second generic
        # "Screenshot failed" would double up on the same error.
        raise
    except Exception as e:
        show_error_notification("Screenshot failed", str(e))
        raise XCodeMCPError(f"Error taking screenshot: {e}")
