#!/usr/bin/env python3
"""get_directory_listing tool - List directory contents with metadata"""

import os
import re
import datetime
from typing import Optional

from drews_xcode_mcp.server import mcp, TOOL_READONLY
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_and_normalize_directory_path
from drews_xcode_mcp.exceptions import AccessDeniedError, InvalidParameterError, XCodeMCPError
from drews_xcode_mcp.utils.applescript import show_error_notification


@mcp.tool(annotations=TOOL_READONLY)
@describe_parameters_from_docstring
@apply_config
def get_directory_listing(directory_path: str,
                         regex_filter: Optional[str] = None,
                         sort_by: str = "time",
                         reverse: bool = True,
                         max_results: int = 50) -> str:
    """
    List contents of a directory with file metadata.

    Args:
        directory_path: Path to directory to list
        regex_filter: Optional regex to filter filenames (applied to basename only)
        sort_by: Sort by "time" (modification time) or "name" (alphabetical). Default: "time"
        reverse: Reverse sort order. Default: True (most recent first / Z-A)
        max_results: Maximum entries to return (default 50, hard limit 100)

    Returns:
        Formatted listing with: name, type (file/dir), size, modified time.
        Default behavior: 50 most recently modified files/folders.
        Format: "main.swift  [file]  2.5 KB  2025-10-01 14:30"
    """
    # Validate sort_by
    if sort_by not in ["time", "name"]:
        raise InvalidParameterError("sort_by must be 'time' or 'name'")

    # Hard limit max_results to 100
    if max_results < 1:
        raise InvalidParameterError("max_results must be at least 1")
    if max_results > 100:
        max_results = 100

    directory_path = validate_and_normalize_directory_path(directory_path)

    if not os.path.isdir(directory_path):
        show_error_notification(f"Not a directory: {directory_path}")
        raise InvalidParameterError(f"Path is not a directory: {directory_path}")

    # Helper function to format file size
    def format_size(size_bytes: int) -> str:
        """Format size in human-readable format"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size_bytes < 1024.0:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} TB"

    try:
        # Get all items in directory
        items = os.listdir(directory_path)

        # Build list of item info
        item_list = []
        for item in items:
            item_path = os.path.join(directory_path, item)

            # Apply regex filter if provided
            if regex_filter and regex_filter.strip():
                try:
                    if not re.search(regex_filter, item):
                        continue
                except re.error as e:
                    raise InvalidParameterError(f"Invalid regex pattern: {e}")

            try:
                stat_info = os.stat(item_path)
                is_dir = os.path.isdir(item_path)

                item_list.append({
                    'name': item,
                    'is_dir': is_dir,
                    'size': 0 if is_dir else stat_info.st_size,
                    'mtime': stat_info.st_mtime,
                    'path': item_path
                })
            except (OSError, PermissionError):
                # Skip items we can't stat
                continue

        # Sort items
        if sort_by == "time":
            item_list.sort(key=lambda x: x['mtime'], reverse=reverse)
        else:  # sort_by == "name"
            item_list.sort(key=lambda x: x['name'].lower(), reverse=reverse)

        # Limit results
        item_list = item_list[:max_results]

        if not item_list:
            return f"No items found in {directory_path}" + (f" matching filter '{regex_filter}'" if regex_filter else "")

        # Format output
        output_lines = []
        output_lines.append(f"Contents of {directory_path}/")
        output_lines.append(f"(Showing {len(item_list)} item(s), sorted by {sort_by}, {'descending' if reverse else 'ascending'})")
        output_lines.append("")

        for item in item_list:
            # Format modification time
            mtime_str = datetime.datetime.fromtimestamp(item['mtime']).strftime('%Y-%m-%d %H:%M')

            # Format item type and size
            if item['is_dir']:
                type_str = "[dir]"
                size_str = "-"
            else:
                type_str = "[file]"
                size_str = format_size(item['size'])

            # Format output line
            output_lines.append(f"{item['name']:40s}  {type_str:8s}  {size_str:>10s}  {mtime_str}")

        return "\n".join(output_lines)

    except Exception as e:
        if isinstance(e, (XCodeMCPError, InvalidParameterError, AccessDeniedError)):
            raise
        raise XCodeMCPError(f"Error listing directory: {e}")
