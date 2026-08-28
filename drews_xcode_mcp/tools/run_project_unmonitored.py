#!/usr/bin/env python3
"""run_project_unmonitored tool - Build, then launch the app and return immediately"""

import os
import sys
from typing import Optional

from drews_xcode_mcp.server import mcp, TOOL_BUILD
from drews_xcode_mcp.config_manager import apply_config
from drews_xcode_mcp.docstring_parameters import describe_parameters_from_docstring
from drews_xcode_mcp.security import validate_and_normalize_project_path
from drews_xcode_mcp.exceptions import InvalidParameterError, XCodeMCPError
from drews_xcode_mcp.utils.applescript import (
    build_open_and_wait_applescript,
    build_wait_for_completion_applescript,
    escape_applescript_string,
    format_timeout_duration,
    is_action_timeout,
    resolve_build_timeout,
    run_applescript,
    show_notification,
    show_error_notification,
    validate_max_lines,
)
from drews_xcode_mcp.utils.scheme_action import (
    SchemeActionStatus,
    build_action_result_report_tail_applescript,
    parse_action_result_report,
    report_build_failure,
)

# Emitted by the AppleScript once the build has succeeded and `run` has been
# dispatched. Any other output carries a build-failure report instead.
_LAUNCHED_SENTINEL = "LAUNCHED"


# Unlike the monitored run/test tools this is deliberately NOT wrapped in
# @exclusive_per_project. It is fire-and-forget — it returns as soon as the app
# is launched while the app keeps running — so the guard would release its
# per-project key immediately and provide no real protection (it would only
# block two launches dispatched in the same instant, not a launch that collides
# with an already-running app). It also does no .xcresult snapshotting, so it
# has none of the result-isolation the guard exists to protect. Callers are
# responsible for not launching the same project repeatedly without stopping it.
@mcp.tool(annotations=TOOL_BUILD)
@describe_parameters_from_docstring
@apply_config
def run_project_unmonitored(project_path: str,
                             scheme: Optional[str] = None,
                             include_warnings: Optional[bool] = None,
                             max_lines: int = 25,
                             timeout: Optional[int] = None) -> str:
    """
    Build the project, then launch the app in Xcode and return without waiting
    for it to finish.

    The app will continue running until you stop it manually in Xcode.
    No runtime monitoring, no automatic termination, no log extraction.

    Use get_runtime_output later (after manual termination) to retrieve logs.

    Perfect for: Long-running apps, servers, apps needing extended manual testing

    If the build fails the app is not launched, and structured build errors are
    returned in the same JSON shape build_project produces.

    Args:
        project_path: Path to an Xcode project/workspace directory
        scheme: Optional scheme to run. If not provided, uses the active scheme.
        include_warnings: Include warnings in build output when the build fails.
            If not provided, uses the global setting.
        max_lines: Maximum number of error/warning lines to show (default 25)
        timeout: Maximum seconds to wait for the build before giving up. If not
            provided, defaults to 600. Must be a positive integer. This bounds
            the build only — the app itself is never waited on.

    Returns:
        A success message once the app has been launched, or JSON describing the
        build failure that prevented launch.
    """
    if include_warnings is not None and not isinstance(include_warnings, bool):
        raise InvalidParameterError("include_warnings must be a boolean value")

    max_lines = validate_max_lines(max_lines)
    effective_timeout = resolve_build_timeout(timeout)

    # Validate and normalize path
    scheme_desc = scheme if scheme else "active scheme"
    normalized_path = validate_and_normalize_project_path(project_path, f"Launching {scheme_desc} in")
    escaped_path = escape_applescript_string(normalized_path)

    # Show launching notification
    project_name = os.path.basename(normalized_path)
    scheme_name = scheme if scheme else "active scheme"
    show_notification("Drew's Xcode MCP", subtitle=scheme_name, message=f"Launching {project_name}")

    # Build explicitly before running, rather than relying on `run` (which is
    # build-and-run) to report the outcome. This tool returns while the app is
    # still alive, so it never observes the run action reaching a terminal state
    # — it cannot tell afterwards whether the app launched or the build failed.
    # A separate build action makes the build outcome observable before we
    # commit to saying "launched". `run`'s own build is then an incremental
    # no-op.
    #
    # Caveat: a scheme configured to build its Run action with a different
    # configuration than its Build action could still fail during `run` after
    # this build succeeds. That is rare, and this check still catches every
    # ordinary compile/link/signing failure that today is reported as success.
    escaped_scheme = escape_applescript_string(scheme) if scheme else None
    script = (
        build_open_and_wait_applescript(escaped_path, escaped_scheme)
        + '    set buildResult to build workspaceDoc\n'
        + build_wait_for_completion_applescript("buildResult", effective_timeout)
        + f'    set buildStatus to "{SchemeActionStatus.UNKNOWN.value}"\n'
        + '    try\n'
        + '        set buildStatus to (status of buildResult) as string\n'
        + '    end try\n'
        + f'    if buildStatus is "{SchemeActionStatus.SUCCEEDED.value}" then\n'
        + '        run workspaceDoc\n'
        + f'        return "{_LAUNCHED_SENTINEL}"\n'
        + '    end if\n'
        + build_action_result_report_tail_applescript("buildResult")
        + 'end tell\n'
    )

    # The script polls inside AppleScript for up to effective_timeout; the
    # subprocess timeout must exceed that, with a buffer for workspace load and
    # IPC overhead.
    success, output = run_applescript(script, timeout=effective_timeout + 60)

    if not success:
        # The AppleScript poll loop raises rather than returning when the build
        # exceeds its budget, so a build timeout arrives here as a failed
        # subprocess. Name it for what it is instead of "failed to launch".
        if is_action_timeout(output):
            duration = format_timeout_duration(effective_timeout)
            show_error_notification(f"Build timed out ({duration})", project_name)
            raise XCodeMCPError(
                f"Build did not complete within {duration}, so '{project_name}' was "
                f"not launched. Raise `timeout` if this project needs longer."
            )
        show_error_notification("Failed to launch app", project_name)
        raise XCodeMCPError(f"Launch failed: {output}")

    if output.strip() != _LAUNCHED_SENTINEL:
        report = parse_action_result_report(output)
        print(f"Build did not succeed (status: {report.status.value}); app not launched.", file=sys.stderr)
        show_error_notification("Build failed - app not launched", project_name)
        return report_build_failure(report, include_warnings, max_lines)

    # Show success notification with sound to get attention
    show_notification(
        "Drew's Xcode MCP",
        subtitle=project_name,
        message="🚀 App launched (running until manually stopped)",
        sound=True
    )

    return (
        f"App '{project_name}' built successfully and its launch was dispatched in "
        f"Xcode.\n\n"
        f"The build was verified before dispatch; the app is now running and will "
        f"continue until you stop it manually in Xcode. (This tool returns without "
        f"waiting on the run action, so a launch failure after a verified build is "
        f"not observed here.)\n\n"
        f"Use get_runtime_output after termination to retrieve console logs."
    )
