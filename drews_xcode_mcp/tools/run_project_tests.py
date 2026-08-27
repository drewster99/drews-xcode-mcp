#!/usr/bin/env python3
"""run_project_tests tool - Run Xcode project tests"""

import os
import sys
import time
import json
from typing import Optional, List

from drews_xcode_mcp.server import mcp, TOOL_BUILD
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_and_normalize_project_path
from drews_xcode_mcp.exceptions import InvalidParameterError, XCodeMCPError
from drews_xcode_mcp.utils.run_guard import exclusive_per_project
from drews_xcode_mcp.utils.applescript import (
    build_open_and_wait_applescript,
    build_wait_for_completion_applescript,
    is_action_timeout,
    resolve_build_timeout,
    format_timeout_duration,
    escape_applescript_string,
    run_applescript,
    show_notification,
    show_result_notification,
    show_error_notification,
    show_warning_notification,
)
from drews_xcode_mcp.utils.xcresult import snapshot_xcresult_mtimes, wait_for_xcresult_after_timestamp, extract_test_results_from_xcresult


# TODO (follow-up): Implement selective test execution with xcodebuild.
#
# AppleScript's 'test' command doesn't support -only-testing: flags, so we need
# to use xcodebuild directly for running specific tests. xcodebuild also
# requires a -destination flag — the original blocker — which is now solvable
# via get_active_run_destination / set_run_destination / list_run_destinations.
# The commented implementation below is preserved as the starting point for
# that follow-up work; once it's properly implemented and tested, this block
# should be removed.
#
# def _get_active_scheme(project_path: str) -> str:
#     """Get the active scheme for a project using AppleScript"""
#     escaped_path = escape_applescript_string(project_path)
#     script = f'''
# tell application "Xcode"
#     open "{escaped_path}"
#     delay 1
#     set workspaceDoc to first workspace document whose path is "{escaped_path}"
#     set activeScheme to active scheme of workspaceDoc
#     return name of activeScheme
# end tell
#     '''
#     success, output = run_applescript(script)
#     if success:
#         return output.strip()
#     raise InvalidParameterError(f"Could not determine active scheme: {output}")
#
#
# def _run_tests_with_xcodebuild(project_path: str, tests_to_run: List[str],
#                                 scheme: Optional[str], max_wait_seconds: int) -> str:
#     """Run specific tests using xcodebuild (AppleScript doesn't support -only-testing:)"""
#
#     # Determine if this is a workspace or project
#     is_workspace = project_path.endswith('.xcworkspace')
#     project_flag = '-workspace' if is_workspace else '-project'
#
#     # Get scheme if not provided
#     if not scheme:
#         scheme = _get_active_scheme(project_path)
#
#     # Build xcodebuild command
#     cmd = [
#         'xcodebuild',
#         'test',
#         project_flag,
#         project_path,
#         '-scheme',
#         scheme
#     ]
#
#     # Add -only-testing: arguments
#     for test_id in tests_to_run:
#         cmd.extend(['-only-testing', test_id])
#
#     print(f"DEBUG: Running xcodebuild command: {' '.join(cmd)}", file=sys.stderr)
#
#     # Run xcodebuild
#     if max_wait_seconds == 0:
#         # Start in background and return immediately
#         subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
#         return "✅ Tests have been started. Use get_latest_test_results to check results later."
#
#     # Run and wait for completion
#     try:
#         result = subprocess.run(
#             cmd,
#             capture_output=True,
#             text=True,
#             timeout=max_wait_seconds
#         )
#
#         # Wait a moment for xcresult to be written
#         time.sleep(2)
#
#         # Get results from xcresult bundle
#         xcresult_path = find_xcresult_bundle(project_path)
#
#         if xcresult_path:
#             print(f"DEBUG: Found xcresult bundle at {xcresult_path}", file=sys.stderr)
#
#             try:
#                 xcresult = subprocess.run(
#                     ['xcrun', 'xcresulttool', 'get', 'test-results', 'tests', '--path', xcresult_path],
#                     capture_output=True,
#                     text=True,
#                     timeout=10
#                 )
#
#                 if xcresult.returncode == 0:
#                     # Parse to show notification
#                     try:
#                         test_data = json.loads(xcresult.stdout)
#                         failure_count = 0
#                         if 'tests' in test_data and '_values' in test_data['tests']:
#                             for test in test_data['tests']['_values']:
#                                 if test.get('testStatus', '') == 'Failure':
#                                     failure_count += 1
#
#                         if failure_count == 0:
#                             show_result_notification("All tests PASSED")
#                         else:
#                             show_error_notification(f"{failure_count} test{'s' if failure_count != 1 else ''} FAILED")
#                     except:
#                         pass
#
#                     return xcresult.stdout
#             except Exception as e:
#                 print(f"DEBUG: Exception getting xcresult data: {e}", file=sys.stderr)
#
#         # Fallback to xcodebuild output
#         if result.returncode == 0:
#             show_result_notification("Tests PASSED")
#             return "✅ Tests passed"
#         else:
#             show_error_notification("Tests FAILED")
#             # Return last 50 lines of output
#             lines = result.stdout.split('\n')
#             return "❌ Tests failed\n\n" + '\n'.join(lines[-50:])
#
#     except subprocess.TimeoutExpired:
#         show_result_notification(f"Tests timeout ({max_wait_seconds}s)")
#         return f"⏳ Tests did not complete within {max_wait_seconds} seconds"


@mcp.tool(annotations=TOOL_BUILD)
@describe_parameters_from_docstring
@apply_config
@exclusive_per_project
def run_project_tests(project_path: str,
                     scheme: Optional[str] = None,
                     timeout: Optional[int] = None) -> str:
    """
    Run tests for the specified Xcode project or workspace.

    Tests run for up to `timeout` seconds (default 600, i.e. 10 minutes) before
    timing out, which guards against a test run hanging indefinitely. Raise it
    for large projects whose build-for-testing alone exceeds the default.

    Args:
        project_path: Path to Xcode project/workspace directory
        scheme: Optional scheme to test (uses active scheme if not specified)
        timeout: Maximum seconds to wait for the build-for-testing plus test run
            to complete. If not provided, defaults to 600. Must be a positive integer.

    Returns:
        JSON with test results if tests complete, otherwise plain text status message.
        Success format:
        {
            "xcresult_path": "...",
            "summary": {"total_tests": N, "passed": M, "failed": K, "skipped": L},
            "failed_tests": [{"test_name": "...", "failure_message": "...", ...}]
        }
        Timeout: Plain text message indicating timeout
    """
    # Validate and normalize the project path
    project_path = validate_and_normalize_project_path(project_path, "run_project_tests")

    # Resolve the test timeout up front so an invalid value errors immediately,
    # before any AppleScript runs or notifications post.
    effective_timeout = resolve_build_timeout(timeout)

    # Show notification
    show_notification("Drew's Xcode MCP", subtitle=os.path.basename(project_path), message="Running tests")

    # TODO: Selective test execution - commented out until we can get active run destination
    # # Handle various forms of empty/invalid tests_to_run parameter
    # # This works around MCP client issues with optional list parameters
    # if tests_to_run is not None:
    #     # Handle string inputs that might come from the client
    #     if isinstance(tests_to_run, str):
    #         tests_to_run = tests_to_run.strip()
    #         if not tests_to_run or tests_to_run in ['[]', 'null', 'undefined', '']:
    #             tests_to_run = None
    #         else:
    #             # Try to parse as a comma-separated list
    #             tests_to_run = [t.strip() for t in tests_to_run.split(',') if t.strip()]
    #     elif not tests_to_run:  # Empty list or other falsy value
    #         tests_to_run = None
    #
    # # If specific tests are requested, use xcodebuild (AppleScript doesn't support -only-testing:)
    # if tests_to_run:
    #     return _run_tests_with_xcodebuild(project_path, tests_to_run, scheme, max_wait_seconds)

    # Build the AppleScript from shared helpers + the test-specific failure
    # extraction tail. The open/wait/scheme-set boilerplate lives in
    # build_open_and_wait_applescript so both build and test paths share it.
    escaped_path = escape_applescript_string(project_path)
    escaped_scheme = escape_applescript_string(scheme) if scheme else None

    # Test-specific: after the wait loop completes, walk `test failures of
    # testResult` and emit a structured text blob the Python side parses.
    failure_extraction_tail = (
        '    -- Get results\n'
        '    set testStatus to status of testResult as string\n'
        '    set testCompleted to completed of testResult\n'
        '\n'
        '    -- Get failures if any with full details\n'
        '    set failureMessages to ""\n'
        '    set failureCount to 0\n'
        '    try\n'
        '        set failures to test failures of testResult\n'
        '        set failureCount to count of failures\n'
        '        if failureCount > 0 then\n'
        '            repeat with failure in failures\n'
        '                set failureMsg to ""\n'
        '                set failurePath to ""\n'
        '                set failureLine to ""\n'
        '\n'
        '                try\n'
        '                    set failureMsg to message of failure\n'
        '                on error\n'
        '                    set failureMsg to "Unknown test failure"\n'
        '                end try\n'
        '\n'
        '                try\n'
        '                    set failurePath to file path of failure\n'
        '                end try\n'
        '\n'
        '                try\n'
        '                    set failureLine to starting line number of failure as string\n'
        '                end try\n'
        '\n'
        '                set failureMessages to failureMessages & "FAILURE: " & failureMsg & "\\n"\n'
        '                if failurePath is not "" and failurePath is not missing value then\n'
        '                    set failureMessages to failureMessages & "FILE: " & failurePath & "\\n"\n'
        '                end if\n'
        '                if failureLine is not "" and failureLine is not "missing value" then\n'
        '                    set failureMessages to failureMessages & "LINE: " & failureLine & "\\n"\n'
        '                end if\n'
        '                set failureMessages to failureMessages & "---\\n"\n'
        '            end repeat\n'
        '        else\n'
        '            -- No test failures in collection, but status might still be failed.\n'
        '            -- We will parse the build log later to extract actual failure details.\n'
        '            if testStatus is "failed" or testStatus contains "fail" then\n'
        '                set failureMessages to "PARSE_FROM_LOG" & "\\n"\n'
        '            end if\n'
        '        end if\n'
        '    on error errMsg\n'
        '        if testStatus is "failed" or testStatus contains "fail" then\n'
        '            set failureMessages to "PARSE_FROM_LOG" & "\\n"\n'
        '        end if\n'
        '    end try\n'
        '\n'
        '    -- Get build log for statistics\n'
        '    set buildLog to ""\n'
        '    try\n'
        '        set buildLog to build log of testResult\n'
        '    end try\n'
        '\n'
        '    return "Status: " & testStatus & "\\n" & ¬\n'
        '           "Completed: " & testCompleted & "\\n" & ¬\n'
        '           "FailureCount: " & (failureCount as string) & "\\n" & ¬\n'
        '           "Failures:\\n" & failureMessages & "\\n" & ¬\n'
        '           "---LOG---\\n" & buildLog\n'
    )

    script = (
        build_open_and_wait_applescript(escaped_path, escaped_scheme)
        + '    set testResult to test workspaceDoc\n'
        + build_wait_for_completion_applescript("testResult", effective_timeout, action_name="Tests")
        + failure_extraction_tail
        + 'end tell\n'
    )

    # Snapshot existing test xcresults and capture start time before launching so
    # we only accept a .xcresult written by THIS test run, not a stale bundle
    # from a previous run.
    existing_xcresults = snapshot_xcresult_mtimes(project_path, logs_subdir="Test")
    test_start_time = time.time()

    # The script polls inside AppleScript for up to effective_timeout; the
    # subprocess timeout must exceed that, plus buffer for workspace load. If the
    # subprocess is nonetheless killed (osascript unresponsive), run_applescript
    # raises XCodeMCPError — surface that as a test timeout rather than letting
    # it propagate as an opaque error.
    try:
        success, output = run_applescript(script, timeout=effective_timeout + 60)
    except XCodeMCPError:
        duration = format_timeout_duration(effective_timeout)
        show_warning_notification(f"Tests timeout ({duration})")
        return f"⏳ Tests did not complete within {duration} (Xcode did not respond)"

    if not success:
        # The AppleScript poll loop raises (rather than returning) when it times
        # out, so a timeout surfaces here as a failed subprocess. Detect it by
        # the helper's error number (not message text) and translate it into a
        # clear test-specific message.
        if is_action_timeout(output):
            duration = format_timeout_duration(effective_timeout)
            show_warning_notification(f"Tests timeout ({duration})")
            return f"⏳ Tests did not complete within {duration}"
        show_error_notification("Failed to run tests", os.path.basename(project_path))
        return f"Failed to run tests: {output}"

    # Debug: Log raw output to see what we're getting
    if os.environ.get('XCODE_MCP_DEBUG'):
        print(f"DEBUG: Raw test output:\n{output}\n", file=sys.stderr)

    # Parse the AppleScript output to get test status. A timeout never reaches
    # here (the poll loop raises and is handled in the `if not success` branch
    # above), so the action always completed; `status` drives the messaging.
    status = ""
    for line in output.split('\n'):
        if line.startswith("Status: "):
            status = line.replace("Status: ", "").strip()

    # If tests completed, get detailed results from xcresult. Gate on the start
    # timestamp so a not-yet-finalized bundle doesn't cause us to return the
    # previous test run's results.
    xcresult_path = wait_for_xcresult_after_timestamp(
        project_path, test_start_time, timeout_seconds=10, logs_subdir="Test",
        prior_mtimes=existing_xcresults
    )

    if xcresult_path:
        print(f"DEBUG: Found xcresult bundle at {xcresult_path}", file=sys.stderr)

        # Extract and parse test results
        success, test_results = extract_test_results_from_xcresult(xcresult_path)

        if success:
            # Parse JSON to show notification
            try:
                result_data = json.loads(test_results)
                summary = result_data.get('summary', {})
                failed = summary.get('failed', 0)

                if failed == 0:
                    show_result_notification("All tests PASSED")
                else:
                    show_error_notification(f"{failed} test{'s' if failed != 1 else ''} FAILED")
            except (json.JSONDecodeError, AttributeError) as e:
                print(f"warn: could not parse test summary for notification: {e}", file=sys.stderr)

            # Return the parsed JSON
            return test_results
        else:
            print(f"DEBUG: Failed to parse xcresult data: {test_results}", file=sys.stderr)

    # Fallback if we couldn't get xcresult data
    print(f"DEBUG: No xcresult bundle found for {project_path}", file=sys.stderr)

    if status == "succeeded":
        show_result_notification("All tests PASSED")
        return "✅ All tests passed"
    elif status == "failed":
        show_error_notification("Tests FAILED")
        return "❌ Tests failed\n\nNo detailed test results available - xcresult bundle not found"
    else:
        show_warning_notification(f"Tests: {status}")
        return f"Test run status: {status}"
