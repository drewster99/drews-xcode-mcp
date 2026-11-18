#!/usr/bin/env python3
"""list_run_destinations tool - List available run destinations for a scheme"""

import json
import os
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
    get_first_scheme,
    list_destinations,
    omit_non_distinguishing_architectures,
    select_destinations_for_platform,
)


@mcp.tool(annotations=TOOL_READONLY)
@describe_parameters_from_docstring
@apply_config
def list_run_destinations(
    project_path: str,
    scheme: Optional[str] = None,
    platform: Optional[str] = None,
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

    'arch' is included only on destinations that share an 'id' with another
    destination of a different architecture, since only then does it tell them
    apart; otherwise it is omitted.

    Use the 'id' field from the results with set_run_destination to change
    which device Xcode builds and runs for.

    Args:
        project_path: Path to an Xcode project (.xcodeproj) or workspace (.xcworkspace).
        scheme: Scheme name to list destinations for. If not provided, uses the
            first scheme found via xcodebuild.
        platform: Return only destinations with exactly this platform, as
            xcodebuild names it (e.g. "macOS", "iOS", "iOS Simulator",
            "watchOS Simulator"). Case-sensitive; "iOS" does not match
            "iOS Simulator". An error naming the reported platforms is raised
            if none matches. If not provided, all platforms are returned.
        include_incompatible_destinations: If True, also include destinations
            whose platform is incompatible with the scheme (each with an 'error'
            field describing why). Defaults to False.

    Returns:
        JSON array of destinations, each with: name, platform, id, and optionally
        arch, OS, error, and variant fields.

    Raises:
        InvalidParameterError: If platform is given and no destination reports it.
    """
    normalized_path = validate_and_normalize_project_path(project_path, "Listing destinations for")
    project_name = os.path.basename(normalized_path)

    # Get scheme name if not provided
    if not scheme:
        scheme = get_first_scheme(normalized_path)
        if not scheme:
            raise XCodeMCPError(
                f"Could not determine scheme for {project_name}. "
                "Please provide a scheme name."
            )

    show_notification("Listing Destinations", project_name, f"Scheme: {scheme}")

    destinations = list_destinations(normalized_path, scheme)

    # Filter by platform before dropping incompatible destinations, so a platform
    # the scheme can't run on yields [] rather than a "no such platform" error.
    if platform is not None:
        destinations = select_destinations_for_platform(destinations, platform)

    # Drop platform-incompatible destinations (each flagged by an 'error' from
    # xcodebuild) unless the caller asked for them.
    if not include_incompatible_destinations:
        destinations = [d for d in destinations if 'error' not in d]

    destinations = omit_non_distinguishing_architectures(destinations)

    if destinations:
        count = len(destinations)
        show_result_notification(
            f"{count} destination{'s' if count != 1 else ''} for {scheme}",
            f"Scheme: {scheme}",
        )
    else:
        show_result_notification("No destinations found", f"Scheme: {scheme}")

    return json.dumps(destinations, indent=2)
