#!/usr/bin/env python3
"""list_project_tests tool - Enumerate tests via xcodebuild"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Optional

from drews_xcode_mcp.server import mcp, TOOL_BUILD
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.exceptions import XCodeMCPError
from drews_xcode_mcp.security import validate_and_normalize_project_path
from drews_xcode_mcp.utils.run_guard import exclusive_per_project
from drews_xcode_mcp.utils.applescript import (
    resolve_build_timeout,
    format_timeout_duration,
    show_notification,
    show_result_notification,
    show_error_notification,
)
from drews_xcode_mcp.utils.xcodebuild_query import (
    project_flag_for,
    get_active_scheme,
    get_first_scheme,
    resolve_buildable_destination,
)


def _short_error(message: str, limit: int = 200) -> str:
    """Condense an enumeration error to its first line, truncated.

    xcodebuild's bundle-load failures can be many lines of dyld search paths;
    the first line carries the cause, so collapse the rest.
    """
    first_line = str(message).strip().split('\n', 1)[0].strip()
    if len(first_line) > limit:
        first_line = first_line[:limit].rstrip() + '…'
    return first_line


def _collect_identifiers(enumeration: dict) -> tuple:
    """
    Extract enabled and disabled test identifiers from the flat-format JSON
    produced by `xcodebuild ... -enumerate-tests -test-enumeration-style flat
    -test-enumeration-format json`.

    The flat schema groups by test plan:
        {"values": [{"testPlan": "...",
                     "enabledTests": [{"identifier": "Bundle/Class/method()"}],
                     "disabledTests": [...]}]}

    Identifiers are globally unique across plans, so they are unioned. Returns
    (sorted_enabled, sorted_disabled).
    """
    enabled = set()
    disabled = set()
    for value in enumeration.get("values", []):
        for test in value.get("enabledTests", []):
            identifier = test.get("identifier")
            if identifier:
                enabled.add(identifier)
        for test in value.get("disabledTests", []):
            identifier = test.get("identifier")
            if identifier:
                disabled.add(identifier)
    return sorted(enabled), sorted(disabled)


@mcp.tool(annotations=TOOL_BUILD)
@describe_parameters_from_docstring
@apply_config
@exclusive_per_project
def list_project_tests(project_path: str,
                       scheme: Optional[str] = None,
                       timeout: Optional[int] = None) -> str:
    """
    List the tests in an Xcode project or workspace by asking xcodebuild to
    enumerate them.

    IMPORTANT: This may take a few minutes on the first call (or after a clean),
    because xcodebuild must build the project for testing before it can
    enumerate the tests — there is no accurate way to discover XCTest and Swift
    Testing tests without building the test bundle. The build reuses Xcode's
    normal DerivedData, so it is incremental: once built, subsequent calls
    return in a second or two, and a build the user already did in Xcode is
    reused rather than repeated. Tests are NOT executed during enumeration.

    Args:
        project_path: Path to Xcode project/workspace directory.
        scheme: Scheme to enumerate. If not provided, the active scheme (top of
            Xcode's scheme menu) is used, falling back to the first scheme.
        timeout: Maximum seconds to wait for the build-for-testing plus
            enumeration. If not provided, defaults to 600 (10 minutes). Raise it
            for large projects whose first build exceeds the default.

    Returns:
        A newline-separated list of test identifiers in the format
        Bundle/Class/testMethod(), directly usable with run_project_tests.
        Disabled tests, if any, are listed separately. Returns a clear message
        if the scheme has no test target or the build fails.
    """
    project_path = validate_and_normalize_project_path(project_path, "list_project_tests")
    project_name = os.path.basename(project_path)

    # Resolve an invalid timeout up front so it errors before any build runs.
    effective_timeout = resolve_build_timeout(timeout)

    # Default to the active scheme (top of Xcode's scheme menu) so enumeration
    # matches what run_project_tests would use, falling back to the first scheme.
    if not scheme:
        scheme = get_active_scheme(project_path) or get_first_scheme(project_path)
        if not scheme:
            raise XCodeMCPError(
                f"Could not determine a scheme for {project_name}. "
                "Please provide a scheme name."
            )

    # xcodebuild's test action requires a destination (it refuses to build for
    # testing without one). Any compatible destination yields the same test
    # list; the resolver prefers a simulator so the test bundle loads reliably.
    destination = resolve_buildable_destination(project_path, scheme)
    if not destination:
        raise XCodeMCPError(
            f"Could not find a buildable run destination for scheme '{scheme}'. "
            "Open the project in Xcode and confirm a device or simulator is "
            "available for this scheme."
        )

    show_notification("Drew's Xcode MCP", subtitle=project_name, message="Enumerating tests")

    # Write the enumeration to a temp file rather than stdout so build log noise
    # never contaminates the JSON we parse. xcodebuild refuses to overwrite an
    # existing output path, so hand it paths inside a fresh temp directory
    # without pre-creating them.
    tmpdir = tempfile.mkdtemp(prefix='xcode-mcp-tests-')
    output_path = os.path.join(tmpdir, 'tests.json')

    # The test action always emits a result bundle; left to default it lands in
    # DerivedData/Logs/Test, where get_latest_test_results would later mistake
    # this enumeration (no executed tests) for a real test run. Redirect it into
    # the temp dir so enumeration leaves Logs/Test untouched.
    result_bundle_path = os.path.join(tmpdir, 'result.xcresult')

    cmd = [
        'xcodebuild', 'test',
        project_flag_for(project_path), project_path,
        '-scheme', scheme,
        '-destination', destination,
        '-resultBundlePath', result_bundle_path,
        '-enumerate-tests',
        '-test-enumeration-style', 'flat',
        '-test-enumeration-format', 'json',
        '-test-enumeration-output-path', output_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=effective_timeout)
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmpdir, ignore_errors=True)
        duration = format_timeout_duration(effective_timeout)
        show_error_notification(f"Enumerate timeout ({duration})", project_name)
        return (f"⏳ Test enumeration did not complete within {duration}. "
                "The build for testing may need a higher timeout on first run.")
    except (OSError, subprocess.SubprocessError) as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        show_error_notification("Error enumerating tests", str(e))
        raise XCodeMCPError(f"Error enumerating tests for {project_name}: {e}")

    try:
        with open(output_path, 'r') as f:
            raw = f.read().strip()
    except OSError:
        raw = ""
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # An empty enumeration file means xcodebuild failed before it could
    # enumerate — almost always a build error or a scheme with no test target.
    if not raw:
        combined = result.stderr + result.stdout
        if 'not currently configured for the test action' in combined:
            show_result_notification("No test target", project_name)
            return (f"Scheme '{scheme}' has no test target configured, so there "
                    "are no tests to enumerate. If another scheme holds the "
                    "tests, pass it as the `scheme` argument.")
        # Prefer the actual `error:` lines; xcodebuild prints its full usage on
        # failure, so a naive tail would surface help text instead of the cause.
        error_lines = [line.strip() for line in combined.split('\n')
                       if 'error:' in line and 'usage:' not in line.lower()]
        if error_lines:
            snippet = '\n'.join(error_lines[:15])
        else:
            tail = combined.strip().split('\n')
            snippet = '\n'.join(line for line in tail[-15:] if line.strip())
        show_error_notification("Could not enumerate tests", project_name)
        return (f"❌ Could not enumerate tests for scheme '{scheme}' "
                f"(the build for testing may have failed):\n\n{snippet}")

    try:
        enumeration = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"warn: enumerate-tests produced invalid JSON: {e}", file=sys.stderr)
        show_error_notification("Could not parse test list", project_name)
        return f"❌ Could not parse the enumerated test list for scheme '{scheme}'."

    enabled, disabled = _collect_identifiers(enumeration)
    enum_errors = [_short_error(e) for e in enumeration.get("errors", []) if str(e).strip()]

    # Errors with no identifiers means a bundle failed to load before any test
    # could be listed — surface that rather than reporting "0 tests".
    if not enabled and not disabled:
        if enum_errors:
            show_error_notification("Could not enumerate tests", project_name)
            joined = '\n'.join(enum_errors[:10])
            return (f"❌ Could not enumerate tests for scheme '{scheme}'. "
                    f"xcodebuild reported:\n\n{joined}")
        show_result_notification("Found 0 tests", project_name)
        return f"No tests found in scheme '{scheme}'."

    total = len(enabled) + len(disabled)
    show_result_notification(f"Found {total} test{'s' if total != 1 else ''}", project_name)

    lines = list(enabled)
    if disabled:
        lines.append("")
        lines.append(f"Disabled ({len(disabled)}):")
        lines.extend(disabled)
    # Partial results: some identifiers came back but a bundle also errored, so
    # the list may be incomplete. Flag it instead of silently under-reporting.
    if enum_errors:
        lines.append("")
        lines.append(f"⚠️ Enumeration reported {len(enum_errors)} error(s); "
                     "this list may be incomplete:")
        lines.extend(enum_errors[:10])
    lines.append("")
    lines.append("Use `run_project_tests` to run all tests, or pass specific "
                 "identifiers above to run selected tests.")
    return "\n".join(lines)
