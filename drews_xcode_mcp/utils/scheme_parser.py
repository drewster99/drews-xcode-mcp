#!/usr/bin/env python3
"""Parse Xcode scheme information from project files without opening Xcode"""

import os
import plistlib
from pathlib import Path


def get_available_schemes(project_path: str) -> list[str]:
    """
    Get available build schemes by parsing .xcschemes files.

    Fast file-based approach (0.1ms) vs AppleScript (1-2s).
    No Xcode interaction required.

    Args:
        project_path: Path to .xcodeproj or .xcworkspace

    Returns:
        List of scheme names, sorted alphabetically
    """
    schemes = []

    # Normalize path to .xcodeproj if it's a workspace
    if project_path.endswith(".xcworkspace"):
        base_path = project_path.replace(".xcworkspace", ".xcodeproj")
    else:
        base_path = project_path

    schemes_dir = os.path.join(base_path, "xcshareddata", "xcschemes")

    if os.path.isdir(schemes_dir):
        for filename in sorted(os.listdir(schemes_dir)):
            if filename.endswith(".xcscheme"):
                scheme_name = filename[:-9]  # Remove .xcscheme (9 chars: . x c s c h e m e)
                schemes.append(scheme_name)

    return schemes


def get_current_scheme(project_path: str) -> str:
    """
    Get the currently selected scheme from workspace state.

    Reads from xcschememanagement.plist in xcuserdata.

    Args:
        project_path: Path to .xcodeproj or .xcworkspace

    Returns:
        Current scheme name, or empty string if not found
    """
    # Handle both .xcodeproj and .xcworkspace
    if project_path.endswith(".xcworkspace"):
        xcuserdata_base = os.path.join(project_path, "xcuserdata")
    else:
        xcuserdata_base = os.path.join(project_path, "xcuserdata")

    if not os.path.isdir(xcuserdata_base):
        return ""

    # Find the user plist (usually username.xcuserdatad)
    for entry in os.listdir(xcuserdata_base):
        if not entry.endswith(".xcuserdatad"):
            continue

        user_dir = os.path.join(xcuserdata_base, entry)

        # Try xcschememanagement.plist
        scheme_mgmt = os.path.join(user_dir, "xcschememanagement.plist")
        if os.path.exists(scheme_mgmt):
            try:
                with open(scheme_mgmt, 'rb') as f:
                    plist = plistlib.load(f)
                    if isinstance(plist, dict):
                        # SchemeUserState contains active scheme info
                        scheme_user_state = plist.get("SchemeUserState", {})
                        if scheme_user_state:
                            # Get first (usually only) scheme that's marked as active
                            for scheme_name, scheme_info in scheme_user_state.items():
                                if isinstance(scheme_info, dict):
                                    if scheme_info.get("isShown"):
                                        return scheme_name
                            # If no explicit active marker, just return first
                            return list(scheme_user_state.keys())[0]
            except Exception:
                pass

    return ""


def get_current_run_destination_and_scheme(project_path: str) -> tuple[str, str]:
    """
    Get the currently selected run destination and scheme.

    Args:
        project_path: Path to .xcodeproj or .xcworkspace

    Returns:
        Tuple of (destination, scheme), or ("", "") if not found
    """
    scheme = get_current_scheme(project_path)

    # Destination info would require parsing UserInterfaceState.xcuserstate
    # which is more complex. For now, just return scheme.
    # The read_active_run_destination utility in xcodebuild_query.py
    # handles destination parsing.

    return "", scheme
