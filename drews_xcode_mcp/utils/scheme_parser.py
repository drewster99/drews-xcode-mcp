#!/usr/bin/env python3
"""Parse Xcode scheme information from project files without opening Xcode"""

import os
import plistlib


def get_available_schemes(project_path: str) -> list[str]:
    """
    Get available build schemes by parsing .xcscheme files.

    Fast file-based approach (~0.1ms) vs AppleScript (1-2s).
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
                scheme_name = filename[:-len(".xcscheme")]
                schemes.append(scheme_name)

    return schemes


def _clean_scheme_key(raw_key: str) -> str:
    """
    Strip xcschememanagement.plist's SchemeUserState key decoration down to a
    plain scheme name.

    Keys look like "SchemeName.xcscheme_^#shared#^_" for a shared scheme, or
    just "SchemeName.xcscheme" for a user-only one.
    """
    name = raw_key
    marker = "_^#shared#^_"
    if name.endswith(marker):
        name = name[: -len(marker)]
    if name.endswith(".xcscheme"):
        name = name[: -len(".xcscheme")]
    return name


def get_current_scheme(project_path: str) -> str:
    """
    Get the currently selected scheme from workspace state.

    Reads xcschememanagement.plist under xcuserdata/*.xcuserdatad/xcschemes/
    and returns the scheme with the lowest orderHint -- Xcode records the
    active scheme as orderHint 0.

    Args:
        project_path: Path to .xcodeproj or .xcworkspace

    Returns:
        Current scheme name, or empty string if not found
    """
    xcuserdata_base = os.path.join(project_path, "xcuserdata")

    if not os.path.isdir(xcuserdata_base):
        return ""

    # Find the user plist (usually username.xcuserdatad)
    for entry in os.listdir(xcuserdata_base):
        if not entry.endswith(".xcuserdatad"):
            continue

        scheme_mgmt = os.path.join(xcuserdata_base, entry, "xcschemes", "xcschememanagement.plist")
        if not os.path.exists(scheme_mgmt):
            continue

        try:
            with open(scheme_mgmt, 'rb') as f:
                plist = plistlib.load(f)
        except Exception:
            continue

        if not isinstance(plist, dict):
            continue

        scheme_user_state = plist.get("SchemeUserState", {})
        if not scheme_user_state:
            continue

        best_key = min(
            scheme_user_state,
            key=lambda k: scheme_user_state[k].get("orderHint", float("inf"))
            if isinstance(scheme_user_state[k], dict) else float("inf"),
        )
        return _clean_scheme_key(best_key)

    return ""
