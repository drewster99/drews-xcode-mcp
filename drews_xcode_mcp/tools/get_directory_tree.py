#!/usr/bin/env python3
"""get_directory_tree tool - Visual directory tree"""

import os
from typing import List

from drews_xcode_mcp.server import mcp, TOOL_READONLY
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_and_normalize_directory_path
from drews_xcode_mcp.exceptions import InvalidParameterError, XCodeMCPError
from drews_xcode_mcp.utils.applescript import show_error_notification

# Directories whose contents we never expand in the tree view. These are
# typically large build/dependency caches that bloat the output without
# adding information the LLM needs.
SKIP_DIR_NAMES = frozenset([
    '.build',
    'build',
    'DerivedData',
    'node_modules',
    'venv',
    '.venv',
    '__pycache__',
    'Pods',
    '.cocoapods',
    '.swiftpm',
    'Carthage',
])

# Hard ceiling on lines returned. Past this we truncate with a note so the
# response can't blow up token usage on pathological repos.
MAX_TREE_LINES = 5000

# Hard ceiling on recursion depth. A caller-supplied max_depth is clamped to
# this so an extreme value can't walk an enormous tree before truncation.
MAX_TREE_DEPTH = 32


@mcp.tool(annotations=TOOL_READONLY)
@describe_parameters_from_docstring
@apply_config
def get_directory_tree(directory_path: str, max_depth: int = 4) -> str:
    """
    Get a visual tree of directories (folders only) in the specified path.

    Shows the folder structure as a tree diagram with box-drawing characters.
    Does not include individual files - use get_directory_listing for file details.

    Special behavior: If directory_path ends with .xcodeproj or .xcworkspace,
    the tree will show the parent directory structure (since these are typically
    at the root of a project folder).

    Args:
        directory_path: Path to directory to scan. Can also be a .xcodeproj or
                       .xcworkspace path (will scan parent directory in that case).
        max_depth: Maximum recursion depth (default 4, prevents excessive output).
                  Depth 1 = immediate subdirectories only, Depth 4 = up to 4 levels deep.

    Returns:
        A visual tree representation showing only directories/folders, with a note
        about using get_directory_listing for file-level details.

        Example:
        /Users/you/Projects/MyApp/
        ├── Sources/
        │   ├── Models/
        │   └── Views/
        ├── Tests/
        └── Resources/
    """
    # Validate max_depth
    if max_depth < 1:
        raise InvalidParameterError("max_depth must be at least 1")
    # Clamp so a huge value can't drive an enormous filesystem walk before the
    # output-line truncation below would ever kick in.
    max_depth = min(max_depth, MAX_TREE_DEPTH)

    directory_path = validate_and_normalize_directory_path(directory_path)

    # Determine which directory to scan
    # If path ends with .xcodeproj or .xcworkspace, scan the parent directory
    if directory_path.endswith('.xcodeproj') or directory_path.endswith('.xcworkspace'):
        scan_dir = os.path.dirname(directory_path)
    else:
        scan_dir = directory_path

    # Verify scan_dir is a directory
    if not os.path.isdir(scan_dir):
        error_msg = f"Not a directory: {scan_dir}"
        show_error_notification(error_msg)
        raise InvalidParameterError(f"Path is not a directory: {scan_dir}")

    # Running count of emitted lines (header included) so traversal can stop as
    # soon as the output budget is reached, rather than walking the whole tree
    # and truncating afterward.
    emitted = 1
    # Set when traversal stops short because the line budget was hit, so the
    # truncation can be reported rather than silently capping the output.
    overflowed = False

    # Build the hierarchy (directories only)
    def build_hierarchy(path: str, prefix: str = "", is_last: bool = True, base_path: str = "", current_depth: int = 0) -> List[str]:
        """Recursively build a visual hierarchy of directories only"""
        nonlocal emitted, overflowed
        lines = []

        if not base_path:
            base_path = path

        # Add current item (only if it's a directory and not the base)
        if path != base_path:
            connector = "└── " if is_last else "├── "
            name = os.path.basename(path) + "/"
            lines.append(prefix + connector + name)
            emitted += 1

            # Update prefix for children
            extension = "    " if is_last else "│   "
            prefix = prefix + extension

        # Check if we've reached the output-line budget or max depth
        if emitted >= MAX_TREE_LINES:
            overflowed = True
            return lines
        if current_depth >= max_depth:
            return lines

        # If it's a directory, recurse into it (with restrictions)
        if os.path.isdir(path):
            # Skip certain directories
            if os.path.basename(path) in SKIP_DIR_NAMES:
                return lines

            # Don't recurse into .xcodeproj or .xcworkspace directories
            if path.endswith('.xcodeproj') or path.endswith('.xcworkspace'):
                return lines

            try:
                items = sorted(os.listdir(path))
                # Filter to directories only, exclude hidden except for important ones
                dir_items = []
                for item in items:
                    item_path = os.path.join(path, item)
                    if os.path.isdir(item_path):
                        # Include if not hidden, or if it's an important hidden dir
                        if not item.startswith('.') or item in ['.git']:
                            dir_items.append(item)

                for i, item in enumerate(dir_items):
                    if emitted >= MAX_TREE_LINES:
                        overflowed = True
                        break
                    item_path = os.path.join(path, item)
                    is_last_item = (i == len(dir_items) - 1)
                    lines.extend(build_hierarchy(item_path, prefix, is_last_item, base_path, current_depth + 1))
            except PermissionError:
                pass

        return lines

    # Build hierarchy starting from scan directory
    hierarchy_lines = [scan_dir + "/"]

    try:
        items = sorted(os.listdir(scan_dir))
        # Filter to directories only
        dir_items = []
        for item in items:
            item_path = os.path.join(scan_dir, item)
            if os.path.isdir(item_path):
                if not item.startswith('.') or item in ['.git']:
                    dir_items.append(item)

        for i, item in enumerate(dir_items):
            if emitted >= MAX_TREE_LINES:
                overflowed = True
                break
            item_path = os.path.join(scan_dir, item)
            is_last_item = (i == len(dir_items) - 1)
            hierarchy_lines.extend(build_hierarchy(item_path, "", is_last_item, scan_dir, 1))

    except Exception as e:
        raise XCodeMCPError(f"Error building directory tree for {directory_path}: {str(e)}")

    # Traversal stops as soon as `emitted` hits MAX_TREE_LINES, so
    # hierarchy_lines never exceeds the cap — `overflowed` is the only signal
    # that entries were dropped.
    truncation_note = ""
    if overflowed:
        truncation_note = (
            f"\n\n[Truncated at {MAX_TREE_LINES} lines; the tree has more entries. "
            f"Lower max_depth or call get_directory_listing on a subdirectory.]"
        )

    tree_output = '\n'.join(hierarchy_lines)
    return tree_output + truncation_note + "\n\nUse `get_directory_listing` to see files and details for a specific directory."
