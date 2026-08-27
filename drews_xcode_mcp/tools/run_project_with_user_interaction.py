#!/usr/bin/env python3
"""run_project_with_user_interaction tool - Run app with user interaction"""

import os
import re
import sys
import time
import datetime
import subprocess
from typing import Optional

from drews_xcode_mcp.server import mcp, TOOL_BUILD
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_and_normalize_project_path
from drews_xcode_mcp.exceptions import XCodeMCPError, InvalidParameterError
from drews_xcode_mcp.utils.run_guard import exclusive_per_project
from drews_xcode_mcp.utils.applescript import (
    build_open_and_wait_applescript,
    build_action_completed_check_applescript,
    escape_applescript_string,
    run_applescript,
    show_notification,
    show_result_notification,
    show_error_notification,
    show_persistent_alert,
)
from drews_xcode_mcp.utils.scheme_action import (
    ACTION_NOT_FOUND,
    SchemeActionReport,
    annotate_with_action_status,
    build_action_result_report_applescript,
    describe_build_failure,
    parse_action_result_report,
)
from drews_xcode_mcp.utils.xcresult import (
    snapshot_xcresult_mtimes,
    wait_for_xcresult_after_timestamp,
    extract_console_logs_from_xcresult
)

# Wall-clock cap on the interactive poll loop. The user is expected to either
# click the alert button or have the app terminate; this is the upper bound on
# how long we keep polling before we give up. Errors with a clear message if hit.
MAX_INTERACTIVE_RUN_SECONDS = 3600

# Seconds to wait after `run workspaceDoc` returns before showing the alert.
# Long enough for a cold simulator to render the app; short enough that we
# detect an immediate launch crash and skip showing a meaningless alert.
LAUNCH_SETTLE_TIMEOUT = 10


@mcp.tool(annotations=TOOL_BUILD)
@describe_parameters_from_docstring
@apply_config
@exclusive_per_project
def run_project_with_user_interaction(project_path: str,
                                       scheme: Optional[str] = None,
                                       regex_filter: Optional[str] = None,
                                       max_lines: int = 20) -> str:
    """
    Run the app and display an alert dialog for you to interact with it.

    The app will run in Xcode/Simulator. Once confirmed running, an alert dialog
    will appear with an "I'm finished - Terminate App" button.

    - Click the button when you're done testing → app will be force-stopped
    - If the app terminates on its own → no force-stop needed

    In either case, runtime logs are extracted and returned after a 2-second wait.

    Perfect for: Interactive testing, manual QA, debugging UI flows

    Args:
        project_path: Path to an Xcode project/workspace directory
        scheme: Optional scheme to run. If not provided, uses the active scheme.
        regex_filter: Optional regex pattern to find matching lines in the output
        max_lines: Maximum number of matching lines to return (default 20)

    Returns:
        JSON string with structured console output
    """
    # Validate and normalize path
    scheme_desc = scheme if scheme else "active scheme"
    normalized_path = validate_and_normalize_project_path(project_path, f"Running {scheme_desc} in")
    escaped_path = escape_applescript_string(normalized_path)

    # Validate regex_filter up front so a bad pattern fails immediately rather
    # than after a multi-minute build+run (it's otherwise only compiled during
    # log extraction at the very end).
    if regex_filter and regex_filter.strip():
        try:
            re.compile(regex_filter)
        except re.error as e:
            raise InvalidParameterError(f"Invalid regex_filter: {e}")

    # Show running notification
    project_name = os.path.basename(normalized_path)
    scheme_name = scheme if scheme else "active scheme"
    show_notification("Drew's Xcode MCP", subtitle=scheme_name, message=f"Running {project_name}")

    escaped_scheme = escape_applescript_string(scheme) if scheme else None
    script = (
        build_open_and_wait_applescript(escaped_path, escaped_scheme)
        + '    set actionResult to run workspaceDoc\n'
        + '    set actionId to ""\n'
        + '    try\n'
        + '        set actionId to (id of actionResult) as string\n'
        + '    end try\n'
        + '    return "launched:" & actionId\n'
        + 'end tell\n'
    )

    print(f"Launching app...", file=sys.stderr)

    # Snapshot existing runtime xcresults BEFORE launching so we wait for a
    # genuinely new bundle rather than risk re-reading a prior run's logs.
    existing_xcresults = snapshot_xcresult_mtimes(normalized_path, logs_subdir="Launch")

    # Capture start time BEFORE running the script
    start_time = time.time()
    start_datetime = datetime.datetime.fromtimestamp(start_time)
    print(f"Run start time: {start_datetime.strftime('%Y-%m-%d %H:%M:%S.%f')}", file=sys.stderr)

    # The launch AppleScript only kicks off `run workspaceDoc` and returns
    # immediately; the default timeout covers workspace load + dispatch.
    success, output = run_applescript(script)

    if not success:
        show_error_notification("Failed to launch app", project_name)
        raise XCodeMCPError(f"Launch failed: {output}")

    # Capture this run's action id so every poll below checks THIS action rather
    # than the workspace-global `last scheme action result`, which a concurrent
    # build/run/test on the same workspace could repoint mid-run.
    action_id = ""
    if output.strip().startswith("launched:"):
        action_id = output.strip()[len("launched:"):].strip()

    # Last-action check, used as a fallback whenever the action-id check isn't
    # usable. This is the original behavior (subject to the cross-action race),
    # so falling back never does worse than before the id pinning was added.
    fallback_check_script = f'''
    set projectPath to "{escaped_path}"

    tell application "Xcode"
        set workspaceDoc to first workspace document whose path is projectPath
        set lastAction to last scheme action result of workspaceDoc
        return completed of lastAction as string
    end tell
    '''

    # The outcome report is pinned to the same action as the completion check, so
    # the status we read always describes the action whose completion we observed.
    if action_id:
        escaped_action_id = escape_applescript_string(action_id)
        check_script = build_action_completed_check_applescript(escaped_path, escaped_action_id)
        # Probe once: the action we just started must be present in the
        # workspace's action results. Fall back to the last-action check unless
        # the probe clearly confirms id matching works — i.e. if the probe
        # itself errors (probe_ok False) OR returns "notfound". Otherwise a
        # transient probe failure would leave every later poll erroring and miss
        # natural termination, hanging the run until the user clicks or the cap.
        probe_ok, probe = run_applescript(check_script)
        if not probe_ok or probe.strip().lower() == "notfound":
            print(f"Warning: run action id not usable (probe_ok={probe_ok}, probe={probe!r}); falling back to last-action check", file=sys.stderr)
            check_script = fallback_check_script
            report_script = build_action_result_report_applescript(escaped_path)
        else:
            report_script = build_action_result_report_applescript(escaped_path, escaped_action_id)
    else:
        print("Warning: could not capture run action id; falling back to last-action check", file=sys.stderr)
        check_script = fallback_check_script
        report_script = build_action_result_report_applescript(escaped_path)

    def read_action_report() -> SchemeActionReport:
        """Read this run action's status, error message and build log."""
        report_ok, report_output = run_applescript(report_script)
        if not report_ok:
            print(f"Warning: could not read run action result: {report_output}", file=sys.stderr)
            return ACTION_NOT_FOUND
        return parse_action_result_report(report_output)

    print(f"App launched, waiting for it to settle (up to {LAUNCH_SETTLE_TIMEOUT}s)...", file=sys.stderr)

    # Brief settle window: poll the action's `completed` flag. If the app
    # crashes or exits during this window, we skip the alert entirely. If it's
    # still running when the window expires, we proceed to show the alert.
    settle_elapsed = 0.0
    app_terminated = False
    action_report = ACTION_NOT_FOUND
    while settle_elapsed < LAUNCH_SETTLE_TIMEOUT:
        success, completed_str = run_applescript(check_script)
        if success and completed_str.strip().lower() == "true":
            print(f"Run action completed during launch settle window", file=sys.stderr)
            app_terminated = True
            action_report = read_action_report()
            break
        time.sleep(0.5)
        settle_elapsed += 0.5

    # `run` in Xcode is build-and-run, so a failed build ends the action without
    # ever launching the app. Detect that before showing an alert that would
    # invite the user to interact with an app that isn't there.
    build_failure = describe_build_failure(action_report, max_lines=max_lines)
    if build_failure is not None:
        print("Build failed; app was never launched.", file=sys.stderr)
        show_error_notification("Build failed - app did not launch", project_name)
        return build_failure

    user_clicked_finish = False
    alert_process = None

    if not app_terminated:
        # Show the persistent alert with clear button text
        alert_process = show_persistent_alert(
            f"{project_name} is running",
            f"{project_name} is now running in Xcode/Simulator.\n\nInteract with the app as needed, then click the button below when you're done testing.",
            button_text="I'm finished - Terminate App"
        )

        # Interactive mode without the alert has no way to know when the user
        # is done — fail fast rather than polling indefinitely.
        if alert_process is None:
            show_error_notification(
                "Interactive run not available",
                "Notifications are disabled; use run_project_until_terminated instead.",
            )
            raise XCodeMCPError(
                "run_project_with_user_interaction requires notifications to be "
                "enabled (the persistent alert is the user's signal that they're "
                "done testing). Use run_project_until_terminated for unattended "
                "runs."
            )

        print(f"Alert shown. Polling for app termination or user finish click...", file=sys.stderr)

        # Poll for either condition with a wall-clock cap so a wedged AppleScript
        # or a forgotten alert can't hang the MCP server forever.
        poll_elapsed = 0.0
        while poll_elapsed < MAX_INTERACTIVE_RUN_SECONDS:
            if alert_process.poll() is not None:
                print(f"User clicked 'I'm finished - Terminate App'", file=sys.stderr)
                user_clicked_finish = True
                break

            success, completed_str = run_applescript(check_script)
            if success and completed_str.strip().lower() == "true":
                print(f"Run action completed while alert was showing", file=sys.stderr)
                app_terminated = True
                action_report = read_action_report()
                try:
                    alert_process.terminate()
                    # Reap the terminated osascript so it doesn't linger as a
                    # defunct child. wait() returns near-instantly once SIGTERM
                    # lands; the timeout just guards a wedged process.
                    alert_process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                break

            time.sleep(2)
            poll_elapsed += 2
        else:
            # Loop exited via the while condition (timeout). Stop the alert and
            # surface this clearly so the caller knows logs may be from a
            # still-running app.
            try:
                alert_process.terminate()
                alert_process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
            show_error_notification(
                f"Interactive run exceeded {MAX_INTERACTIVE_RUN_SECONDS // 60} min",
                project_name,
            )
            raise XCodeMCPError(
                f"Interactive run exceeded the {MAX_INTERACTIVE_RUN_SECONDS}-second "
                f"cap. The app may still be running; stop it manually in Xcode."
            )

    # If user clicked finish, we need to stop the app
    if user_clicked_finish and not app_terminated:
        print(f"Force-stopping app...", file=sys.stderr)
        stop_script = f'''
        set projectPath to "{escaped_path}"

        tell application "Xcode"
            set workspaceDoc to first workspace document whose path is projectPath
            stop workspaceDoc
        end tell
        '''
        run_applescript(stop_script)

        # Wait and verify it stopped, reusing the same action-pinned check.
        for _ in range(10):  # Wait up to 20 seconds
            success, completed_str = run_applescript(check_script)
            if success and completed_str.strip().lower() == "true":
                print(f"App stopped successfully", file=sys.stderr)
                break
            time.sleep(2)

        action_report = read_action_report()

    # A build that fails only after the alert has gone up reaches this point with
    # the alert already dismissed, so re-check before treating the run as one
    # that produced runtime logs.
    build_failure = describe_build_failure(action_report, max_lines=max_lines)
    if build_failure is not None:
        print("Build failed; app was never launched.", file=sys.stderr)
        show_error_notification("Build failed - app did not launch", project_name)
        return build_failure

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
