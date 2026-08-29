#!/usr/bin/env python3
"""build_project tool - Build an Xcode project"""

import json
import os
import re
import sys
import time
from typing import Optional

from drews_xcode_mcp.server import mcp, TOOL_BUILD
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_and_normalize_project_path
from drews_xcode_mcp.exceptions import InvalidParameterError, XCodeMCPError
from drews_xcode_mcp.utils.applescript import (
    build_open_and_wait_applescript,
    build_wait_for_completion_applescript,
    resolve_build_timeout,
    escape_applescript_string,
    run_applescript,
    show_notification,
    show_result_notification,
    show_error_notification,
    show_warning_notification,
    validate_max_lines,
)
from drews_xcode_mcp.utils.xcresult import extract_build_errors_and_warnings
from drews_xcode_mcp.utils.build_log_parser import (
    find_derived_data_for_project,
    aggregate_warnings_since_clean,
    get_scheme_name_for_uuid,
    snapshot_build_uuids,
    wait_for_new_build_uuid,
)

# How long to wait for our build's entry to appear in LogStoreManifest.plist
# after AppleScript reports the build action complete. Xcode usually finalizes
# the xcactivitylog + manifest within a few hundred ms; this is a generous cap.
MANIFEST_ENTRY_WAIT_SECONDS = 10.0


def _supplement_with_xcactivitylog_warnings(
    errors_json: str,
    project_path: str,
    include_warnings: Optional[bool],
    regex_filter: Optional[str],
    max_lines: int,
    pre_build_uuids: Optional[set] = None,
    unix_start_time: Optional[float] = None,
    scheme_name: Optional[str] = None,
) -> str:
    """
    Supplement build results with comprehensive warnings from xcactivitylog files.

    The AppleScript build log only contains warnings for files recompiled in the
    current build. xcactivitylog files contain warnings from all builds since the
    last clean, providing comprehensive coverage across incremental builds.

    xcactivitylog is the primary source. Any errors or warnings from the AppleScript
    build log not already present in xcactivitylog are preserved as supplements.

    pre_build_uuids and unix_start_time are used to wait for this build's
    manifest entry to be written before reading. Without them, aggregation can
    race against Xcode and return stale warnings from a previous build.

    scheme_name (when known) is passed through to wait_for_new_build_uuid so we
    only wait for entries matching our scheme, and to aggregate_warnings_since_clean
    so cross-scheme entries in the same DerivedData don't contaminate the result.
    If scheme_name is None (user wanted the active scheme), we look it up from the
    matched manifest entry after wait succeeds.
    """
    from drews_xcode_mcp.utils.xcresult import BUILD_WARNINGS_FORCED, BUILD_WARNINGS_ENABLED

    if BUILD_WARNINGS_FORCED is not None:
        show_warnings = BUILD_WARNINGS_FORCED
    else:
        show_warnings = include_warnings if include_warnings is not None else BUILD_WARNINGS_ENABLED

    if not show_warnings:
        return errors_json

    try:
        derived_data_path = find_derived_data_for_project(project_path)
        if not derived_data_path:
            return errors_json

        logs_dir = os.path.join(derived_data_path, "Logs", "Build")
        manifest_path = os.path.join(logs_dir, "LogStoreManifest.plist")

        if not os.path.exists(manifest_path):
            return errors_json

        # Wait for our build's manifest entry to appear so aggregation sees
        # the current build's compiled-files set and warnings, not stale ones
        # from the prior build. Falls through to the existing behavior on
        # timeout — degraded but not broken. The aggregation_status flag
        # captures the degradation so we can surface it in the JSON response.
        aggregation_status: Optional[dict] = None
        our_uuid: Optional[str] = None
        if pre_build_uuids is not None and unix_start_time is not None:
            our_uuid = wait_for_new_build_uuid(
                manifest_path,
                pre_build_uuids,
                unix_start_time,
                timeout_seconds=MANIFEST_ENTRY_WAIT_SECONDS,
                scheme_name=scheme_name,
            )
            if our_uuid is None:
                print(
                    f"Warning: timed out waiting for manifest entry after build "
                    f"of {project_path}; aggregated warnings may be stale.",
                    file=sys.stderr,
                )
                aggregation_status = {
                    'status': 'possibly_stale',
                    'reason': (
                        f"this build's manifest entry did not appear within "
                        f"{MANIFEST_ENTRY_WAIT_SECONDS}s; warnings may include "
                        f"stale entries from a prior build"
                    ),
                }

        # Use the user-provided scheme for aggregation filtering when known;
        # otherwise read the scheme name out of the matched manifest entry.
        # When neither is available (e.g. wait timed out and scheme was not
        # passed), pass None and aggregate without a scheme filter — the
        # action-prefix filter still excludes non-Build entries.
        aggregation_scheme = scheme_name
        if aggregation_scheme is None and our_uuid is not None:
            aggregation_scheme = get_scheme_name_for_uuid(manifest_path, our_uuid)

        xcactivity_result = aggregate_warnings_since_clean(
            manifest_path, logs_dir, scheme_name=aggregation_scheme,
        )
        if xcactivity_result.get('summary', {}).get('manifest_unreadable'):
            aggregation_status = {
                'status': 'unavailable',
                'reason': xcactivity_result['summary'].get('error', 'manifest unreadable'),
            }

        def _with_status(json_text: str) -> str:
            """Attach aggregation_status to the JSON payload, if set."""
            if aggregation_status is None:
                return json_text
            try:
                payload = json.loads(json_text)
            except json.JSONDecodeError:
                return json_text
            payload['warning_aggregation'] = aggregation_status
            return json.dumps(payload)

        xcactivity_items = xcactivity_result.get('aggregated_warnings', [])

        if not xcactivity_items:
            return _with_status(errors_json)

        # Apply regex_filter to xcactivitylog items if provided
        if regex_filter and regex_filter.strip():
            filter_re = re.compile(regex_filter)
            xcactivity_items = [
                w for w in xcactivity_items
                if filter_re.search(
                    f"{w['file']}:{w['line']}:{w['column']}: {w['type']}: {w['message']}"
                )
            ]
            if not xcactivity_items:
                return _with_status(errors_json)

        result = json.loads(errors_json)
        existing_text = result.get('errors_and_warnings', '')
        build_failed = result.get('summary', {}).get('build_failed', False)

        # Parse error/warning lines from existing AppleScript result
        error_re = re.compile(
            r'(:\d+:\d+: error:)|(^error\s*:)|(:\s+error:)', re.IGNORECASE
        )
        warning_re = re.compile(
            r'(:\d+:\d+: warning:)|(^warning\s*:)|(:\s+warning:)', re.IGNORECASE
        )
        flc_re = re.compile(r'(.+?):(\d+):(\d+): (?:warning|error):')

        existing_lines = existing_text.split('\n')
        applescript_error_lines = [l for l in existing_lines if error_re.search(l)]
        applescript_warning_lines = [l for l in existing_lines if warning_re.search(l)]

        # Build dedup set from xcactivitylog entries
        xcact_keys = set()
        for w in xcactivity_items:
            xcact_keys.add((w['file'], w['line'], w['column']))

        # Separate xcactivitylog items into error and warning text lines
        xcact_error_lines = []
        xcact_warning_lines = []
        for w in xcactivity_items:
            line = f"{w['file']}:{w['line']}:{w['column']}: {w['type']}: {w['message']}"
            if w['type'] == 'error':
                xcact_error_lines.append(line)
            else:
                xcact_warning_lines.append(line)

        # Find AppleScript lines not already in xcactivitylog
        extra_errors = []
        for line in applescript_error_lines:
            m = flc_re.match(line)
            if m:
                key = (m.group(1), int(m.group(2)), int(m.group(3)))
                if key not in xcact_keys:
                    extra_errors.append(line)
            else:
                extra_errors.append(line)

        extra_warnings = []
        for line in applescript_warning_lines:
            m = flc_re.match(line)
            if m:
                key = (m.group(1), int(m.group(2)), int(m.group(3)))
                if key not in xcact_keys:
                    extra_warnings.append(line)
            else:
                extra_warnings.append(line)

        # Combine: errors first (xcactivitylog + extras), then warnings
        all_error_lines = xcact_error_lines + extra_errors
        all_warning_lines = xcact_warning_lines + extra_warnings
        total_errors = len(all_error_lines)
        total_warnings = len(all_warning_lines)

        # Apply max_lines with errors prioritized
        combined = all_error_lines + all_warning_lines
        displayed_errors = min(total_errors, max_lines)
        displayed_warnings = max(0, min(total_warnings, max_lines - displayed_errors))
        if len(combined) > max_lines:
            combined = combined[:max_lines]

        # Build count message
        if build_failed:
            if total_errors > 0 and total_warnings > 0:
                count_msg = (
                    f"Build failed with {total_errors} error{'s' if total_errors != 1 else ''}"
                    f" and {total_warnings} warning{'s' if total_warnings != 1 else ''}."
                )
            elif total_errors > 0:
                count_msg = f"Build failed with {total_errors} error{'s' if total_errors != 1 else ''}."
            elif total_warnings > 0:
                count_msg = (
                    f"Build failed with 0 recognized errors"
                    f" and {total_warnings} warning{'s' if total_warnings != 1 else ''}."
                    f" See full log for failure details."
                )
            else:
                return _with_status(errors_json)
        else:
            if total_warnings > 0 and total_errors > 0:
                count_msg = (
                    f"Build succeeded with {total_errors} error{'s' if total_errors != 1 else ''}"
                    f" and {total_warnings} warning{'s' if total_warnings != 1 else ''}."
                )
            elif total_warnings > 0:
                count_msg = f"Build succeeded with {total_warnings} warning{'s' if total_warnings != 1 else ''}."
            else:
                return _with_status(errors_json)

        if total_errors + total_warnings > max_lines:
            count_msg += f" Showing first {len(combined)} of {total_errors + total_warnings}."

        important_list = "\n".join(combined)
        output_text = f"{count_msg}\n{important_list}" if combined else count_msg

        result['summary']['total_errors'] = total_errors
        result['summary']['total_warnings'] = total_warnings
        result['summary']['showing_errors'] = displayed_errors
        result['summary']['showing_warnings'] = displayed_warnings
        result['errors_and_warnings'] = output_text
        if aggregation_status is not None:
            result['warning_aggregation'] = aggregation_status

        return json.dumps(result)

    except (OSError, json.JSONDecodeError, KeyError) as e:
        # I/O failures, malformed JSON, or missing expected keys — degrade
        # gracefully and return the un-supplemented result. Programming errors
        # (AttributeError, TypeError) propagate so they're visible.
        print(f"Warning: xcactivitylog supplement failed: {e}", file=sys.stderr)
        return errors_json


@mcp.tool(annotations=TOOL_BUILD)
@describe_parameters_from_docstring
@apply_config
def build_project(project_path: str,
                 scheme: Optional[str] = None,
                 include_warnings: Optional[bool] = None,
                 regex_filter: Optional[str] = None,
                 max_lines: int = 25,
                 timeout: Optional[int] = None) -> str:
    """
    Build the specified Xcode project or workspace.

    Builds run for up to `timeout` seconds (default 600, i.e. 10 minutes) before
    timing out, which guards against a build that hangs indefinitely. Raise it
    for large projects whose cold build exceeds the default.

    Args:
        project_path: Path to an Xcode project or workspace directory.
        scheme: Name of the scheme to build. If not provided, uses the active scheme.
        include_warnings: Include warnings in build output. If not provided, uses global setting.
        regex_filter: Optional regex to filter error/warning lines
        max_lines: Maximum number of error/warning lines to show (default 25)
        timeout: Maximum seconds to wait for the build to complete. If not
            provided, defaults to 600. Must be a positive integer.

    Returns:
        Always returns JSON with format:
        {
            "full_log_path": "~/Library/Caches/xcode-mcp-server/logs/build-{hash}.txt",
            "summary": {"total_errors": N, "total_warnings": M, "showing_errors": X, "showing_warnings": Y},
            "errors_and_warnings": "Build failed with N errors...\nerror: ...\n..."
        }
        The errors_and_warnings field contains a summary message followed by the actual errors/warnings.
        Errors are prioritized over warnings - errors are shown first, then warnings fill remaining slots.
    """
    # Validate include_warnings parameter
    if include_warnings is not None and not isinstance(include_warnings, bool):
        raise InvalidParameterError("include_warnings must be a boolean value")

    max_lines = validate_max_lines(max_lines)

    # Resolve the build timeout up front so an invalid value errors immediately,
    # before any AppleScript runs.
    effective_timeout = resolve_build_timeout(timeout)

    # Validate regex_filter up front so a bad pattern produces a clear error
    # immediately, before any AppleScript runs. The supplement helper assumes
    # the pattern compiles.
    if regex_filter and regex_filter.strip():
        try:
            re.compile(regex_filter)
        except re.error as e:
            raise InvalidParameterError(f"Invalid regex_filter: {e}")

    # Validate and normalize path
    scheme_desc = scheme if scheme else "active scheme"
    normalized_path = validate_and_normalize_project_path(project_path, f"Building {scheme_desc} in")
    escaped_path = escape_applescript_string(normalized_path)

    # Show building notification
    project_name = os.path.basename(normalized_path)
    scheme_name = scheme if scheme else "active scheme"
    show_notification("Drew's Xcode MCP", subtitle=project_name, message=f"Building {scheme_name}")

    escaped_scheme = escape_applescript_string(scheme) if scheme else None
    script = (
        build_open_and_wait_applescript(escaped_path, escaped_scheme)
        + '    set actionResult to build workspaceDoc\n'
        + build_wait_for_completion_applescript("actionResult", effective_timeout)
        + '    set buildStatus to "unknown"\n'
        + '    try\n'
        + '        set buildStatus to status of actionResult as string\n'
        + '    end try\n'
        + '    return "BUILD_STATUS:" & buildStatus & "\n" & build log of actionResult\n'
        + 'end tell\n'
    )

    # Snapshot the manifest's build UUIDs and capture our start time right
    # before triggering the build, so we can later identify the entry our
    # build creates and avoid racing against Xcode's log finalization.
    pre_build_uuids = set()
    unix_start_time = time.time()
    derived_data_path = find_derived_data_for_project(normalized_path)
    if derived_data_path:
        manifest_path = os.path.join(derived_data_path, "Logs", "Build", "LogStoreManifest.plist")
        pre_build_uuids = snapshot_build_uuids(manifest_path)

    # The script polls inside AppleScript for up to effective_timeout; the
    # subprocess timeout must exceed that, with a small buffer for workspace
    # load and IPC overhead.
    success, output = run_applescript(script, timeout=effective_timeout + 60)

    if success:
        # Parse the BUILD_STATUS: prefix from the AppleScript output
        build_status = None
        build_log = output
        if output.startswith("BUILD_STATUS:"):
            newline_pos = output.find("\n")
            if newline_pos >= 0:
                build_status = output[len("BUILD_STATUS:"):newline_pos].strip()
                build_log = output[newline_pos + 1:]
            else:
                # No newline — output was just the status line (empty build log)
                build_status = output[len("BUILD_STATUS:"):].strip()
                build_log = ""

        # Always extract and format errors/warnings (returns JSON)
        errors_output = extract_build_errors_and_warnings(build_log, include_warnings, regex_filter, max_lines, build_status=build_status)

        # Supplement with comprehensive warnings from xcactivitylog files
        # (AppleScript build log only has warnings for files recompiled in this build)
        errors_output = _supplement_with_xcactivitylog_warnings(
            errors_output, normalized_path, include_warnings, regex_filter, max_lines,
            pre_build_uuids=pre_build_uuids, unix_start_time=unix_start_time,
            scheme_name=scheme,
        )

        # Parse JSON to show appropriate notification
        try:
            result = json.loads(errors_output)
            summary = result.get("summary", {})
            is_failed = summary.get("build_failed", False)
            total_errors = summary.get("total_errors", 0)
            total_warnings = summary.get("total_warnings", 0)

            if is_failed:
                if total_errors > 0:
                    show_error_notification(f"Build failed with {total_errors} error{'s' if total_errors != 1 else ''}", project_name)
                else:
                    show_error_notification("Build failed (see log for details)", project_name)
            elif total_warnings > 0:
                show_warning_notification(f"Build succeeded with {total_warnings} warning{'s' if total_warnings != 1 else ''}", project_name)
            else:
                show_result_notification(f"✅ Build succeeded", project_name)
        except json.JSONDecodeError:
            # Fallback if JSON parsing fails
            show_error_notification("Build failed", project_name)

        return errors_output
    else:
        show_error_notification("Build failed to start", project_name)
        raise XCodeMCPError(f"Build failed to start for scheme {scheme} in project {project_path}: {output}")
