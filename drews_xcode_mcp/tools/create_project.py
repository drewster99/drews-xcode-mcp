#!/usr/bin/env python3
"""create_project tool - Create a new Xcode project from a built-in template"""

import json
import os
import re
from typing import Optional

from drews_xcode_mcp.server import mcp, TOOL_CREATE
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_parent_for_new_project
from drews_xcode_mcp.exceptions import InvalidParameterError
from drews_xcode_mcp.utils.applescript import (
    show_notification,
    show_result_notification,
)
from drews_xcode_mcp.utils.project_templates import (
    generate_project,
    sanitize_to_identifier,
)
from drews_xcode_mcp.tools.get_xcode_projects import register_created_project


SUPPORTED_PLATFORMS = {"ios", "macos"}

# bundle_identifier and deployment_target are interpolated unquoted into
# project.pbxproj (PRODUCT_BUNDLE_IDENTIFIER / *_DEPLOYMENT_TARGET). Without
# validation a value containing ';', '{', whitespace, quotes, or newlines could
# inject or corrupt build settings. Restrict to the characters Apple actually
# allows so the generated pbxproj can't be broken out of.
# Reverse-DNS style: dot-separated segments, each a non-empty run of
# letters/digits/hyphens that starts and ends alphanumeric. This still blocks
# every pbxproj-breaking character but additionally rejects structurally
# malformed values the old `[A-Za-z0-9.\-]*` accepted (e.g. "com..example",
# "com.example." with a trailing dot, "a....").
_BUNDLE_IDENTIFIER_RE = re.compile(
    r'^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$'
)
# One to three dot-separated numeric components (e.g. "26", "17.0", "17.0.1");
# rejects nonsense like "1.2.3.4.5".
_DEPLOYMENT_TARGET_RE = re.compile(r'^\d+(\.\d+){0,2}$')


@mcp.tool(annotations=TOOL_CREATE)
@describe_parameters_from_docstring
@apply_config
def create_project(
    parent_directory: str,
    project_name: str,
    platform: str = "ios",
    bundle_identifier: str = "",
    deployment_target: str = "",
) -> str:
    """
    Create a new Xcode project with a SwiftUI app template.

    Creates a complete, buildable Xcode project with a SwiftUI app entry point,
    ContentView, and asset catalog. The project uses the modern Xcode 16+ format
    (objectVersion 77) with automatic file discovery.

    Args:
        parent_directory: Directory where the project folder will be created.
            Must be within the allowed folders configured for this server.
        project_name: Name of the project (e.g. "MyApp"). Used for the folder
            name, target name, and scheme name.
        platform: Target platform - "ios" or "macos" (case-insensitive).
            Defaults to "ios".
        bundle_identifier: Bundle identifier (e.g. "com.mycompany.MyApp").
            Defaults to "com.example.{ProjectName}".
        deployment_target: Minimum deployment target version (e.g. "26.0").
            Defaults to "26.0".

    Returns:
        JSON string with project_path, project_directory, platform,
        bundle_identifier, and files_created.
    """
    # Validate platform
    platform = platform.lower().strip()
    if platform not in SUPPORTED_PLATFORMS:
        raise InvalidParameterError(
            f"platform must be one of: {', '.join(sorted(SUPPORTED_PLATFORMS))}. "
            f"Got: '{platform}'"
        )

    # Validate optional template fields before they reach the pbxproj template.
    # Coerce None→"" defensively in case a client omits/nulls these.
    bundle_identifier = (bundle_identifier or "").strip()
    if bundle_identifier and not _BUNDLE_IDENTIFIER_RE.match(bundle_identifier):
        raise InvalidParameterError(
            "bundle_identifier may contain only letters, digits, hyphens, and "
            "dots, and must start with a letter or digit (e.g. 'com.example.MyApp')"
        )

    deployment_target = (deployment_target or "").strip()
    if deployment_target and not _DEPLOYMENT_TARGET_RE.match(deployment_target):
        raise InvalidParameterError(
            "deployment_target must be a version number such as '17.0' or '26.0'"
        )

    # Validate parent directory and project name
    parent_directory = validate_parent_for_new_project(parent_directory, project_name)

    show_notification(
        "Creating Project",
        project_name,
        f"Platform: {platform}",
    )

    # Generate the project
    result = generate_project(
        parent_dir=parent_directory,
        project_name=project_name,
        platform=platform,
        bundle_identifier=bundle_identifier or None,
        deployment_target=deployment_target or None,
    )

    # Register so get_xcode_projects finds it before Spotlight indexes it
    register_created_project(result["project_path"])

    show_result_notification(
        f"Created: {project_name}",
        f"Platform: {platform}",
    )

    # Compute the actual bundle identifier used
    if not bundle_identifier:
        identifier = sanitize_to_identifier(project_name)
        bundle_identifier = f"com.example.{identifier}"

    result["platform"] = platform
    result["bundle_identifier"] = bundle_identifier

    return json.dumps(result, indent=2)
