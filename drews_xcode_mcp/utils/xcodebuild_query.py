#!/usr/bin/env python3
"""Shared xcodebuild query helpers for scheme and run-destination discovery.

These wrap read-only `xcodebuild -list` / `-showdestinations` invocations that
have no Xcode side effects, so multiple tools (list_run_destinations,
list_project_tests, and the selective test runner) can resolve a scheme and a
buildable destination the same way.
"""

import glob
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from drews_xcode_mcp.exceptions import XCodeMCPError


def project_flag_for(project_path: str) -> str:
    """Return the xcodebuild flag (`-workspace` or `-project`) for a path."""
    return '-workspace' if project_path.endswith('.xcworkspace') else '-project'


def get_active_scheme(project_path: str) -> Optional[str]:
    """
    Return the active scheme from xcschememanagement.plist without opening Xcode.

    "Active" here means the scheme with the lowest orderHint (the top of Xcode's
    scheme menu). This is a side-effect-free heuristic; the only way to read the
    truly-selected scheme is AppleScript, which would open the project in Xcode.
    Returns None if the plist is missing or unreadable.
    """
    pattern = os.path.join(project_path, "xcuserdata", "*", "xcschemes", "xcschememanagement.plist")
    matches = glob.glob(pattern)
    if not matches:
        return None

    plist_path = max(matches, key=os.path.getmtime)
    try:
        result = subprocess.run(
            ['plutil', '-convert', 'json', '-o', '-', plist_path],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        print(f"warn: plutil timed out reading {plist_path}", file=sys.stderr)
        return None
    except FileNotFoundError:
        print("warn: `plutil` binary not found on PATH", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(
            f"warn: plutil exited {result.returncode} for {plist_path}: "
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"warn: plutil produced invalid JSON for {plist_path}: {e}", file=sys.stderr)
        return None

    scheme_state = data.get("SchemeUserState", {})
    best_scheme = None
    best_order = float('inf')
    for key, value in scheme_state.items():
        scheme_name = key.split('.xcscheme')[0]
        order = value.get('orderHint', 999)
        if order < best_order:
            best_order = order
            best_scheme = scheme_name
    return best_scheme


def get_first_scheme(project_path: str) -> Optional[str]:
    """Return the first scheme name via `xcodebuild -list` (no Xcode side effects)."""
    try:
        result = subprocess.run(
            ['xcodebuild', '-list', project_flag_for(project_path), project_path],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        print(f"warn: xcodebuild -list timed out for {project_path}", file=sys.stderr)
        return None
    except FileNotFoundError:
        print("warn: `xcodebuild` binary not found on PATH", file=sys.stderr)
        return None

    in_schemes = False
    for line in result.stdout.split('\n'):
        stripped = line.strip()
        if stripped == 'Schemes:':
            in_schemes = True
            continue
        if in_schemes:
            if stripped == '' or stripped.endswith(':'):
                break
            return stripped
    return None


def parse_destination_line(line: str) -> Optional[Dict]:
    """
    Parse a single `xcodebuild -showdestinations` destination line.
    Format: { platform:iOS Simulator, arch:arm64, id:ABC123, OS:26.4, name:iPhone 17 Pro }

    Ineligible destinations carry a trailing `error:` describing why the
    scheme can't use them; that key is preserved so callers can filter on it.
    Returns None for lines without both a name and an id (e.g. generic
    "Any Mac" placeholders).
    """
    line = line.strip()
    if not line.startswith('{') or not line.endswith('}'):
        return None

    inner = line[1:-1].strip()
    if not inner:
        return None

    # Parse key:value pairs — keys are simple words, values run until next ", key:" or end
    result = {}
    pattern = r'(\w+):(.+?)(?=, \w+:|$)'
    for match in re.finditer(pattern, inner):
        key = match.group(1).strip()
        value = match.group(2).strip()
        result[key] = value

    if not result.get('name') or not result.get('id'):
        return None

    return result


def list_destinations(project_path: str, scheme: str, timeout: int = 30) -> List[Dict]:
    """
    Return parsed destinations for a scheme via `xcodebuild -showdestinations`.

    Generic placeholder destinations (those whose id contains 'placeholder')
    are dropped. Ineligible destinations are kept, each with an 'error' field.
    Raises subprocess.TimeoutExpired if xcodebuild does not respond in time.
    """
    result = subprocess.run(
        ['xcodebuild', '-showdestinations', project_flag_for(project_path),
         project_path, '-scheme', scheme],
        capture_output=True, text=True, timeout=timeout,
    )
    output = result.stdout + result.stderr

    destinations = []
    for line in output.split('\n'):
        line = line.strip()
        if line.startswith('{') and line.endswith('}'):
            parsed = parse_destination_line(line)
            if parsed and 'placeholder' not in parsed.get('id', ''):
                destinations.append(parsed)
    return destinations


def find_xcuserstate(project_path: str) -> str:
    """Find the most recent UserInterfaceState.xcuserstate for a project.

    Returns the file path, or "" if none exists (project never opened in Xcode).
    """
    if project_path.endswith('.xcodeproj'):
        workspace_dir = os.path.join(project_path, "project.xcworkspace")
    else:
        workspace_dir = project_path

    pattern = os.path.join(workspace_dir, "xcuserdata", "*", "UserInterfaceState.xcuserstate")
    matches = glob.glob(pattern)
    if not matches:
        return ""
    return max(matches, key=os.path.getmtime)


def decode_active_destinations(xcuserstate_path: str) -> Dict:
    """Run the Swift decoder to extract the active destination per scheme.

    Returns a dict like {"SchemeName": "UDID_platform_arch"}, or an empty dict
    on any failure (missing script, swift not found, timeout, bad output).
    """
    swift_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'decode_active_destination.swift')
    if not os.path.exists(swift_script):
        print(f"warn: helper script not found: {swift_script}", file=sys.stderr)
        return {}

    try:
        result = subprocess.run(
            ['swift', swift_script, xcuserstate_path],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        print("warn: decode_active_destination.swift timed out", file=sys.stderr)
        return {}
    except FileNotFoundError:
        print("warn: `swift` binary not found on PATH", file=sys.stderr)
        return {}

    if result.returncode != 0:
        print(
            f"warn: decode_active_destination.swift exited {result.returncode}: "
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )
        return {}

    if not result.stdout.strip():
        return {}

    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError as e:
        print(f"warn: decode_active_destination.swift produced invalid JSON: {e}", file=sys.stderr)
        return {}


# Architectures Xcode embeds in a run destination identifier. Several contain an
# underscore (arm64_32, x86_64), so an identifier cannot be split on '_' alone:
# the architecture is the longest of these that the identifier ends with.
# Ordered longest-match-first.
RUN_DESTINATION_ARCHITECTURES = (
    'arm64_32', 'arm64e', 'arm64', 'x86_64', 'i386', 'armv7k', 'armv7s', 'armv7',
)


@dataclass(frozen=True)
class ActiveRunDestination:
    """
    The run destination Xcode has stored for a scheme in its workspace state.

    `platform` is the SDK name Xcode records ("iphonesimulator", "iphoneos",
    "macosx", ...). `variant` is the extra platform component Mac destinations
    carry ("macos"), and is "" for identifiers without one.
    """
    scheme: str
    id: str
    platform: str
    architecture: str
    variant: str
    identifier: str


def parse_run_destination_identifier(identifier: str, for_scheme: str) -> Optional[ActiveRunDestination]:
    """
    Parse one stored run destination identifier into its components.

    Identifiers are "<UDID>_<sdk>[_<variant>]_<arch>", e.g.
    "12521A3C-..._iphonesimulator_arm64" or "00006040-..._macosx_macos_arm64".
    An architecture this module does not know is taken to be the final
    component, which leaves the variant short if that architecture itself
    contains an underscore. Returns None if the identifier does not carry at
    least a UDID, an SDK and an architecture.
    """
    tokens = identifier.split('_')
    if len(tokens) < 3:
        return None

    remainder = '_'.join(tokens[2:])
    for candidate in RUN_DESTINATION_ARCHITECTURES:
        if remainder == candidate or remainder.endswith('_' + candidate):
            architecture = candidate
            variant = remainder[:len(remainder) - len(candidate)].rstrip('_')
            break
    else:
        architecture = tokens[-1]
        variant = '_'.join(tokens[2:-1])

    return ActiveRunDestination(
        scheme=for_scheme,
        id=tokens[0],
        platform=tokens[1],
        architecture=architecture,
        variant=variant,
        identifier=identifier,
    )


def read_active_run_destination(project_path: str, scheme: Optional[str] = None) -> ActiveRunDestination:
    """
    Return the active run destination recorded in Xcode's workspace state (no
    Xcode side effects).

    Prefers the given scheme's stored destination, then the active scheme's,
    then any stored destination. Raises XCodeMCPError naming the step that
    failed: the project has never been opened in Xcode, nothing has been run
    yet, or the stored identifier could not be parsed.
    """
    xcuserstate = find_xcuserstate(project_path)
    if not xcuserstate:
        raise XCodeMCPError(
            "No workspace state file found. The project may not have been "
            "opened in Xcode yet."
        )

    scheme_destinations = decode_active_destinations(xcuserstate)
    if not scheme_destinations:
        raise XCodeMCPError(
            "Could not determine active run destination. The project may not "
            "have been built or run yet."
        )

    if scheme and scheme in scheme_destinations:
        selected_scheme = scheme
    else:
        active_scheme = get_active_scheme(project_path)
        if active_scheme and active_scheme in scheme_destinations:
            selected_scheme = active_scheme
        else:
            selected_scheme = next(iter(scheme_destinations))

    identifier = scheme_destinations[selected_scheme]
    if not identifier:
        raise XCodeMCPError("No active run destination found in workspace state.")

    destination = parse_run_destination_identifier(identifier, for_scheme=selected_scheme)
    if destination is None:
        raise XCodeMCPError(f"Unexpected destination format: {identifier}")
    return destination


def resolve_active_destination_id(project_path: str, scheme: Optional[str] = None) -> Optional[str]:
    """
    Return the UDID of the active run destination, or None when it cannot be
    determined — for callers that treat the active destination as a preference
    rather than a requirement.
    """
    try:
        return read_active_run_destination(project_path, scheme).id
    except XCodeMCPError:
        return None


def _destination_test_rank(dest: Dict) -> int:
    """
    Rank a compatible destination by how well it supports building and loading a
    test bundle (lower is better):

      0  Simulator — no code signing, the test bundle loads reliably, and it
         enumerates the same tests as the matching device.
      1  Native macOS — for genuine Mac apps (no "Designed for iPad" variant).
      2  Everything else — physical devices (need signing/attachment) and the
         "My Mac (Designed for iPad)" Catalyst-style destination, where an iOS
         test bundle can fail to load.
    """
    platform = dest.get('platform', '')
    variant = dest.get('variant', '')
    if 'Simulator' in platform:
        return 0
    if platform == 'macOS' and 'Designed for' not in variant:
        return 1
    return 2


# `-showdestinations` can omit a scheme's simulators transiently; retry a few
# times before settling for a rank-2 "trap" destination.
_RESOLVE_DESTINATION_ATTEMPTS = 5
_RESOLVE_DESTINATION_RETRY_DELAY = 2.0


def resolve_buildable_destination(project_path: str, scheme: str) -> Optional[str]:
    """
    Return a `-destination` argument value (e.g. "id=00006040-...") for the
    destination best suited to building and enumerating the scheme's tests.

    Considers only destinations xcodebuild reports as compatible (no 'error').
    Prefers the active run destination — what the user has selected in Xcode —
    so a macOS app builds on My Mac and an iOS app builds on its selected
    simulator, matching the IDE. The active destination is only trusted when it
    is a safe build target (a simulator or a native Mac, not a physical device
    that needs signing/attachment); otherwise, and when no active destination is
    stored, it falls back to a preference of simulator > native Mac > other (see
    _destination_test_rank). That avoids both the "My Mac (Designed for iPad)"
    trap for iOS apps and the wrong-platform (iOS simulator) pick for a
    multiplatform macOS library.

    `-showdestinations` intermittently omits a scheme's simulators while
    CoreSimulator is busy (e.g. right after a prior build-for-testing). If the
    only compatible destinations are rank-2 "traps" (physical devices and the
    "Designed for iPad" Mac), this retries to give the simulators a chance to
    reappear — otherwise enumeration would land on a destination where the iOS
    test bundle can't load.

    Returns None if no compatible destination can be determined (e.g. xcodebuild
    timed out, or the scheme has no eligible destinations).
    """
    # The active destination doesn't change between retries, so resolve it once.
    active_udid = resolve_active_destination_id(project_path, scheme)

    best = None
    for attempt in range(_RESOLVE_DESTINATION_ATTEMPTS):
        try:
            destinations = list_destinations(project_path, scheme)
        except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError):
            return None

        compatible = [dest for dest in destinations if 'error' not in dest]
        if not compatible:
            return None

        # Prefer the active run destination when it's compatible AND a safe build
        # target. A device (rank 2) is skipped so enumeration doesn't fail on
        # code-signing; we fall back to a simulator of the same platform instead.
        if active_udid:
            active_entries = [d for d in compatible if d.get('id') == active_udid]
            if active_entries and min(_destination_test_rank(d) for d in active_entries) < 2:
                return f"id={active_udid}"

        # min() is stable, so this keeps xcodebuild's order within a rank.
        best = min(compatible, key=_destination_test_rank)
        if _destination_test_rank(best) < 2:
            return f"id={best['id']}"

        # Only rank-2 traps are listed — the simulators are probably just not
        # enumerated yet. Wait and re-query before settling for the trap.
        if attempt < _RESOLVE_DESTINATION_ATTEMPTS - 1:
            time.sleep(_RESOLVE_DESTINATION_RETRY_DELAY)

    return f"id={best['id']}" if best else None
