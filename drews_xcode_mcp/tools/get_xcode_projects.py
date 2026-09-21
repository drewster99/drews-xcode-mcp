#!/usr/bin/env python3
"""get_xcode_projects tool - Find Xcode projects and workspaces"""

import json
import os
import sys
import subprocess
import re

from drews_xcode_mcp.server import mcp, TOOL_READONLY
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import ALLOWED_FOLDERS, is_path_allowed
from drews_xcode_mcp.exceptions import AccessDeniedError, InvalidParameterError, XCodeMCPError
from drews_xcode_mcp.utils.applescript import show_access_denied_notification, show_error_notification, show_result_notification, show_warning_notification
from drews_xcode_mcp.utils.scheme_parser import get_available_schemes, get_current_scheme
from drews_xcode_mcp.utils.xcodebuild_query import lookup_simulator_info, read_active_run_destination

# Tracks .xcodeproj paths created during this server session, so they can
# be returned by get_xcode_projects before Spotlight indexes them.
_recently_created_projects: list[str] = []


def register_created_project(xcodeproj_path: str):
    """Record a newly created .xcodeproj so get_xcode_projects can find it immediately."""
    _recently_created_projects.append(xcodeproj_path)


def _get_open_and_recent_projects() -> tuple[list[str], list[str]]:
    """
    Get currently open and recently opened Xcode projects.

    Open projects come from Xcode's AppleScript `workspace documents`. Recents
    come from decoding macOS's shared file list (Xcode's own recent documents),
    trying the newer .sfl4 format first and falling back to .sfl3 for older
    macOS versions. A project that is both open and in the recents list is
    reported only in the open list.

    Returns:
        Tuple of (open_paths, recent_paths). Both empty on any failure.
    """
    try:
        utils_dir = os.path.dirname(os.path.abspath(__file__))
        parent_dir = os.path.dirname(utils_dir)
        swift_script = os.path.join(parent_dir, 'utils', 'decode_xcode_recents.swift')

        if not os.path.exists(swift_script):
            print(f"Warning: Swift decoder not found at {swift_script}", file=sys.stderr)
            return [], []

        result = subprocess.run(
            ['swift', swift_script, '--include-open'],
            capture_output=True, text=True, timeout=10,
        )

        if result.returncode != 0 or not result.stdout.strip():
            return [], []

        open_paths = []
        recent_paths = []
        for line in result.stdout.strip().split('\n'):
            if line.startswith('OPEN:'):
                path = line[len('OPEN:'):]
                if path and os.path.exists(path):
                    open_paths.append(path)
            elif line.startswith('RECENT:'):
                path = line[len('RECENT:'):]
                if path and os.path.exists(path):
                    recent_paths.append(path)

        return open_paths, recent_paths
    except Exception as e:
        print(f"Warning: Failed to get open/recent projects: {e}", file=sys.stderr)
        return [], []


def _filter_project_results(paths: list[str], search_paths: list[str] = None, max_depth: int = None, regex_filter: str = None) -> list[str]:
    """
    Filter project paths to remove noise and duplicates.

    Filters applied:
    1. Remove Pods.xcodeproj (CocoaPods dependencies)
    2. Remove paths under $HOME/Library (iCloud sync duplicates, system data)
    3. Remove paths with .playground in parent directories
    4. Remove nested projects (projects inside other .xcodeproj/.xcworkspace folders)
    5. Filter by directory depth from search path (if max_depth specified)
    6. Filter by regex pattern (if regex_filter specified)
    7. Prefer .xcworkspace over .xcodeproj when both exist in same directory

    Args:
        paths: List of project paths to filter
        search_paths: List of base paths being searched (for depth calculation)
        max_depth: Maximum directory depth from search path (None = no limit)
                   Depth 0 = directly in search path, depth 1 = one level down, etc.
        regex_filter: Optional regex pattern to filter paths

    Returns:
        Filtered list of project paths
    """
    if not paths:
        return []

    home_library = os.path.expanduser("~/Library")
    filtered = []

    # Compile regex if provided
    regex_pattern = None
    if regex_filter:
        try:
            regex_pattern = re.compile(regex_filter)
        except re.error as e:
            print(f"Warning: Invalid regex pattern '{regex_filter}': {e}", file=sys.stderr)

    for path in paths:
        # Filter 0: Enforce the allow-list on every path. mdfind results are
        # already constrained by `-onlyin`, but recents come from Xcode's global
        # recents list and could otherwise point outside the configured folders.
        if not is_path_allowed(path):
            continue

        # Filter 1: Skip Pods.xcodeproj
        if os.path.basename(path) == "Pods.xcodeproj":
            continue

        # Filter 2: Skip anything under $HOME/Library. Match on a path-component
        # boundary so a sibling like "~/LibraryNotes" isn't swept up.
        if path == home_library or path.startswith(home_library + os.sep):
            continue

        # Filter 3: Skip if any parent directory ends with .playground
        path_parts = path.split('/')
        has_playground_parent = any(part.endswith('.playground') for part in path_parts[:-1])
        if has_playground_parent:
            continue

        # Filter 4: Skip nested projects (project inside another .xcodeproj or .xcworkspace)
        has_nested_parent = any(
            part.endswith('.xcodeproj') or part.endswith('.xcworkspace')
            for part in path_parts[:-1]
        )
        if has_nested_parent:
            continue

        # Filter 5: Check depth limit if specified
        if max_depth is not None and search_paths:
            # Calculate minimum depth from any search path.
            # Use realpath on both sides so symlinked search paths still match
            # the resolved paths returned by mdfind.
            min_depth = None
            abs_path = os.path.realpath(path)

            for search_path in search_paths:
                abs_search = os.path.realpath(search_path)
                # Compare on a path-component boundary so "/a/App" doesn't match
                # a sibling "/a/App-Other".
                if abs_path == abs_search or abs_path.startswith(abs_search + os.sep):
                    # Calculate depth from this search path
                    # Depth 0 = directly in search path, depth 1 = one level down, etc.
                    rel_path = abs_path[len(abs_search):].lstrip('/')
                    depth = rel_path.count('/')
                    if min_depth is None or depth < min_depth:
                        min_depth = depth

            # Skip if too deep from all search paths
            if min_depth is None or min_depth > max_depth:
                continue

        # Filter 6: Apply regex filter if specified
        if regex_pattern and not regex_pattern.search(path):
            continue

        filtered.append(path)

    # Filter 7: Prefer .xcworkspace over .xcodeproj in same directory
    # Group by directory and base name
    project_groups = {}
    for path in filtered:
        dirname = os.path.dirname(path)
        basename = os.path.basename(path)

        # Extract base name without extension
        if basename.endswith('.xcodeproj'):
            base = basename[:-10]  # Remove .xcodeproj
        elif basename.endswith('.xcworkspace'):
            base = basename[:-12]  # Remove .xcworkspace
        else:
            continue

        key = (dirname, base)
        if key not in project_groups:
            project_groups[key] = []
        project_groups[key].append(path)

    # For each group, prefer .xcworkspace if both exist
    final_results = []
    for (dirname, base), group_paths in project_groups.items():
        if len(group_paths) == 1:
            final_results.append(group_paths[0])
        else:
            # Prefer .xcworkspace over .xcodeproj
            workspace = [p for p in group_paths if p.endswith('.xcworkspace')]
            if workspace:
                final_results.append(workspace[0])
            else:
                # Shouldn't happen, but fall back to first one
                final_results.append(group_paths[0])

    return final_results


def _build_open_entry(path: str) -> tuple[dict | None, str | None]:
    """
    Build a `currently_open` entry with current scheme and run destination.

    Both are read without opening/side-effecting Xcode: the scheme comes from
    parsing xcschememanagement.plist, the destination from Xcode's workspace
    state file.

    Returns:
        (entry, None) on success, or (None, warning_message) if either the
        scheme or the destination can't be determined -- the caller should
        downgrade this project to the `recent` group in that case.
    """
    scheme = get_current_scheme(path)
    if not scheme:
        return None, f"Could not determine current scheme for open project '{path}'; listing as recent instead."

    try:
        _, destination = read_active_run_destination(path, scheme)
    except (XCodeMCPError, OSError) as e:
        return None, f"Could not determine run destination for open project '{path}' ({e}); listing as recent instead."

    name, _os_version = lookup_simulator_info(destination.id)
    destination_name = name or destination.id

    entry = {
        "project": path,
        "current_scheme": scheme,
        "current_run_destination": destination_name,
        "guidance": {
            "build": f"`build_project(project_path='{path}', scheme='{scheme}')`",
            "build_and_run": f"`run_project_with_user_interaction(project_path='{path}', scheme='{scheme}')`",
            "change_destination": (
                f"`list_run_destinations(project_path='{path}', scheme='{scheme}')` to see options, "
                f"then `set_run_destination(project_path='{path}', run_destination='...')`"
            ),
        },
    }
    return entry, None


def _build_recent_entry(path: str) -> dict:
    """Build a `recent` entry listing the project's available schemes."""
    schemes = get_available_schemes(path)

    guidance = {
        "list_schemes": f"`get_project_schemes(project_path='{path}')`",
    }
    if schemes:
        guidance["build"] = f"`build_project(project_path='{path}', scheme='{schemes[0]}')`"

    return {
        "project": path,
        "available_schemes": schemes,
        "guidance": guidance,
    }


def _build_other_entry(path: str) -> dict:
    """Build an `other_projects` entry -- just enough to look up schemes next."""
    return {
        "project": path,
        "guidance": {
            "list_schemes": f"`get_project_schemes(project_path='{path}')`",
        },
    }


@mcp.tool(annotations=TOOL_READONLY)
@describe_parameters_from_docstring
@apply_config
def get_xcode_projects(
    search_path: str = "",
    max_search_depth: int = 3,
    regex_filter: str = None,
    max_results: int = 10
) -> str:
    """
    Find Xcode projects and workspaces, grouped by how ready they are to build.

    Returns JSON grouped into three categories:
    - currently_open: projects open in Xcode right now, with their current
      scheme and run destination, and exact ready-to-call commands to build
      or build-and-run. Always returned in full -- never truncated by
      max_results.
    - recent: projects from Xcode's recent-documents list that are not
      currently open, with their available schemes.
    - other_projects: everything else found by a filesystem search.

    recent and other_projects together are capped at max_results, with recent
    taking precedence -- other_projects only fills remaining room after all
    (unfiltered) recents are counted.

    If search_path is empty, searches all paths to which this tool has been
    granted access. Uses `mdfind` (Spotlight indexing) to find files
    efficiently for the other_projects group.

    Args:
        search_path: Path to search for other_projects. If empty, searches all allowed folders.
        max_search_depth: Maximum directory depth from search path (default: 3)
                         Depth 0 = directly in search path, depth 1 = one level down, etc.
        regex_filter: Optional regex pattern to filter results
        max_results: Maximum combined number of recent + other_projects entries to return (default: 10)

    Returns:
        JSON string: {"result": "success" | "success_with_warnings", "warnings": [...],
        "guidance": "...", "content": {"currently_open": [...], "recent": [...], "other_projects": [...]}}
    """
    warnings: list[str] = []

    # Determine paths to search
    paths_to_search = []

    if not search_path or search_path.strip() == "":
        # Search all allowed folders
        paths_to_search = list(ALLOWED_FOLDERS)
    else:
        # Search specific path
        project_path = search_path.strip()

        # Relative paths would resolve against the server's cwd, which is
        # almost never what the LLM intended — reject up front rather than
        # silently producing surprising results.
        if not os.path.isabs(project_path):
            raise InvalidParameterError(
                f"search_path must be an absolute path (got: {project_path!r})"
            )

        # Security check
        if not is_path_allowed(project_path):
            show_access_denied_notification(f"Access denied: {project_path}")
            raise AccessDeniedError(f"Access to path '{project_path}' is not allowed. Set XCODEMCP_ALLOWED_FOLDERS environment variable.")

        # Check if the path exists
        if not os.path.exists(project_path):
            show_error_notification(f"Path not found: {project_path}")
            raise InvalidParameterError(f"Project path does not exist: {project_path}")

        paths_to_search = [project_path]

    # Search for projects in all paths. Collect per-path failures so the
    # caller can tell "no projects" apart from "the search itself was
    # incomplete".
    mdfind_timeout_seconds = 30
    all_results = []
    for path in paths_to_search:
        try:
            mdfindResult = subprocess.run(
                ['mdfind', '-onlyin', path,
                 'kMDItemFSName == "*.xcodeproj" || kMDItemFSName == "*.xcworkspace"'],
                capture_output=True, text=True, check=True,
                timeout=mdfind_timeout_seconds,
            )
            stdout = mdfindResult.stdout.strip()
            if stdout:
                all_results.extend(stdout.split('\n'))
        except subprocess.TimeoutExpired:
            reason = f"mdfind timed out after {mdfind_timeout_seconds}s"
            warnings.append(f"{path}: {reason}")
            show_warning_notification(f"mdfind timed out for {os.path.basename(path)}")
            print(f"Warning: {reason} in {path}", file=sys.stderr)
        except subprocess.CalledProcessError as e:
            reason = f"mdfind exited {e.returncode}: {(e.stderr or '').strip() or '(no stderr)'}"
            warnings.append(f"{path}: {reason}")
            show_warning_notification(f"mdfind failed for {os.path.basename(path)}", reason)
            print(f"Warning: {reason} in {path}", file=sys.stderr)
        except OSError as e:
            reason = f"mdfind not invokable: {e}"
            warnings.append(f"{path}: {reason}")
            show_warning_notification(f"mdfind failed for {os.path.basename(path)}", str(e))
            print(f"Warning: {reason} in {path}", file=sys.stderr)

    # Supplement mdfind with recently created projects that Spotlight
    # may not have indexed yet
    mdfind_set = set(all_results)
    for path in _recently_created_projects:
        if path not in mdfind_set and os.path.exists(path):
            all_results.append(path)

    # Get open and recent projects
    open_paths, recent_paths = _get_open_and_recent_projects()
    open_paths = _filter_project_results(
        open_paths, search_paths=paths_to_search, max_depth=None, regex_filter=regex_filter
    )
    recent_paths = _filter_project_results(
        recent_paths, search_paths=paths_to_search, max_depth=max_search_depth, regex_filter=regex_filter
    )

    # Filter mdfind ("other") results, excluding anything already in open/recent
    other_paths = _filter_project_results(
        all_results, search_paths=paths_to_search, max_depth=max_search_depth, regex_filter=regex_filter
    )
    open_and_recent_set = set(open_paths) | set(recent_paths)
    other_paths = [p for p in other_paths if p not in open_and_recent_set]

    # Build currently_open entries, downgrading any that fail to recent
    currently_open = []
    downgraded_to_recent = []
    for path in open_paths:
        entry, warning = _build_open_entry(path)
        if entry:
            currently_open.append(entry)
        else:
            warnings.append(warning)
            downgraded_to_recent.append(path)

    # Recent = original recents + any downgraded open projects, deduplicated,
    # downgraded ones first since they were just open a moment ago
    recent_combined = list(dict.fromkeys(downgraded_to_recent + recent_paths))

    # Apply max_results to recent (precedence) + other combined
    remaining = max_results if max_results and max_results > 0 else None
    if remaining is not None:
        recent_combined = recent_combined[:remaining]
        remaining_after_recent = max(0, remaining - len(recent_combined))
        other_paths = other_paths[:remaining_after_recent]

    recent_entries = [_build_recent_entry(p) for p in recent_combined]
    other_entries = [_build_other_entry(p) for p in other_paths]

    total_count = len(currently_open) + len(recent_entries) + len(other_entries)
    if total_count:
        show_result_notification(
            f"Found {total_count} project{'s' if total_count != 1 else ''}",
            f"{len(currently_open)} open, {len(recent_entries)} recent, {len(other_entries)} other",
        )
    else:
        show_result_notification("No projects found")

    result = {
        "result": "success_with_warnings" if warnings else "success",
        "guidance": (
            "For any project, `get_project_schemes(project_path='...')` lists all available "
            "schemes (opens the project in Xcode if not already open). "
            "`get_active_run_destination(project_path='...')` reports the currently selected run target."
        ),
        "content": {
            "currently_open": currently_open,
            "recent": recent_entries,
            "other_projects": other_entries,
        },
    }
    if warnings:
        result["warnings"] = warnings

    return json.dumps(result, indent=2)
