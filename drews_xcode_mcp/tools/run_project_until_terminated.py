#!/usr/bin/env python3
"""run_project_until_terminated tool - Run app until it terminates or times out"""

import os
import re
import sys
import time
import datetime
from typing import Optional

from drews_xcode_mcp.server import mcp, TOOL_BUILD
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_and_normalize_project_path
from drews_xcode_mcp.exceptions import XCodeMCPError, InvalidParameterError
from drews_xcode_mcp.utils.run_guard import exclusive_per_project
from drews_xcode_mcp.utils.applescript import (
    resolve_build_timeout,
    format_timeout_duration,
    build_open_and_wait_applescript,
    escape_applescript_string,
    run_applescript,
    show_notification,
    show_result_notification,
    show_error_notification,
    show_warning_notification,
)
from drews_xcode_mcp.utils.scheme_action import (
    annotate_with_action_status,
    build_action_result_report_tail_applescript,
    describe_build_failure,
    parse_action_result_report,
)
from drews_xcode_mcp.utils.xcresult import (
    snapshot_xcresult_mtimes,
    wait_for_xcresult_after_timestamp,
    extract_console_logs_from_xcresult
)

# Marks the first line of the AppleScript payload, which reports whether this
# tool's own timeout fired. The scheme action result cannot express that: a
# forced stop looks the same to Xcode whether we asked for it after a timeout or
# because the user was done.
_OUTCOME_PREFIX = "OUTCOME:"
_OUTCOME_TIMEOUT = "timeout"


@mcp.tool(annotations=TOOL_BUILD)
@describe_parameters_from_docstring
@apply_config
@exclusive_per_project
def run_project_until_terminated(project_path: str,
                                  scheme: Optional[str] = None,
                                  regex_filter: Optional[str] = None,
                                  max_lines: int = 20,
                                  timeout: Optional[int] = None) -> str:
    """
    Run the app and wait for it to terminate naturally (up to `timeout` seconds).

    The app will run in Xcode/Simulator. If it doesn't terminate within `timeout`
    seconds (default 600, i.e. 10 minutes), it will be force-stopped and runtime
    logs will be extracted.

    No user interaction required - fully automated.

    Perfect for: Automated tests, CLI tools, apps with defined exit points

    Args:
        project_path: Path to an Xcode project/workspace directory
        scheme: Optional scheme to run. If not provided, uses the active scheme.
        regex_filter: Optional regex pattern to find matching lines in the output
        max_lines: Maximum number of matching lines to return (default 20)
        timeout: Maximum seconds to wait for the app to terminate before
            force-stopping it. If not provided, defaults to 600. Must be a
            positive integer.

    Returns:
        JSON string with structured console output
    """
    # Validate and normalize path
    scheme_desc = scheme if scheme else "active scheme"
    normalized_path = validate_and_normalize_project_path(project_path, f"Running {scheme_desc} in")
    escaped_path = escape_applescript_string(normalized_path)
    effective_timeout = resolve_build_timeout(timeout)

    # Validate regex_filter up front so a bad pattern fails immediately rather
    # than after the (multi-minute) build+run, where it's otherwise only
    # compiled during log extraction.
    if regex_filter and regex_filter.strip():
        try:
            re.compile(regex_filter)
        except re.error as e:
            raise InvalidParameterError(f"Invalid regex_filter: {e}")

    # Show running notification
    project_name = os.path.basename(normalized_path)
    scheme_name = scheme if scheme else "active scheme"
    show_notification("Drew's Xcode MCP", subtitle=scheme_name, message=f"Running {project_name}")

    # The poll loop runs entirely inside AppleScript against the `actionResult`
    # reference returned by `run workspaceDoc`. This pins the wait to the action
    # this tool started — reading the workspace-global `last scheme action
    # result` (the prior approach) could observe a concurrent build/run/test on
    # the same workspace and report the wrong action's status. It also replaces
    # one osascript spawn every 2s with a single subprocess for the whole run.
    # Loops measure elapsed time with AppleScript's `(current date) - startDate`
    # (real wall-clock seconds) instead of summing fixed `delay` increments.
    # Counting `delay 1.0` ignores the per-iteration Apple Event round-trip, so
    # the old loop drifted longer than `effective_timeout` proportionally to the
    # timeout — which could push the run past the subprocess budget and get
    # osascript killed before its own `stop` ran. Wall-clock keeps the inner
    # bound honest regardless of IPC overhead.
    escaped_scheme = escape_applescript_string(scheme) if scheme else None
    script = (
        build_open_and_wait_applescript(escaped_path, escaped_scheme)
        + '    set actionResult to run workspaceDoc\n'
        + '    set runStartDate to (current date)\n'
        + '    set didTimeout to false\n'
        + '    repeat\n'
        + '        if completed of actionResult is true then exit repeat\n'
        + f'        if ((current date) - runStartDate) >= {effective_timeout} then\n'
        + '            set didTimeout to true\n'
        + '            exit repeat\n'
        + '        end if\n'
        + '        delay 1.0\n'
        + '    end repeat\n'
        + '    if didTimeout then\n'
        + '        stop workspaceDoc\n'
        + '        set stopStartDate to (current date)\n'
        + '        repeat\n'
        + '            if completed of actionResult is true then exit repeat\n'
        + '            if ((current date) - stopStartDate) >= 20 then exit repeat\n'
        + '            delay 1.0\n'
        + '        end repeat\n'
        + f'        set outcomeText to "{_OUTCOME_TIMEOUT}"\n'
        + '    else\n'
        + '        set outcomeText to "terminated"\n'
        + '    end if\n'
        # Report the action's real outcome alongside our own. `completed` above
        # goes true for a failed build just as it does for an app that ran and
        # exited, so `status` is what tells the two apart.
        + build_action_result_report_tail_applescript(
            "actionResult", leading_line_expression=f'"{_OUTCOME_PREFIX}" & outcomeText'
        )
        + 'end tell\n'
    )

    print(f"Launching app and waiting for termination (up to {format_timeout_duration(effective_timeout)})...", file=sys.stderr)

    # Snapshot existing runtime xcresults BEFORE launching so we wait for a
    # genuinely new bundle rather than risk re-reading a prior run's logs.
    existing_xcresults = snapshot_xcresult_mtimes(normalized_path, logs_subdir="Launch")

    # Capture start time BEFORE running the script
    start_time = time.time()
    start_datetime = datetime.datetime.fromtimestamp(start_time)
    print(f"Run start time: {start_datetime.strftime('%Y-%m-%d %H:%M:%S.%f')}", file=sys.stderr)

    # The script polls inside AppleScript for up to effective_timeout (plus up
    # to 20s verifying a forced stop); the subprocess timeout must exceed that,
    # with a buffer for workspace load and IPC overhead. If the subprocess is
    # nonetheless killed (run_applescript raises), osascript dies mid-run and the
    # AppleScript's own `stop` never executes — so issue a best-effort stop with
    # a fresh short-lived osascript before propagating, rather than leaving the
    # app running.
    try:
        success, output = run_applescript(script, timeout=effective_timeout + 60)
    except XCodeMCPError:
        stop_script = (
            f'set projectPath to "{escaped_path}"\n'
            'tell application "Xcode"\n'
            '    set workspaceDoc to first workspace document whose path is projectPath\n'
            '    stop workspaceDoc\n'
            'end tell\n'
        )
        try:
            run_applescript(stop_script)
            print("Issued best-effort stop after run subprocess was killed.", file=sys.stderr)
        except XCodeMCPError:
            print("Best-effort stop after subprocess kill also failed; app may still be running.", file=sys.stderr)
        raise

    if not success:
        show_error_notification("Failed to launch app", project_name)
        raise XCodeMCPError(f"Launch failed: {output}")

    outcome_line, _, report_text = output.partition("\n")
    did_timeout = outcome_line.strip() == f"{_OUTCOME_PREFIX}{_OUTCOME_TIMEOUT}"
    action_report = parse_action_result_report(report_text)

    # `run` in Xcode is build-and-run, so a failed build ends the action without
    # ever launching the app — no runtime logs will exist to collect.
    build_failure = describe_build_failure(action_report, max_lines=max_lines)
    if build_failure is not None:
        print("Build failed; app was never launched.", file=sys.stderr)
        show_error_notification("Build failed - app did not launch", project_name)
        return build_failure

    if did_timeout:
        duration = format_timeout_duration(effective_timeout)
        print(f"App did not terminate within {duration}; force-stopped.", file=sys.stderr)
        show_warning_notification(f"App timeout ({duration})", "Force-stopped app")
    else:
        print(f"App terminated naturally.", file=sys.stderr)

    # Wait for xcresult to finalize
    print(f"Waiting for runtime logs to become available...", file=sys.stderr)
    time.sleep(2)

    # Wait for an xcresult file that was modified at or after our start time
    xcresult_timeout = 10
    xcresult_path = wait_for_xcresult_after_timestamp(normalized_path, start_time, xcresult_timeout, prior_mtimes=existing_xcresults)

    if not xcresult_path:
        show_error_notification("Run completed but logs unavailable", "Could not find xcresult")
        return annotate_with_action_status(
            "Run completed. Could not find xcresult file to extract console logs.",
            action_report,
        )

    print(f"Using xcresult: {xcresult_path}", file=sys.stderr)

    # Extract console logs (returns JSON)
    success, console_output = extract_console_logs_from_xcresult(xcresult_path, regex_filter, max_lines)

    if not success:
        show_error_notification("Failed to extract logs", console_output)
        return annotate_with_action_status(f"Run completed. {console_output}", action_report)

    if not console_output:
        show_result_notification(f"Run completed")
        return annotate_with_action_status(
            "Run completed. No console output found (or filtered out).", action_report
        )

    # Show result notification with error count
    import json
    try:
        output_data = json.loads(console_output)
        summary = output_data.get("summary", {})
        errors = summary.get("errors_and_faults", 0)
        if errors > 0:
            show_error_notification(f"Run completed", f"{errors} errors/faults")
        else:
            show_result_notification(f"Run completed")
    except json.JSONDecodeError:
        show_result_notification(f"Run completed")

    return annotate_with_action_status(console_output, action_report)
