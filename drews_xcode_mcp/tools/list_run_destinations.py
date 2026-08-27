#!/usr/bin/env python3
"""list_run_destinations tool - List available run destinations for a scheme"""

import json
import os
import subprocess
from typing import Optional

from drews_xcode_mcp.server import mcp, TOOL_READONLY
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_and_normalize_project_path
from drews_xcode_mcp.exceptions import XCodeMCPError
from drews_xcode_mcp.utils.applescript import (
    show_notification,
    show_result_notification,
)
from drews_xcode_mcp.utils.xcodebuild_query import (
    project_flag_for,
    get_first_scheme,
    parse_destination_line,
)


@mcp.tool(annotations=TOOL_READONLY)
@describe_parameters_from_docstring
@apply_config
def list_run_destinations(
    project_path: str,
    scheme: Optional[str] = None,
    include_incompatible_destinations: bool = False,
) -> str:
    """
    List available run destinations (devices and simulators) for a project scheme.

    By default, only destinations the scheme can actually build and run for are
    returned. xcodebuild also reports destinations whose platform doesn't match
    the scheme (e.g. every iOS simulator for a macOS-only app); each carries an
    'error' explaining the mismatch. Those are excluded unless
    include_incompatible_destinations is True. Excluding them can drop dozens of
    simulator entries for a single-platform app.

    Use the 'id' field from the results with set_run_destination to change
    which device Xcode builds and runs for.

    Args:
        project_path: Path to an Xcode project (.xcodeproj) or workspace (.xcworkspace).
        scheme: Scheme name to list destinations for. If not provided, uses the
            first scheme found via xcodebuild.
        include_incompatible_destinations: If True, also include destinations
            whose platform is incompatible with the scheme (each with an 'error'
            field describing why). Defaults to False.

    Returns:
        JSON array of destinations, each with: name, platform, id, and optionally
        arch, OS, error, and variant fields.
    """
    normalized_path = validate_and_normalize_project_path(project_path, "Listing destinations for")
    project_name = os.path.basename(normalized_path)

    # Determine the xcodebuild flag based on project type
    project_flag = project_flag_for(normalized_path)

    # Get scheme name if not provided
    if not scheme:
        scheme = get_first_scheme(normalized_path)
        if not scheme:
            raise XCodeMCPError(
                f"Could not determine scheme for {project_name}. "
                "Please provide a scheme name."
            )

    show_notification("Listing Destinations", project_name, f"Scheme: {scheme}")

    try:
        result = subprocess.run(
            ['xcodebuild', '-showdestinations', project_flag, normalized_path, '-scheme', scheme],
            capture_output=True, text=True, timeout=30,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            # Look for a clear error message from xcodebuild
            for err_line in result.stderr.split('\n'):
                if 'error:' in err_line:
                    raise XCodeMCPError(err_line.strip())
            raise XCodeMCPError(f"xcodebuild failed for scheme '{scheme}'")
    except subprocess.TimeoutExpired:
        raise XCodeMCPError("xcodebuild -showdestinations timed out after 30 seconds")
    except XCodeMCPError:
        raise
    except Exception as e:
        raise XCodeMCPError(f"Failed to run xcodebuild: {e}")

    # Parse destination lines
    destinations = []
    for line in output.split('\n'):
        line = line.strip()
        if line.startswith('{') and line.endswith('}'):
            parsed = parse_destination_line(line)
            if parsed:
                # Skip generic placeholder destinations
                if 'placeholder' in parsed.get('id', ''):
                    continue
                # Drop platform-incompatible destinations (each flagged by an
                # 'error' from xcodebuild) unless the caller asked for them.
                if not include_incompatible_destinations and 'error' in parsed:
                    continue
                destinations.append(parsed)

    if destinations:
        count = len(destinations)
        show_result_notification(
            f"{count} destination{'s' if count != 1 else ''} for {scheme}",
            f"Scheme: {scheme}",
        )
    else:
        show_result_notification("No destinations found", f"Scheme: {scheme}")

    return json.dumps(destinations, indent=2)
