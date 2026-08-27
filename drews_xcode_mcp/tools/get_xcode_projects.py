#!/usr/bin/env python3
"""get_xcode_projects tool - Find Xcode projects and workspaces"""

import os
import sys
import subprocess
import re

from drews_xcode_mcp.server import mcp, TOOL_READONLY
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import ALLOWED_FOLDERS, is_path_allowed
from drews_xcode_mcp.exceptions import AccessDeniedError, InvalidParameterError
from drews_xcode_mcp.utils.applescript import show_access_denied_notification, show_error_notification, show_result_notification, show_warning_notification

# Tracks .xcodeproj paths created during this server session, so they can
# be returned by get_xcode_projects before Spotlight indexes them.
_recently_created_projects: list[str] = []


def register_created_project(xcodeproj_path: str):
    """Record a newly created .xcodeproj so get_xcode_projects can find it immediately."""
    _recently_created_projects.append(xcodeproj_path)


def _get_recent_xcode_projects() -> list[str]:
    """
    Get recently opened Xcode projects by decoding macOS shared file list.

    Returns:
        List of absolute paths to recently opened .xcodeproj and .xcworkspace files.
        Returns empty list if unable to decode recents.
    """
    try:
        # Get path to Swift decoder script
        utils_dir = os.path.dirname(os.path.abspath(__file__))
        parent_dir = os.path.dirname(utils_dir)
        swift_script = os.path.join(parent_dir, 'utils', 'decode_xcode_recents.swift')

        if not os.path.exists(swift_script):
            print(f"Warning: Swift decoder not found at {swift_script}", file=sys.stderr)
            return []

        # Run Swift script to decode bookmark data
        result = subprocess.run(['swift', swift_script],
                              capture_output=True, text=True, timeout=5)

        if result.returncode == 0 and result.stdout.strip():
            paths = result.stdout.strip().split('\n')
            return [p for p in paths if p and os.path.exists(p)]

        return []
    except Exception as e:
        print(f"Warning: Failed to get recent projects: {e}", file=sys.stderr)
        return []


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


@mcp.tool(annotations=TOOL_READONLY)
@describe_parameters_from_docstring
@apply_config
def get_xcode_projects(
    search_path: str = "",
    include_recents: bool = True,
    max_search_depth: int = 3,
    regex_filter: str = None,
    max_results: int = 10
) -> str:
    """
    Search for .xcodeproj and .xcworkspace files, optionally including recent projects.

    If search_path is empty, searches all paths to which this tool has been granted access.
    Uses `mdfind` (Spotlight indexing) to find files efficiently.

    Args:
        search_path: Path to search. If empty, searches all allowed folders.
        include_recents: If True, include recently opened projects first (default: True)
        max_search_depth: Maximum directory depth from search path (default: 3)
                         Depth 0 = directly in search path, depth 1 = one level down, etc.
        regex_filter: Optional regex pattern to filter results
        max_results: Maximum number of results to return (default: 10)

    Returns:
        A newline-separated list of .xcodeproj and .xcworkspace paths.
        Recent projects appear first if include_recents=True.
        Returns empty string if none are found.
    """
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
    search_warnings = []
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
            search_warnings.append(f"{path}: {reason}")
            show_warning_notification(f"mdfind timed out for {os.path.basename(path)}")
            print(f"Warning: {reason} in {path}", file=sys.stderr)
        except subprocess.CalledProcessError as e:
            reason = f"mdfind exited {e.returncode}: {(e.stderr or '').strip() or '(no stderr)'}"
            search_warnings.append(f"{path}: {reason}")
            show_warning_notification(f"mdfind failed for {os.path.basename(path)}", reason)
            print(f"Warning: {reason} in {path}", file=sys.stderr)
        except OSError as e:
            reason = f"mdfind not invokable: {e}"
            search_warnings.append(f"{path}: {reason}")
            show_warning_notification(f"mdfind failed for {os.path.basename(path)}", str(e))
            print(f"Warning: {reason} in {path}", file=sys.stderr)

    # Supplement mdfind with recently created projects that Spotlight
    # may not have indexed yet
    mdfind_set = set(all_results)
    for path in _recently_created_projects:
        if path not in mdfind_set and os.path.exists(path):
            all_results.append(path)

    # Get recent projects if requested
    recent_projects = []
    if include_recents:
        recent_projects = _get_recent_xcode_projects()
        # Filter recents with same criteria
        recent_projects = _filter_project_results(
            recent_projects,
            search_paths=paths_to_search,
            max_depth=max_search_depth,
            regex_filter=regex_filter
        )

    # Filter mdfind results
    filtered_results = _filter_project_results(
        all_results,
        search_paths=paths_to_search,
        max_depth=max_search_depth,
        regex_filter=regex_filter
    )

    # Combine recents (first) + mdfind results, removing duplicates
    # Use dict to preserve order while removing duplicates
    combined = {}
    for path in recent_projects:
        combined[path] = True
    for path in filtered_results:
        combined[path] = True

    unique_results = list(combined.keys())

    # Apply max_results limit
    if max_results and max_results > 0:
        unique_results = unique_results[:max_results]

    # Show result notification
    if unique_results:
        count = len(unique_results)
        # Get first 3 project names for notification
        sample_names = [os.path.basename(p) for p in unique_results[:3]]
        if count <= 3:
            details = "\n".join(f"• {name}" for name in sample_names)
        else:
            details = "\n".join(f"• {name}" for name in sample_names) + f"\n• +{count - 3} more"

        # Add note about recents if included
        note = f"Found {count} project{'s' if count != 1 else ''}"
        if include_recents and recent_projects:
            note += f" ({len(recent_projects)} recent)"
        show_result_notification(note, details)
    else:
        show_result_notification("No projects found")

    result = '\n'.join(unique_results) if unique_results else ""
    if result:
        result += "\n\nTo build a project, use `get_project_schemes` to see available build schemes, then call `build_project`."

    if search_warnings:
        warn_block = "\n".join(f"- {w}" for w in search_warnings)
        prefix = "\n\n" if result else ""
        result += (
            f"{prefix}[search warnings — results may be incomplete]\n{warn_block}"
        )

    return result
