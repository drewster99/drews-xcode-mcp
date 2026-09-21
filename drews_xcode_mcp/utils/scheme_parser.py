#!/usr/bin/env python3
"""Parse Xcode scheme information from project files without opening Xcode"""

import getpass
import os
import plistlib
import re


def _referenced_project_paths(workspace_path: str) -> list[str]:
    """
    Parse a .xcworkspace's contents.xcworkspacedata for FileRef entries that
    point at a .xcodeproj, and resolve them to absolute paths.

    Only handles the common "group:" and "container:" location prefixes
    (paths relative to the workspace's parent directory); "self:" (the
    workspace's own directory) and absolute paths are also handled.
    """
    contents_path = os.path.join(workspace_path, "contents.xcworkspacedata")
    if not os.path.exists(contents_path):
        return []

    try:
        with open(contents_path, "r", encoding="utf-8") as f:
            contents = f.read()
    except OSError:
        return []

    workspace_dir = os.path.dirname(workspace_path)
    paths = []
    for match in re.finditer(r'location\s*=\s*"([^"]+)"', contents):
        location = match.group(1)
        if not location.endswith(".xcodeproj"):
            continue

        if location.startswith("group:") or location.startswith("container:"):
            rel = location.split(":", 1)[1]
            paths.append(os.path.normpath(os.path.join(workspace_dir, rel)))
        elif location.startswith("self:"):
            rel = location.split(":", 1)[1]
            paths.append(os.path.normpath(os.path.join(workspace_dir, rel)))
        elif location.startswith("absolute:"):
            paths.append(location.split(":", 1)[1])
        elif os.path.isabs(location):
            paths.append(location)
        else:
            paths.append(os.path.normpath(os.path.join(workspace_dir, location)))

    return paths


def _schemes_dir_candidates(project_path: str) -> list[str]:
    """
    Return every xcshareddata/xcschemes directory that could hold schemes for
    a given .xcodeproj or .xcworkspace path.

    A workspace can have its own shared schemes directly, and/or inherit
    schemes from each project it references (the common case -- most
    workspaces have no schemes of their own and only exist to bundle
    projects). There's no guarantee the workspace shares its base name with
    its project, so this parses contents.xcworkspacedata rather than
    guessing a sibling path.
    """
    if project_path.endswith(".xcworkspace"):
        candidates = [os.path.join(project_path, "xcshareddata", "xcschemes")]
        for referenced in _referenced_project_paths(project_path):
            candidates.append(os.path.join(referenced, "xcshareddata", "xcschemes"))
        return candidates

    return [os.path.join(project_path, "xcshareddata", "xcschemes")]


def get_available_schemes(project_path: str) -> list[str]:
    """
    Get available build schemes by parsing .xcscheme files.

    Fast file-based approach (~0.1ms) vs AppleScript (1-2s).
    No Xcode interaction required.

    Args:
        project_path: Path to .xcodeproj or .xcworkspace

    Returns:
        List of scheme names, sorted alphabetically, deduplicated across all
        contributing projects for a workspace.
    """
    schemes: set[str] = set()

    for schemes_dir in _schemes_dir_candidates(project_path):
        if not os.path.isdir(schemes_dir):
            continue
        for filename in os.listdir(schemes_dir):
            if filename.endswith(".xcscheme"):
                schemes.add(filename[: -len(".xcscheme")])

    return sorted(schemes)


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


def _scheme_from_management_plist(scheme_mgmt: str) -> str | None:
    """Read the orderHint-0 scheme name out of one xcschememanagement.plist, or None."""
    try:
        with open(scheme_mgmt, 'rb') as f:
            plist = plistlib.load(f)
    except Exception:
        return None

    if not isinstance(plist, dict):
        return None

    scheme_user_state = plist.get("SchemeUserState", {})
    if not scheme_user_state:
        return None

    best_key = min(
        scheme_user_state,
        key=lambda k: scheme_user_state[k].get("orderHint", float("inf"))
        if isinstance(scheme_user_state[k], dict) else float("inf"),
    )
    return _clean_scheme_key(best_key)


def get_current_scheme(project_path: str) -> str:
    """
    Get the currently selected scheme from workspace state.

    Reads xcschememanagement.plist under
    xcuserdata/<current-user>.xcuserdatad/xcschemes/ and returns the scheme
    with the lowest orderHint -- Xcode records the active scheme as
    orderHint 0. Only ever reads the *current* OS user's xcuserdata: a
    checked-out repo can carry another contributor's committed xcuserdata
    directory, and that other user's scheme selection is not this session's
    active scheme.

    Args:
        project_path: Path to .xcodeproj or .xcworkspace

    Returns:
        Current scheme name, or empty string if not found
    """
    xcuserdata_base = os.path.join(project_path, "xcuserdata")
    current_user = getpass.getuser()

    if os.path.isdir(xcuserdata_base):
        scheme_mgmt = os.path.join(
            xcuserdata_base, f"{current_user}.xcuserdatad", "xcschemes", "xcschememanagement.plist"
        )
        if os.path.exists(scheme_mgmt):
            scheme = _scheme_from_management_plist(scheme_mgmt)
            if scheme:
                return scheme

    return ""
