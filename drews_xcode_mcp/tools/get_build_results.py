#!/usr/bin/env python3
"""get_build_results tool - Aggregate build warnings across multiple builds since last clean"""

import os
import json
from typing import Optional

from drews_xcode_mcp.server import mcp, TOOL_READONLY
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_and_normalize_project_path
from drews_xcode_mcp.exceptions import XCodeMCPError
from drews_xcode_mcp.utils.build_log_parser import (
    find_derived_data_for_project,
    aggregate_warnings_since_clean
)


@mcp.tool(annotations=TOOL_READONLY)
@describe_parameters_from_docstring
@apply_config
def get_build_results(project_path: str,
                     max_warnings: int = 50) -> str:
    """
    Get aggregated build errors and warnings from all builds since the last clean operation.

    This tool addresses the issue where incremental builds only show warnings for recompiled files.
    It parses the build log manifest (LogStoreManifest.plist) and aggregates warnings from all
    builds since the last clean, excluding warnings from files that were subsequently recompiled.

    Strategy:
    1. Locate the project's DerivedData/Logs/Build directory
    2. Parse LogStoreManifest.plist to find all builds since last clean
    3. For each build, parse the .xcactivitylog file to extract:
       - Warnings and errors with file/line/column/message
       - List of files compiled in that build
    4. Aggregate warnings, keeping only the most recent warning for each file
    5. If a file was recompiled in a later build, use warnings from that later build

    This ensures you see all current warnings even after incremental builds.

    Args:
        project_path: Path to an Xcode project or workspace directory
        max_warnings: Maximum number of warnings to show in response (default 50)

    Returns:
        JSON string with format:
        {
            "derived_data_path": "/path/to/DerivedData/...",
            "summary": {
                "total_builds": N,
                "builds_since_clean": M,
                "builds_analyzed": K,
                "clean_info": "...",
                "total_warnings": X,
                "warnings_by_type": {"warnings": W, "errors": E},
                "unique_files_with_warnings": F,
                "files_recompiled_multiple_times": R
            },
            "aggregated_warnings": [
                {
                    "file": "/path/to/File.swift",
                    "line": 123,
                    "column": 45,
                    "message": "...",
                    "type": "warning"
                }
            ],
            "files_with_multiple_builds": [
                {
                    "file": "/path/to/File.swift",
                    "builds": 3,
                    "warnings_excluded": 2
                }
            ],
            "builds_analyzed": [
                {
                    "uuid": "...",
                    "title": "Build FunVoice",
                    "time": 782185536.0,
                    "warnings_found": 4,
                    "files_compiled": 26
                }
            ]
        }

        Note: Only the first max_warnings warnings are included in aggregated_warnings.
        The summary counts reflect the total before limiting.
    """
    # Validate and normalize path
    normalized_path = validate_and_normalize_project_path(project_path, "Getting build results for")

    # Find the DerivedData directory for this project
    derived_data_path = find_derived_data_for_project(normalized_path)

    if not derived_data_path:
        raise XCodeMCPError(
            f"Could not find DerivedData directory for project. "
            f"Make sure the project has been built at least once in Xcode."
        )

    # Locate the build logs directory and manifest
    logs_dir = os.path.join(derived_data_path, "Logs", "Build")
    manifest_path = os.path.join(logs_dir, "LogStoreManifest.plist")

    if not os.path.exists(logs_dir):
        raise XCodeMCPError(
            f"Build logs directory not found: {logs_dir}\n"
            f"Make sure the project has been built at least once in Xcode."
        )

    if not os.path.exists(manifest_path):
        raise XCodeMCPError(
            f"Build log manifest not found: {manifest_path}\n"
            f"Make sure the project has been built at least once in Xcode."
        )

    # Aggregate warnings since last clean
    result = aggregate_warnings_since_clean(manifest_path, logs_dir)

    # Add derived data path to result
    result['derived_data_path'] = derived_data_path

    # Limit warnings to max_warnings for display
    aggregated_warnings = result.get('aggregated_warnings', [])
    total_warnings = len(aggregated_warnings)

    if total_warnings > max_warnings:
        result['aggregated_warnings'] = aggregated_warnings[:max_warnings]
        result['display_note'] = (
            f"Showing first {max_warnings} of {total_warnings} warnings. "
            f"The summary counts reflect all warnings. Increase max_warnings parameter to see more."
        )

    # Format as JSON
    return json.dumps(result, indent=2)
