#!/usr/bin/env python3
"""xcresult and build log utilities"""

import os
import sys
import subprocess
import json
import re
import time
import datetime
from typing import Optional, Tuple

from drews_xcode_mcp.exceptions import InvalidParameterError
from drews_xcode_mcp.utils.paths import LOG_DIR
from drews_xcode_mcp.utils.build_log_parser import select_derived_data_dirs_for_project

# Global build warning settings - initialized by CLI.
# These are set once during startup and then read by every concurrent build
# tool invocation. We use a one-way latch (`_BUILD_WARNINGS_LOCKED`) to make
# the "set once" contract enforceable: once the CLI calls
# freeze_build_warnings_settings() before mcp.run(), any subsequent mutation
# is a programming error and raises rather than racing readers.
BUILD_WARNINGS_ENABLED = True
BUILD_WARNINGS_FORCED = None  # True if forced on, False if forced off, None if not forced
_BUILD_WARNINGS_LOCKED = False


def set_build_warnings_enabled(enabled: bool, forced: bool = False):
    """Set the global build warnings setting.

    Must be called during CLI startup, before freeze_build_warnings_settings()
    is invoked. After freezing, this raises to prevent accidental concurrent
    mutation by tool code.
    """
    global BUILD_WARNINGS_ENABLED, BUILD_WARNINGS_FORCED
    if _BUILD_WARNINGS_LOCKED:
        raise RuntimeError(
            "Build warning settings are frozen after server startup; "
            "set_build_warnings_enabled must be called before mcp.run()."
        )
    BUILD_WARNINGS_ENABLED = enabled
    BUILD_WARNINGS_FORCED = enabled if forced else None


def freeze_build_warnings_settings():
    """Lock further mutation of BUILD_WARNINGS_*. Called by the CLI right
    before the FastMCP run loop starts."""
    global _BUILD_WARNINGS_LOCKED
    _BUILD_WARNINGS_LOCKED = True


# OSLog message types (OSLogType) as they appear in a ConsoleSessionSection's
# log entries. The older ConsoleLogSection pre-classified severity into its
# `kind` field; the session format exposes the raw numeric type instead, so it
# has to be mapped back onto the kinds _format_structured_logs classifies by.
_OS_LOG_TYPE_TO_KIND = {
    '0': 'osLog',   # default
    '1': 'osLog',   # info
    '2': 'osLog',   # debug
    '16': 'error',
    '17': 'fault',
}


def _unwrap_xcresult_object(node):
    """
    Collapse xcresulttool's typed JSON into plain Python values.

    The legacy object API wraps every value as {"_type": ..., "_value": ...} and
    every array as {"_type": ..., "_values": [...]}. Stripping that up front lets
    the rest of the code read the structure like ordinary JSON.
    """
    if isinstance(node, dict):
        if '_value' in node:
            return node['_value']
        if '_values' in node:
            return [_unwrap_xcresult_object(value) for value in node['_values']]
        return {key: _unwrap_xcresult_object(value)
                for key, value in node.items() if key != '_type'}
    if isinstance(node, list):
        return [_unwrap_xcresult_object(value) for value in node]
    return node


def _get_xcresult_object(xcresult_path: str, object_id: Optional[str] = None) -> Optional[dict]:
    """
    Read one object out of a result bundle, or None if it cannot be read.

    Uses `xcresulttool get object --legacy`, which Apple has marked deprecated.
    It is the only route to a bundle's ConsoleSessionSection today — the
    supported `get log --type console` command does not surface it (see
    _console_entries_from_session_section) — so the deprecation is accepted
    knowingly rather than overlooked.

    Args:
        xcresult_path: Path to the .xcresult bundle.
        object_id: Id of the object to read. When omitted, reads the bundle's
            root ActionsInvocationRecord.
    """
    command = ['xcrun', 'xcresulttool', 'get', 'object', '--legacy',
               '--path', xcresult_path, '--format', 'json']
    if object_id:
        command += ['--id', object_id]

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"Warning: could not read xcresult object: {e}", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(f"Warning: could not read xcresult object: {result.stderr.strip()[:200]}",
              file=sys.stderr)
        return None

    try:
        return _unwrap_xcresult_object(json.loads(result.stdout))
    except json.JSONDecodeError as e:
        print(f"Warning: could not parse xcresult object: {e}", file=sys.stderr)
        return None


def _console_entries_from_session_section(xcresult_path: str) -> list:
    """
    Read console output from a bundle's ConsoleSessionSection, returning [] when
    the bundle has none.

    Xcode 27 moved runtime console output into a new ConsoleSessionSection and
    left the ConsoleLogSection that `get log --type console` reads as an empty
    stub — so that command succeeds and reports nothing rather than failing,
    which is why a run under Xcode 27 appeared to produce no output at all.
    Bundles from earlier Xcode versions carry no consoleSessionRef and return []
    here immediately.

    Only the app's own output is kept. A session also records several hundred
    LLDB `progress` events (symbol loading and similar); the old console section
    never included those, and surfacing them would bury the app's output.
    """
    root = _get_xcresult_object(xcresult_path)
    if not root:
        return []

    session_ref = None
    for action in root.get('actions') or []:
        reference = (action.get('actionResult') or {}).get('consoleSessionRef')
        if isinstance(reference, dict) and reference.get('id'):
            session_ref = reference['id']
            break

    if not session_ref:
        return []

    section = _get_xcresult_object(xcresult_path, session_ref)
    if not section:
        return []

    entries = []
    for item in section.get('items') or []:
        content = (item.get('groupIO') or {}).get('endpointIO', {}).get('content', {})

        if 'log' in content:
            log = content.get('log') or {}
            message = (log.get('message') or '').strip()
            if not message:
                continue
            entry = {
                'kind': _OS_LOG_TYPE_TO_KIND.get(str(log.get('messageType')), 'osLog'),
                'content': message,
            }
            if log.get('subsystem'):
                entry['subsystem'] = log['subsystem']
            if log.get('category'):
                entry['category'] = log['category']
            entries.append(entry)

        elif 'data' in content:
            # Raw stdout/stderr. A single chunk can carry many lines, so split it
            # — the formatter treats one entry as one line for context and
            # regex matching.
            for line in (content.get('data') or '').splitlines():
                line = line.strip()
                if line:
                    entries.append({'kind': 'output', 'content': line})

    for index, entry in enumerate(entries, start=1):
        entry['line'] = index

    if entries:
        print(f"Read {len(entries)} console entries from ConsoleSessionSection", file=sys.stderr)
    return entries


def _console_entries_from_legacy_log(log_data: dict) -> list:
    """Normalize the items returned by `get log --type console` into console entries."""
    entries = []
    for idx, item in enumerate(log_data.get('items', []), start=1):
        content = item.get('content', '').strip()
        if not content:
            continue

        entry = {
            'line': idx,
            'kind': item.get('kind', 'unknown'),
            'content': content,
        }

        log_data_obj = item.get('logData', {})
        if log_data_obj:
            if log_data_obj.get('subsystem'):
                entry['subsystem'] = log_data_obj['subsystem']
            if log_data_obj.get('category'):
                entry['category'] = log_data_obj['category']

        entries.append(entry)
    return entries


def extract_console_logs_from_xcresult(xcresult_path: str,
                                      regex_filter: Optional[str] = None,
                                      max_lines: int = 20) -> Tuple[bool, str]:
    """
    Extract console logs from xcresult bundle and return structured JSON.

    Args:
        xcresult_path: Path to the .xcresult file
        regex_filter: Optional regex pattern to find matching lines
        max_lines: Maximum matching lines to return (default 20)

    Returns:
        Tuple of (success, json_output_or_error_message)
    """
    # The xcresult file may still be finalizing, so retry a few times
    max_retries = 7
    retry_delay = 1

    for attempt in range(max_retries):
        try:
            if attempt > 0:
                print(f"Retry attempt {attempt + 1}/{max_retries} after {retry_delay}s delay...", file=sys.stderr)
                time.sleep(retry_delay)

            result = subprocess.run(
                ['xcrun', 'xcresulttool', 'get', 'log',
                 '--path', xcresult_path,
                 '--type', 'console'],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                if "root ID is missing" in result.stderr and attempt < max_retries - 1:
                    print(f"xcresult not ready yet: {result.stderr.strip()}", file=sys.stderr)
                    continue
                return False, f"Failed to extract console logs: {result.stderr}"

            # Success - break out of retry loop
            break

        except subprocess.TimeoutExpired:
            if attempt < max_retries - 1:
                continue
            return False, "Timeout extracting console logs"
        except Exception as e:
            if attempt < max_retries - 1:
                continue
            return False, f"Error extracting console logs: {e}"

    # Parse the JSON output
    try:
        log_data = json.loads(result.stdout)

        # Extract ALL log entries (no filtering at this stage)
        all_logs = _console_entries_from_legacy_log(log_data)

        # The supported command above reads the bundle's ConsoleLogSection,
        # which Xcode 27 leaves as an empty stub after moving console output to
        # a ConsoleSessionSection. It reports success with nothing in it, so an
        # empty result is the signal to look for a session section. Checking it
        # only when nothing was found keeps the supported path primary — if a
        # later Xcode populates ConsoleLogSection again, this stops being used
        # without a code change — and costs an extra lookup only on runs that
        # produced no output.
        if not all_logs:
            all_logs = _console_entries_from_session_section(xcresult_path)

        if not all_logs:
            return True, json.dumps({"summary": {"total_lines": 0}, "full_log_path": None})

        # Format the output using helper function
        output = _format_structured_logs(all_logs, xcresult_path, regex_filter, max_lines)
        return True, output

    except json.JSONDecodeError as e:
        return False, f"Failed to parse console logs: {e}"
    except InvalidParameterError:
        raise
    except Exception as e:
        return False, f"Error processing console logs: {e}"


def _format_structured_logs(all_logs: list, xcresult_path: str,
                           regex_filter: Optional[str], max_lines: int) -> str:
    """
    Format structured logs into JSON with priority-based selection and write full unfiltered log file.

    Strategy:
    - Write ALL logs to unfiltered plaintext file
    - First 2 errors/faults (3 lines before, 2 after each) - from unfiltered logs
    - Last 3 errors/faults (5 lines before, 4 after each) - from unfiltered logs
    - First 2 warnings (2 lines before, 1 after each) - from unfiltered logs
    - Last 10 info/debug lines - from unfiltered logs
    - If regex_filter: Up to max_lines matching lines

    Args:
        all_logs: Complete list of all log entries (unfiltered)
        xcresult_path: Path to xcresult bundle (used for temp file naming)
        regex_filter: Optional regex to find matching lines
        max_lines: Maximum matching lines to include in response

    Returns:
        JSON string with formatted output including full_log_path
    """
    import hashlib

    # Categorize logs by severity
    errors_and_faults = []
    warnings = []
    others = []

    for log in all_logs:
        kind = log.get('kind', 'unknown')
        if kind in ['error', 'fault', 'osLogDefaultErrorFault']:
            errors_and_faults.append(log)
        elif kind in ['warning']:
            warnings.append(log)
        else:
            others.append(log)

    # Write complete UNFILTERED plaintext log to a per-user cache directory.
    os.makedirs(LOG_DIR, exist_ok=True)

    xcresult_hash = hashlib.md5(xcresult_path.encode()).hexdigest()[:8]
    temp_log_path = os.path.join(LOG_DIR, f"runtime-{xcresult_hash}.txt")

    try:
        with open(temp_log_path, 'w') as f:
            for log in all_logs:
                f.write(f"{log['content']}\n")
    except Exception as e:
        print(f"Warning: Failed to write full log to {temp_log_path}: {e}", file=sys.stderr)
        temp_log_path = None

    # Build summary
    summary = {
        "total_lines": len(all_logs),
        "errors_and_faults": len(errors_and_faults),
        "warnings": len(warnings),
        "info_debug": len(others)
    }

    # Find matching lines if regex_filter is provided
    matching_lines = []
    total_matching = 0
    if regex_filter and regex_filter.strip():
        try:
            for log in all_logs:
                if re.search(regex_filter, log['content']):
                    total_matching += 1
                    if len(matching_lines) < max_lines:
                        matching_lines.append({
                            'line': log['line'],
                            'content': log['content'],
                            'kind': log['kind'],
                            'subsystem': log.get('subsystem'),
                            'category': log.get('category')
                        })
            summary['matching_lines'] = total_matching
        except re.error as e:
            raise InvalidParameterError(f"Invalid regex pattern: {e}")

    # Helper function to get context lines from ALL logs (unfiltered)
    def get_context(log_list, target_log, lines_before, lines_after):
        """Get context lines around a target log entry."""
        target_line = target_log['line']
        context_before = []
        context_after = []

        for log in log_list:
            line_num = log['line']
            if target_line - lines_before <= line_num < target_line:
                context_before.append({
                    'line': line_num,
                    'content': log['content']
                })
            elif target_line < line_num <= target_line + lines_after:
                context_after.append({
                    'line': line_num,
                    'content': log['content']
                })

        return context_before, context_after

    # Select errors/faults with context (from ALL unfiltered logs)
    selected_errors = []
    if errors_and_faults:
        # First 2 errors (3 before, 2 after)
        for err in errors_and_faults[:2]:
            before, after = get_context(all_logs, err, 3, 2)
            selected_errors.append({
                'line': err['line'],
                'content': err['content'],
                'kind': err['kind'],
                'subsystem': err.get('subsystem'),
                'category': err.get('category'),
                'context_before': before,
                'context_after': after
            })

        # Last 3 errors (5 before, 4 after) - avoid duplicates if < 8 total
        if len(errors_and_faults) > 5:
            for err in errors_and_faults[-3:]:
                before, after = get_context(all_logs, err, 5, 4)
                selected_errors.append({
                    'line': err['line'],
                    'content': err['content'],
                    'kind': err['kind'],
                    'subsystem': err.get('subsystem'),
                    'category': err.get('category'),
                    'context_before': before,
                    'context_after': after
                })
        elif len(errors_and_faults) > 2:
            # If 3-5 total, just add the remaining ones with context
            for err in errors_and_faults[2:]:
                before, after = get_context(all_logs, err, 5, 4)
                selected_errors.append({
                    'line': err['line'],
                    'content': err['content'],
                    'kind': err['kind'],
                    'subsystem': err.get('subsystem'),
                    'category': err.get('category'),
                    'context_before': before,
                    'context_after': after
                })

    # Select warnings with context (from ALL unfiltered logs)
    selected_warnings = []
    for warn in warnings[:2]:
        before, after = get_context(all_logs, warn, 2, 1)
        selected_warnings.append({
            'line': warn['line'],
            'content': warn['content'],
            'kind': warn['kind'],
            'subsystem': warn.get('subsystem'),
            'category': warn.get('category'),
            'context_before': before,
            'context_after': after
        })

    # Get last 10 info/debug lines (from ALL unfiltered logs)
    trailing_info = []
    for log in others[-10:]:
        trailing_info.append({
            'line': log['line'],
            'content': log['content'],
            'kind': log.get('kind', 'unknown')
        })

    # Build output
    output = {
        "full_log_path": temp_log_path,
        "summary": summary
    }

    if selected_errors:
        output["errors_and_faults"] = selected_errors
        if len(errors_and_faults) > len(selected_errors):
            output["errors_note"] = f"Showing {len(selected_errors)} of {len(errors_and_faults)} errors/faults (first 2 and last 3). Use Read tool with full_log_path for complete log."

    if selected_warnings:
        output["warnings"] = selected_warnings
        if len(warnings) > len(selected_warnings):
            output["warnings_note"] = f"Showing {len(selected_warnings)} of {len(warnings)} warnings (first 2). Use Read tool with full_log_path for complete log."

    if matching_lines:
        output["matching_lines"] = matching_lines
        if len(matching_lines) < summary.get('matching_lines', 0):
            output["matching_note"] = f"Showing first {len(matching_lines)} of {summary['matching_lines']} matching lines. Use Read tool with full_log_path and grep for more."

    if trailing_info:
        output["trailing_info"] = trailing_info

    return json.dumps(output, indent=2)


def extract_build_errors_and_warnings(build_log: str,
                                     include_warnings: Optional[bool] = None,
                                     regex_filter: Optional[str] = None,
                                     max_lines: int = 25,
                                     build_status: Optional[str] = None) -> str:
    """
    Extract and format errors and warnings from a build log using regex pattern matching.

    Uses Xcode diagnostic format patterns to match compiler errors and warnings:
    - file:line:column: error: (typical compiler output)
    - ^error: at start of line (standalone errors like linker errors)

    This avoids false positives from Objective-C method signatures like error:(NSError**)error
    and code snippets in warning messages.

    Writes the complete unfiltered build log to ~/Library/Caches/xcode-mcp-server/logs/build-{hash}.txt
    for full analysis.

    Prioritizes errors over warnings: shows up to max_lines total with errors first, then
    warnings to fill remaining slots. Optional regex_filter can further filter the error/warning
    lines before limiting.

    Args:
        build_log: The raw build log output from Xcode
        include_warnings: Include warnings in output. If not provided, uses global setting.
                         Note: Command-line flags override this parameter if set.
        regex_filter: Optional regex to further filter error/warning lines
        max_lines: Maximum number of error/warning lines to include (default 25)
        build_status: The status string from Xcode's scheme action result
                     (e.g. "succeeded", "failed", "error occurred").
                     When provided, used to detect build failure even if no errors
                     match the regex patterns (signing errors, provisioning issues, etc.).

    Returns:
        JSON string with format:
        {
            "full_log_path": "~/Library/Caches/xcode-mcp-server/logs/build-{hash}.txt",
            "summary": {
                "build_failed": bool,
                "total_errors": N,
                "total_warnings": M,
                "showing_errors": X,
                "showing_warnings": Y
            },
            "errors_and_warnings": "Build failed with N errors...\nerror: ...\n..."
        }
    """
    import hashlib

    # Determine whether to include warnings
    # Command-line flags override function parameter (user control > LLM control)
    if BUILD_WARNINGS_FORCED is not None:
        # User explicitly set a command-line flag to force behavior
        show_warnings = BUILD_WARNINGS_FORCED
    else:
        # No forcing, use function parameter or default
        show_warnings = include_warnings if include_warnings is not None else BUILD_WARNINGS_ENABLED

    # Write complete UNFILTERED build log to a per-user cache directory.
    os.makedirs(LOG_DIR, exist_ok=True)

    build_hash = hashlib.md5(build_log.encode()).hexdigest()[:8]
    temp_log_path = os.path.join(LOG_DIR, f"build-{build_hash}.txt")

    try:
        with open(temp_log_path, 'w') as f:
            f.write(build_log)
    except Exception as e:
        print(f"Warning: Failed to write full log to {temp_log_path}: {e}", file=sys.stderr)
        temp_log_path = None

    output_lines = build_log.split("\n")

    # Determine build outcome from Xcode's status property when available
    log_says_failed = any(line.strip().lower() == "build failed" for line in output_lines)
    if build_status is not None:
        build_failed = build_status.lower() not in ("succeeded",)
        # Safety net: if the log explicitly says "Build failed" but AppleScript
        # reported success (e.g. linker failures in debug dylib linking), trust the log
        if not build_failed and log_says_failed:
            build_failed = True
    else:
        build_failed = log_says_failed

    error_lines = []
    warning_lines = []

    # Pattern for compiler errors/warnings in Xcode diagnostic format:
    # - file:line:column: error: message (typical compiler output)
    # - ^error: at start of line (standalone errors like linker errors)
    # - path: error: message (project-level errors like missing packages, signing)
    # The leading \s+ in the third alternative avoids false positives from
    # Objective-C method signatures like "error:(NSError**)error"
    error_pattern = re.compile(r'(:\d+:\d+: error:)|(^error\s*:)|(:\s+error:)', re.IGNORECASE | re.MULTILINE)
    warning_pattern = re.compile(r'(:\d+:\d+: warning:)|(^warning\s*:)|(:\s+warning:)', re.IGNORECASE | re.MULTILINE)

    # Single iteration through output lines to extract errors/warnings
    for line in output_lines:
        if error_pattern.search(line):
            error_lines.append(line)
        elif show_warnings and warning_pattern.search(line):
            warning_lines.append(line)

    # Store total counts before filtering
    total_errors = len(error_lines)
    total_warnings = len(warning_lines)

    # Apply regex filter if provided
    if regex_filter and regex_filter.strip():
        try:
            filter_pattern = re.compile(regex_filter)
            error_lines = [line for line in error_lines if filter_pattern.search(line)]
            warning_lines = [line for line in warning_lines if filter_pattern.search(line)]
        except re.error as e:
            raise InvalidParameterError(f"Invalid regex pattern: {e}")

    # Combine errors first, then warnings
    important_lines = error_lines + warning_lines

    # Calculate what we're actually showing
    displayed_errors = min(len(error_lines), max_lines)
    displayed_warnings = 0 if len(error_lines) >= max_lines else min(len(warning_lines), max_lines - len(error_lines))

    # Limit to max_lines
    if len(important_lines) > max_lines:
        important_lines = important_lines[:max_lines]

    important_list = "\n".join(important_lines)

    # Build appropriate message based on what we found
    if error_lines and warning_lines:
        # Build detailed count message
        count_msg = f"Build failed with {total_errors} error{'s' if total_errors != 1 else ''} and {total_warnings} warning{'s' if total_warnings != 1 else ''}."
        if total_errors + total_warnings > max_lines:
            if displayed_warnings == 0:
                # Showing only errors, warnings excluded
                count_msg += f" Showing first {displayed_errors} of {total_errors} errors."
            else:
                # Showing errors and warnings, but some may be truncated
                error_part = f"all {displayed_errors} error{'s' if displayed_errors != 1 else ''}" if displayed_errors == len(error_lines) else f"first {displayed_errors} of {len(error_lines)} errors"
                warning_part = f"first {displayed_warnings} of {len(warning_lines)} warnings"
                count_msg += f" Showing {error_part} and {warning_part}."
        output_text = f"{count_msg}\n{important_list}"
    elif error_lines:
        count_msg = f"Build failed with {total_errors} error{'s' if total_errors != 1 else ''}."
        if len(error_lines) > max_lines:
            count_msg += f" Showing first {max_lines} of {total_errors} errors."
        output_text = f"{count_msg}\n{important_list}"
    elif warning_lines:
        if build_failed:
            # Build failed but only warnings matched (no error patterns) — e.g. signing failure after partial compilation
            tail_lines = [l for l in output_lines if l.strip()][-max_lines:]
            tail_text = "\n".join(tail_lines)
            count_msg = f"Build failed with 0 recognized errors and {total_warnings} warning{'s' if total_warnings != 1 else ''}. See log tail and full log for details."
            output_text = f"{count_msg}\n{tail_text}"
            total_errors = 1  # Signal failure in summary
        else:
            count_msg = f"Build succeeded with {total_warnings} warning{'s' if total_warnings != 1 else ''}."
            if len(warning_lines) > max_lines:
                count_msg += f" Showing first {max_lines} of {total_warnings} warnings."
            output_text = f"{count_msg}\n{important_list}"
    else:
        if build_failed:
            # Build failed but no errors matched our regex patterns
            # (e.g. signing errors, provisioning issues, validation failures)
            tail_lines = [l for l in output_lines if l.strip()][-max_lines:]
            tail_text = "\n".join(tail_lines)
            output_text = f"Build failed with 0 recognized errors. See log tail and full log for details.\n{tail_text}"
            total_errors = 1  # Signal failure in summary
        else:
            output_text = "Build succeeded with 0 errors."

    # Build JSON output
    result = {
        "full_log_path": temp_log_path,
        "summary": {
            "build_failed": build_failed,
            "total_errors": total_errors,
            "total_warnings": total_warnings,
            "showing_errors": displayed_errors,
            "showing_warnings": displayed_warnings
        },
        "errors_and_warnings": output_text
    }

    return json.dumps(result, indent=2)


# NOTE: We intentionally do NOT use Xcode's structured build errors/warnings
# available via AppleScript (e.g. `build errors of last scheme action result`).
# While these provide structured data (message, file path, line/column numbers),
# they are unreliable for two reasons:
# 1. Xcode accumulates issues across incremental builds until a clean or until
#    the issue is resolved — so counts include stale issues from earlier builds.
# 2. Structured errors may not disappear even after the underlying issue is fixed,
#    leading to persistent phantom errors that confuse the output.
# Instead, we regex-match the build log text (which reflects the current build only)
# and use the build status from `status of scheme action result` to detect failures
# that don't produce regex-matchable error lines (e.g. signing, provisioning,
# missing package products).


def _matching_derived_data_dirs(project_path: str) -> list:
    """
    Return the DerivedData directories that belong to this project, as a list of
    (sort_key, path). Directory names are `{ProjectName}-{hash}`, so a different
    project sharing the same name also matches the name prefix; the candidates
    are then disambiguated by info.plist's WorkspacePath
    (select_derived_data_dirs_for_project) so an unrelated same-named project
    can't leak in.

    This is the expensive part of locating .xcresult bundles — it lists the
    whole DerivedData base and parses each candidate's info.plist — so callers
    that poll should resolve it ONCE rather than per iteration.

    Args:
        project_path: Path to .xcodeproj or .xcworkspace
    """
    normalized_path = os.path.realpath(project_path)
    project_name = (
        os.path.basename(normalized_path)
        .replace('.xcworkspace', '')
        .replace('.xcodeproj', '')
    )
    derived_data_base = os.path.expanduser("~/Library/Developer/Xcode/DerivedData")

    if not os.path.exists(derived_data_base):
        return []

    try:
        entries = os.listdir(derived_data_base)
    except OSError as e:
        print(
            f"Error listing DerivedData base {derived_data_base}: {e}",
            file=sys.stderr,
        )
        return []

    dir_candidates = [
        (0.0, os.path.join(derived_data_base, d))
        for d in entries
        if d.startswith(project_name + "-")
        and os.path.isdir(os.path.join(derived_data_base, d))
    ]
    if not dir_candidates:
        return []
    return select_derived_data_dirs_for_project(dir_candidates, normalized_path)


def _scan_logs_for_xcresults(matching_dirs: list, logs_subdir: str) -> list:
    """
    Return [(mtime, path), ...] for every .xcresult bundle under the given
    DerivedData directories' `Logs/<logs_subdir>` folder, sorted newest-first.

    Cheap relative to _matching_derived_data_dirs (no info.plist parsing), so it
    is safe to call once per poll iteration. The `Logs/<subdir>` folder is
    resolved fresh each call because Xcode may create it after the matching
    DerivedData directory already exists.

    Args:
        matching_dirs: Output of _matching_derived_data_dirs (key, path) pairs.
        logs_subdir: Either "Launch" (runtime logs) or "Test" (test results).
    """
    candidates = []
    for _key, derived_data_path in matching_dirs:
        logs_dir = os.path.join(derived_data_path, "Logs", logs_subdir)
        if not os.path.isdir(logs_dir):
            continue
        try:
            for f in os.listdir(logs_dir):
                if not f.endswith('.xcresult'):
                    continue
                full_path = os.path.join(logs_dir, f)
                try:
                    candidates.append((os.path.getmtime(full_path), full_path))
                except OSError:
                    continue
        except OSError as e:
            print(f"Error listing {logs_dir}: {e}", file=sys.stderr)
            continue

    candidates.sort(reverse=True)
    return candidates


def _gather_xcresult_candidates(project_path: str, logs_subdir: str) -> list:
    """
    Return [(mtime, path), ...] for every .xcresult bundle that belongs to this
    project, sorted newest-first. Shared by _find_most_recent_xcresult and
    snapshot_xcresult_mtimes. Single-shot convenience over
    _matching_derived_data_dirs + _scan_logs_for_xcresults; pollers should call
    those two directly so the expensive directory matching happens once.

    Args:
        project_path: Path to .xcodeproj or .xcworkspace
        logs_subdir: Either "Launch" (runtime logs) or "Test" (test results)
    """
    return _scan_logs_for_xcresults(_matching_derived_data_dirs(project_path), logs_subdir)


def snapshot_xcresult_mtimes(project_path: str, logs_subdir: str = "Launch") -> dict:
    """
    Capture {bundle_path: mtime} for every .xcresult bundle that already exists
    for this project BEFORE an action is started.

    Passing this to wait_for_xcresult_after_timestamp as `prior_mtimes` lets it
    wait for a bundle that is genuinely new — either a path that wasn't present
    beforehand, or a pre-existing path whose mtime has since advanced (Xcode
    rewriting a bundle in place). Recording the mtime (not just the path) is
    what makes the in-place-rewrite case work: keying on path alone would
    exclude such a bundle forever. With coarse (1-second) filesystem timestamps
    the pure-timestamp gate can otherwise re-accept a stale prior-run bundle.
    Note: this does not fully disambiguate two runs of the SAME project started
    near-simultaneously — that would require bundle-level action metadata — but
    it removes the common stale-result failure.

    Args:
        project_path: Path to .xcodeproj or .xcworkspace
        logs_subdir: "Launch" (runtime logs) or "Test" (test results)
    """
    return {path: mtime for mtime, path in _gather_xcresult_candidates(project_path, logs_subdir)}


def _find_most_recent_xcresult(project_path: str, logs_subdir: str) -> Optional[str]:
    """
    Find the globally most-recent .xcresult file across the DerivedData
    directories that belong to this project.

    Xcode regenerates the random-hash suffix when a project moves or its Xcode
    version changes, so multiple matching DerivedData directories can coexist.
    Directory names are `{ProjectName}-{hash}`, so a different project sharing
    the same name also matches the prefix; candidates are disambiguated by
    info.plist's WorkspacePath before any .xcresult is considered, so logs from
    an unrelated same-named project can't leak in. Iterating and returning on
    the first match (the prior behavior) is non-deterministic — `os.listdir`
    order is filesystem-defined.

    Args:
        project_path: Path to .xcodeproj or .xcworkspace
        logs_subdir: Either "Launch" (runtime logs) or "Test" (test results)

    Returns:
        Path to the most-recent matching .xcresult, or None.
    """
    candidates = _gather_xcresult_candidates(project_path, logs_subdir)
    if not candidates:
        return None
    return candidates[0][1]


def find_xcresult_for_project(project_path: str) -> Optional[str]:
    """
    Find the most recent runtime-launch xcresult file for a given project.

    Args:
        project_path: Path to the .xcodeproj or .xcworkspace

    Returns:
        Path to the most recent xcresult file, or None if not found
    """
    return _find_most_recent_xcresult(project_path, logs_subdir="Launch")


def wait_for_xcresult_after_timestamp(project_path: str, start_timestamp: float, timeout_seconds: int, logs_subdir: str = "Launch", prior_mtimes: Optional[dict] = None) -> Optional[str]:
    """
    Wait for a NEW xcresult bundle produced by the action that just started.

    Selection is the newest candidate that is genuinely new — meaning either:
      (a) its path was not present before the action began, AND it was both
          created and modified at or after our start time; or
      (b) its path WAS present before but its mtime has advanced past the
          snapshot value (Xcode rewrote the bundle in place). In this case the
          ctime can still be the original creation time, so it is not gated on
          ctime — the advanced mtime is itself proof this action touched it.

    Recording the pre-action mtime per path (`prior_mtimes`) — not just the set
    of paths — is what makes case (b) work: keying on path alone would exclude a
    rewritten bundle forever. The mtime/snapshot comparison is the primary
    guard; the coarse (1-second) timestamp gate is a secondary check for
    brand-new bundles.

    Args:
        project_path: Path to the .xcodeproj or .xcworkspace
        start_timestamp: Unix timestamp (from time.time()) when the operation started
        timeout_seconds: Maximum seconds to wait for a valid xcresult file
        logs_subdir: DerivedData logs subdirectory to search — "Launch" for
            runtime-run results (default) or "Test" for test results.
        prior_mtimes: {path: mtime} of bundles that existed before the action
            started (from snapshot_xcresult_mtimes). Pass this so a stale
            prior-run bundle is never returned and an in-place rewrite still is.
            When omitted, falls back to timestamp-only gating.

    Returns:
        Path to the xcresult file if found, or None if timeout expires or no valid file found
    """
    prior = prior_mtimes or {}
    debug = bool(os.environ.get('XCODE_MCP_DEBUG'))

    # Some filesystems store mtime/ctime with only 1-second resolution, so a file
    # created in the same wall-clock second as start_timestamp can read as older.
    # Allow a small slack so we don't time out waiting for a result that's
    # already there.
    timestamp_slack_seconds = 1.0
    effective_start = start_timestamp - timestamp_slack_seconds

    start_datetime = datetime.datetime.fromtimestamp(start_timestamp)
    print(f"Waiting for new xcresult modified at or after: {start_datetime.strftime('%Y-%m-%d %H:%M:%S.%f')} (excluding {len(prior)} pre-existing)", file=sys.stderr)

    # Resolve the matching DerivedData directories ONCE; only the cheap log-dir
    # scan repeats each iteration (the Logs/<subdir> folder is re-listed inside
    # the scan because Xcode may create it after this point).
    matching_dirs = _matching_derived_data_dirs(project_path)
    end_time = time.time() + timeout_seconds

    while time.time() < end_time:
        # Candidates are newest-first; take the newest that is genuinely new.
        for mtime, xcresult_path in _scan_logs_for_xcresults(matching_dirs, logs_subdir):
            prior_mtime = prior.get(xcresult_path)

            if prior_mtime is not None:
                # Pre-existing path. Only fresh if it was rewritten in place
                # (mtime advanced past the snapshot) AND that rewrite is at/after
                # our start — not gated on ctime, which may be the original.
                if mtime > prior_mtime and mtime >= effective_start:
                    print(f"Accepting rewritten xcresult: {xcresult_path}", file=sys.stderr)
                    return xcresult_path
                if debug:
                    print(f"Skipping unchanged pre-existing xcresult: {xcresult_path}", file=sys.stderr)
                continue

            # Brand-new path (absent from the snapshot). Apply the secondary
            # timestamp gate as a sanity check on both create and modify times.
            try:
                create_time = os.path.getctime(xcresult_path)
            except OSError:
                continue
            if mtime >= effective_start and create_time >= effective_start:
                print(f"Accepting new xcresult: {xcresult_path}", file=sys.stderr)
                return xcresult_path
            if debug:
                print(f"Skipping xcresult older than start time: {xcresult_path}", file=sys.stderr)

        time.sleep(1)

    return None


def format_test_identifier(bundle: str, class_name: str = None, method: str = None) -> str:
    """
    Format test identifier in standard format.
    Returns: "Bundle/Class/method" or "Bundle/Class" or "Bundle"
    """
    if method and class_name:
        return f"{bundle}/{class_name}/{method}"
    elif class_name:
        return f"{bundle}/{class_name}"
    else:
        return bundle


def find_xcresult_bundle(project_path: str, wait_seconds: int = 10) -> Optional[str]:
    """
    Find the most recent test-run .xcresult bundle for the project.

    Args:
        project_path: Path to the Xcode project
        wait_seconds: Maximum seconds to wait for xcresult to appear (not currently used,
                      but kept for API compatibility)

    Returns:
        Path to the most recent xcresult bundle or None if not found
    """
    return _find_most_recent_xcresult(project_path, logs_subdir="Test")


def extract_test_results_from_xcresult(xcresult_path: str) -> Tuple[bool, str]:
    """
    Extract and parse test results from xcresult bundle.

    Args:
        xcresult_path: Path to the .xcresult bundle

    Returns:
        Tuple of (success, json_output_or_error_message).
        JSON shape:
        {
            "xcresult_path": "...",
            "summary": {
                "total_tests": N,
                "passed": M,
                "failed": K,
                "skipped": L,
                "other": {"count": X, "statuses": ["Expected Failure", ...]}
            },
            "failed_tests": [
                {
                    "test_name": "Bundle/Class/method",
                    "duration": "0.5s",
                    "failure_message": "msg1\\nmsg2",
                    "failures": [
                        {"message": "msg1", "file": "...", "line": 42},
                        {"message": "msg2", "file": "...", "line": 99}
                    ]
                }
            ]
        }
        The summary.other key only appears when at least one test has a
        non-standard result status (Xcode 16 Swift Testing emits these).
        The failures array preserves per-failure file/line for tests with
        multiple asserts; failure_message is a newline-joined convenience
        copy of the messages.
    """
    try:
        # Extract test results from xcresult bundle
        result = subprocess.run(
            ['xcrun', 'xcresulttool', 'get', 'test-results', 'tests', '--path', xcresult_path],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode != 0:
            return False, f"Failed to extract test results: {result.stderr}"

        # Parse the JSON
        test_data = json.loads(result.stdout)

    except subprocess.TimeoutExpired:
        return False, "Timeout extracting test results"
    except json.JSONDecodeError as e:
        return False, f"Failed to parse test results JSON: {e}"
    except Exception as e:
        return False, f"Error extracting test results: {e}"

    # Recursively walk the test tree to find all test cases
    def walk_test_nodes(node, parent_path=""):
        """Recursively walk test nodes and collect test case results."""
        test_cases = []

        node_type = node.get('nodeType', '')
        node_name = node.get('name', '')

        # Build the path for this node
        if node_type in ['Unit test bundle', 'Test Suite']:
            current_path = f"{parent_path}/{node_name}" if parent_path else node_name
        else:
            current_path = parent_path

        # If this is a test case, record it
        if node_type == 'Test Case':
            test_info = {
                'name': f"{current_path}/{node_name}",
                'result': node.get('result', 'Unknown'),
                'duration': node.get('duration', '0s')
            }

            # Extract failure details if test failed
            if test_info['result'] in ['Failed', 'Failure']:
                failures = []

                # Get failure messages from the node
                if 'failureMessages' in node:
                    for failure in node['failureMessages']:
                        failure_info = {
                            'message': failure.get('message', 'Unknown failure')
                        }

                        # Extract file location if available
                        if 'location' in failure:
                            location = failure['location']
                            if 'file' in location:
                                failure_info['file'] = location['file']
                            if 'line' in location:
                                failure_info['line'] = location['line']

                        failures.append(failure_info)

                test_info['failures'] = failures

            test_cases.append(test_info)

        # Recursively process children
        if 'children' in node:
            for child in node['children']:
                test_cases.extend(walk_test_nodes(child, current_path))

        return test_cases

    # Walk the test tree starting from testNodes
    all_tests = []
    for test_node in test_data.get('testNodes', []):
        all_tests.extend(walk_test_nodes(test_node))

    # Categorize tests. Xcode 16+ Swift Testing emits statuses beyond the
    # classic Passed/Failed/Skipped trio (e.g. "Expected Failure"); anything
    # we don't recognize lands in `other` so the buckets sum to total_tests.
    passed_tests = [t for t in all_tests if t['result'] in ['Passed', 'Success']]
    failed_tests = [t for t in all_tests if t['result'] in ['Failed', 'Failure']]
    skipped_tests = [t for t in all_tests if t['result'] in ['Skipped']]
    classified = {id(t) for t in passed_tests + failed_tests + skipped_tests}
    other_tests = [t for t in all_tests if id(t) not in classified]

    # Build summary
    summary = {
        'total_tests': len(all_tests),
        'passed': len(passed_tests),
        'failed': len(failed_tests),
        'skipped': len(skipped_tests),
    }

    if other_tests:
        other_statuses = sorted({t['result'] for t in other_tests})
        summary['other'] = {
            'count': len(other_tests),
            'statuses': other_statuses,
        }

    # Format failed tests for output
    failed_test_details = []
    for test in failed_tests:
        test_detail = {
            'test_name': test['name'],
            'duration': test['duration']
        }

        # Preserve every failure with its own location. Earlier code wrote
        # `test_detail['file']`/`['line']` flat, which silently lost all but
        # the last failure's location when a test had multiple asserts.
        if 'failures' in test and test['failures']:
            failure_details = []
            for failure in test['failures']:
                detail = {'message': failure.get('message', 'Unknown failure')}
                if 'file' in failure:
                    detail['file'] = failure['file']
                if 'line' in failure:
                    detail['line'] = failure['line']
                failure_details.append(detail)

            test_detail['failures'] = failure_details
            # Keep a flat failure_message for clients that read it directly.
            test_detail['failure_message'] = '\n'.join(d['message'] for d in failure_details)
        else:
            test_detail['failure_message'] = 'Test failed (no failure message available)'

        failed_test_details.append(test_detail)

    # Build output JSON
    output = {
        'xcresult_path': xcresult_path,
        'summary': summary
    }

    if failed_test_details:
        output['failed_tests'] = failed_test_details

    return True, json.dumps(output, indent=2)
