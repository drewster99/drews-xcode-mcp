#!/usr/bin/env python3
"""get_latest_test_results tool - Get test results from last run"""

import os
import json
import sys

from drews_xcode_mcp.server import mcp, TOOL_READONLY
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_and_normalize_project_path
from drews_xcode_mcp.utils.applescript import (
    escape_applescript_string,
    run_applescript,
    show_result_notification,
    show_error_notification,
    show_warning_notification
)
from drews_xcode_mcp.utils.xcresult import find_xcresult_bundle, extract_test_results_from_xcresult


@mcp.tool(annotations=TOOL_READONLY)
@describe_parameters_from_docstring
@apply_config
def get_latest_test_results(project_path: str) -> str:
    """
    Get the test results from the most recent test run.

    Args:
        project_path: Path to Xcode project/workspace directory

    Returns:
        JSON with test results or plain text error message.
        Success format:
        {
            "xcresult_path": "...",
            "summary": {"total_tests": N, "passed": M, "failed": K, "skipped": L},
            "failed_tests": [{"test_name": "...", "failure_message": "...", ...}]
        }
    """
    # Validate and normalize the project path
    project_path = validate_and_normalize_project_path(project_path, "get_latest_test_results")

    # Try to find the most recent xcresult bundle
    xcresult_path = find_xcresult_bundle(project_path)

    if xcresult_path and os.path.exists(xcresult_path):
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

            return test_results

    # Fallback: Try to get from Xcode via AppleScript
    show_warning_notification("Using AppleScript fallback", "xcresult unavailable or parsing failed")
    escaped_path = escape_applescript_string(project_path)

    script = f'''
set projectPath to "{escaped_path}"

tell application "Xcode"
    try
        -- Try to get the workspace document if it's already open
        set workspaceDoc to first workspace document whose path is projectPath

        -- Try to get last scheme action result
        set lastResult to last scheme action result of workspaceDoc

        set resultStatus to status of lastResult as string
        set resultCompleted to completed of lastResult

        -- Check if it was a test action by looking for test failures
        set isTestResult to false
        set failureMessages to ""
        try
            set failures to test failures of lastResult
            set isTestResult to true
            repeat with failure in failures
                set failureMessages to failureMessages & (message of failure) & "\\n"
            end repeat
        end try

        if isTestResult then
            return "Last test status: " & resultStatus & "\\n" & ¬
                   "Completed: " & resultCompleted & "\\n" & ¬
                   "Test failures:\\n" & failureMessages
        else
            return "No test results available (last action was not a test)"
        end if
    on error
        return "No test results available"
    end try
end tell
    '''

    success, output = run_applescript(script)

    if success:
        # Parse output to show notification
        if "No test results available" in output:
            show_result_notification("No test results")
        elif "succeeded" in output.lower():
            show_result_notification("All tests PASSED")
        elif "failed" in output.lower():
            show_error_notification("Tests FAILED")
        return output
    else:
        show_error_notification("Failed to get test results", "AppleScript fallback also failed")
        return "No test results available"
