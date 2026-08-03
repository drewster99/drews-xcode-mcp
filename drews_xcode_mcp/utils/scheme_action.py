#!/usr/bin/env python3
"""Reading and interpreting Xcode scheme action results.

Xcode's `build`, `run` and `test` commands all return a *scheme action result*.
Two of its properties matter for deciding whether an action actually did what
was asked:

- `completed` — a boolean that becomes true for EVERY terminal outcome, success
  or failure alike. On its own it says only "Xcode is done with this action".
- `status` — the actual outcome, drawn from Xcode's `scheme action result
  status` enumeration (see SchemeActionStatus below).

Because `run` in Xcode means build-and-run, a run whose build fails still
completes. Polling `completed` alone therefore cannot distinguish "the app ran
and exited" from "the build failed and the app never launched" — reading
`status` is what separates them.
"""

import json
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from drews_xcode_mcp.utils.xcresult import extract_build_errors_and_warnings

# Markers delimiting the report emitted by build_action_result_report_applescript
# and consumed by parse_action_result_report. They live here, next to the parser,
# so both halves of the wire format stay in one place.
_REPORT_STATUS_PREFIX = "STATUS:"
_REPORT_ERROR_MARKER = "---ERROR-MESSAGE---"
_REPORT_BUILD_LOG_MARKER = "---BUILD-LOG---"
_REPORT_NOT_FOUND = "NOTFOUND"

# What AppleScript yields when coercing an unset `error message` to text. Xcode
# populates that property only when status is "error occurred".
_APPLESCRIPT_MISSING_VALUE = "missing value"


class SchemeActionStatus(Enum):
    """The outcome of a scheme action, per Xcode's `scheme action result status`
    enumeration.

    UNKNOWN is not one of Xcode's values; it represents a status we could not
    read (an older Xcode, or an AppleScript error while fetching the property).
    It is deliberately NOT treated as a failure, so an unreadable status can
    never manufacture an error that did not happen.
    """

    NOT_YET_STARTED = "not yet started"
    RUNNING = "running"
    CANCELLED = "cancelled"
    FAILED = "failed"
    ERROR_OCCURRED = "error occurred"
    SUCCEEDED = "succeeded"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: Optional[str]) -> "SchemeActionStatus":
        """Map AppleScript's status text onto a case, tolerating case and spacing."""
        if raw is None:
            return cls.UNKNOWN
        normalized = " ".join(raw.strip().lower().split())
        for member in cls:
            if member.value == normalized:
                return member
        return cls.UNKNOWN

    @property
    def indicates_failure(self) -> bool:
        """True when Xcode reports the action did not do what was asked.

        `cancelled` is excluded on purpose: the monitored run tools stop the app
        themselves (user clicked "I'm finished", or a run timeout fired), and
        Xcode reports those deliberate stops as cancelled. Treating cancelled as
        a failure would turn every normal interactive run into an error.
        """
        return self in (SchemeActionStatus.FAILED, SchemeActionStatus.ERROR_OCCURRED)


@dataclass(frozen=True)
class SchemeActionReport:
    """A snapshot of a scheme action result's outcome-bearing properties."""

    found: bool
    status: SchemeActionStatus
    error_message: Optional[str]
    build_log: str


# Report for an action we could not locate at all. Its UNKNOWN status means
# callers fall back to their pre-existing behavior rather than inventing a
# failure they cannot substantiate.
ACTION_NOT_FOUND = SchemeActionReport(
    found=False, status=SchemeActionStatus.UNKNOWN, error_message=None, build_log=""
)

# Statuses with no failure for a build log to explain: the action either
# succeeded or has not finished, so its log is neither needed nor complete
# enough to draw a conclusion from. Every other status — including UNKNOWN —
# gets its log examined, so an unreadable status still detects a failed build.
#
# Note the deliberate asymmetry with SchemeActionStatus.indicates_failure, which
# excludes CANCELLED. That property answers "should this run be reported as
# failed", where a stop we asked for is not a failure. This set answers "could a
# build failure be hiding here", and CANCELLED is kept in scope for that second
# question.
#
# A run whose build fails has since been observed reporting FAILED (Xcode 27
# beta 4, run action on a project with a syntax error), so CANCELLED is not the
# status this detection depends on. It stays in scope anyway because the cost is
# one build-log read on a path that already ended abnormally, and because a
# cancelled action whose build genuinely succeeded carries no build errors and is
# cleared on the evidence rather than by assumption.
_STATUSES_NEEDING_NO_EXPLANATION = frozenset({
    SchemeActionStatus.SUCCEEDED,
    SchemeActionStatus.RUNNING,
    SchemeActionStatus.NOT_YET_STARTED,
})


def build_action_result_report_applescript(escaped_path: str,
                                           escaped_action_id: Optional[str] = None) -> str:
    """
    Return AppleScript that reports a scheme action result's status, error
    message and build log in one round trip.

    Args:
        escaped_path: Project path, already passed through escape_applescript_string.
        escaped_action_id: The action result's id, already escaped. When given,
            the action is matched by id so a concurrent build/run/test on the
            same workspace cannot make us read the wrong action's outcome. When
            None, the workspace's `last scheme action result` is used — the
            fallback for when an id could not be captured.

    Returns:
        AppleScript source whose output parse_action_result_report understands.
    """
    if escaped_action_id is not None:
        locate = (
            f'    set targetId to "{escaped_action_id}"\n'
            f'    set matchedResult to missing value\n'
            f'    repeat with candidate in scheme action results of workspaceDoc\n'
            f'        if ((id of candidate) as string) is targetId then\n'
            f'            set matchedResult to contents of candidate\n'
            f'            exit repeat\n'
            f'        end if\n'
            f'    end repeat\n'
        )
    else:
        locate = (
            f'    set matchedResult to missing value\n'
            f'    try\n'
            f'        set matchedResult to last scheme action result of workspaceDoc\n'
            f'    end try\n'
        )

    return (
        f'set projectPath to "{escaped_path}"\n'
        f'tell application "Xcode"\n'
        f'    set workspaceDoc to first workspace document whose path is projectPath\n'
        f'{locate}'
        f'    if matchedResult is missing value then return "{_REPORT_NOT_FOUND}"\n'
        + build_action_result_report_tail_applescript("matchedResult")
        + 'end tell\n'
    )


def _applescript_needs_explanation_condition() -> str:
    """Render _STATUSES_NEEDING_NO_EXPLANATION as an AppleScript guard, so the
    set of statuses worth fetching a build log for is defined in exactly one
    place and the AppleScript cannot drift from describe_build_failure."""
    return " and ".join(
        f'actionStatus is not "{status.value}"'
        for status in sorted(_STATUSES_NEEDING_NO_EXPLANATION, key=lambda s: s.value)
    )


def build_action_result_report_tail_applescript(result_var: str,
                                                leading_line_expression: Optional[str] = None) -> str:
    """
    Return the AppleScript tail that emits a report for an action result already
    in scope, for callers that hold the action they started (rather than looking
    it up again by id afterwards).

    Every property read is wrapped in `try` so one unreadable property still
    yields a usable report for the others.

    Args:
        result_var: AppleScript variable holding a scheme action result.
            Internal constant, not user input — interpolated unescaped.
        leading_line_expression: Optional AppleScript expression emitted as a
            first line ahead of the report, for callers that need to convey
            something the action result cannot express (e.g. whether the caller's
            own timeout fired). Strip it before calling
            parse_action_result_report. Internal constant, interpolated unescaped.
    """
    lead = f'{leading_line_expression} & linefeed & ' if leading_line_expression else ''
    return (
        f'    set actionStatus to "{SchemeActionStatus.UNKNOWN.value}"\n'
        f'    try\n'
        f'        set actionStatus to (status of {result_var}) as string\n'
        f'    end try\n'
        f'    set actionError to ""\n'
        f'    try\n'
        f'        set actionError to (error message of {result_var}) as string\n'
        f'    end try\n'
        # The build log is only ever consulted to explain a failure, and it can
        # run to hundreds of kilobytes on a large project. Fetch it only for the
        # statuses that might need explaining, rather than shuttling it through
        # osascript on every successful run.
        f'    set actionLog to ""\n'
        f'    if {_applescript_needs_explanation_condition()} then\n'
        f'        try\n'
        f'            set actionLog to (build log of {result_var}) as string\n'
        f'        end try\n'
        f'    end if\n'
        f'    return {lead}"{_REPORT_STATUS_PREFIX}" & actionStatus & linefeed & '
        f'"{_REPORT_ERROR_MARKER}" & linefeed & actionError & linefeed & '
        f'"{_REPORT_BUILD_LOG_MARKER}" & linefeed & actionLog\n'
    )


def parse_action_result_report(output: str) -> SchemeActionReport:
    """Parse the output of build_action_result_report_applescript.

    Returns ACTION_NOT_FOUND for the not-found sentinel or any output that does
    not carry the expected markers, so a malformed report degrades to "no
    information" rather than to a wrong verdict.
    """
    text = (output or "").strip()
    if not text or text == _REPORT_NOT_FOUND:
        return ACTION_NOT_FOUND

    if not text.startswith(_REPORT_STATUS_PREFIX) or _REPORT_BUILD_LOG_MARKER not in text:
        print(f"Warning: unrecognized scheme action report: {text[:200]!r}", file=sys.stderr)
        return ACTION_NOT_FOUND

    status_line, _, remainder = text.partition("\n")
    status = SchemeActionStatus.parse(status_line[len(_REPORT_STATUS_PREFIX):])

    if remainder.startswith(_REPORT_ERROR_MARKER):
        remainder = remainder[len(_REPORT_ERROR_MARKER):].lstrip("\n")
    error_text, _, build_log = remainder.partition(_REPORT_BUILD_LOG_MARKER)

    error_message = error_text.strip()
    if not error_message or error_message == _APPLESCRIPT_MISSING_VALUE:
        error_message = None

    return SchemeActionReport(
        found=True,
        status=status,
        error_message=error_message,
        build_log=build_log.lstrip("\n"),
    )


def describe_build_failure(report: SchemeActionReport,
                           include_warnings: Optional[bool] = None,
                           max_lines: int = 25) -> Optional[str]:
    """
    Return a JSON build-failure report when this action's build phase failed;
    None when the build log shows no build failure.

    The build log is the authority here rather than `status` alone. A run action
    reports status `failed` both when the build fails and when the app itself
    exits badly, and those need different reporting: a failed build means the app
    never launched and there are no runtime logs to collect, while a bad exit
    means the runtime logs are exactly what the caller wants. Only the build log
    distinguishes the two.

    No regex filter is applied. The run tools' `regex_filter` selects lines of
    *console* output; applying it to build diagnostics would silently hide the
    errors that explain the failure.
    """
    # A build that failed cannot have reported success, so an action Xcode calls
    # succeeded needs no further examination. Checking this first also keeps a
    # successful build whose log merely *contains* the word "error" — an echoing
    # script phase, a diagnostic quoting one — from being read as a failure and
    # discarding the runtime output the caller actually asked for. An action
    # still in flight is skipped for the converse reason: its log is incomplete,
    # so any verdict drawn from it would be premature.
    if report.status in _STATUSES_NEEDING_NO_EXPLANATION:
        return None

    if not report.build_log.strip():
        return None

    # Beyond that, decide from the log alone: passing the action's status to the
    # extractor would make every runtime failure look like a build failure,
    # which is the distinction this function exists to draw. Notably a run whose
    # build failed and a run whose app crashed can arrive here with the same
    # status, so status cannot be the discriminator.
    errors_json = extract_build_errors_and_warnings(
        report.build_log, include_warnings, None, max_lines, build_status=None
    )
    try:
        payload = json.loads(errors_json)
    except json.JSONDecodeError:
        print("Warning: could not parse build error extraction output", file=sys.stderr)
        return None

    summary = payload.get("summary", {})
    if not summary.get("build_failed") and summary.get("total_errors", 0) == 0:
        return None

    payload["summary"]["build_failed"] = True
    payload["scheme_action"] = _action_details(report, app_launched=False)
    return json.dumps(payload, indent=2)


def report_build_failure(report: SchemeActionReport,
                         include_warnings: Optional[bool] = None,
                         max_lines: int = 25) -> str:
    """
    Format an action whose build is already known to have failed, in the same
    JSON shape build_project returns.

    For a dedicated `build` action, `status` is authoritative — there is no app
    execution for it to conflate the build with — so it is passed through to the
    extractor. That matters for failures no error-line pattern matches (signing,
    provisioning, a missing package product): status is the only thing that marks
    those as failures at all.
    """
    errors_json = extract_build_errors_and_warnings(
        report.build_log, include_warnings, None, max_lines, build_status=report.status.value
    )
    try:
        payload = json.loads(errors_json)
    except json.JSONDecodeError:
        print("Warning: could not parse build error extraction output", file=sys.stderr)
        return errors_json

    payload["scheme_action"] = _action_details(report, app_launched=False)
    return json.dumps(payload, indent=2)


def annotate_with_action_status(output: str, report: SchemeActionReport) -> str:
    """
    Fold a failing action status into a tool's normal output so the caller cannot
    read the result as a clean success.

    Used for the case the build log cannot explain — the build succeeded and the
    app launched, but Xcode still reports the action as failed (a crash, or a
    nonzero exit). The runtime logs remain the useful payload, so they are kept
    and the status is attached alongside them.
    """
    if not report.status.indicates_failure:
        return output

    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return f"⚠️ Xcode reported this run as '{report.status.value}'.\n\n{output}"

    if not isinstance(payload, dict):
        return f"⚠️ Xcode reported this run as '{report.status.value}'.\n\n{output}"

    payload["scheme_action"] = _action_details(report, app_launched=True)
    return json.dumps(payload, indent=2)


def _action_details(report: SchemeActionReport, app_launched: bool) -> dict:
    """Build the `scheme_action` block attached to tool output."""
    details = {
        "status": report.status.value,
        "app_launched": app_launched,
    }
    if report.error_message:
        details["error_message"] = report.error_message
    return details
