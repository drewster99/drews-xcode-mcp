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
from typing import Dict, List, Optional, Tuple

from drews_xcode_mcp.exceptions import XCodeMCPError


def project_flag_for(project_path: str) -> str:
    """Return the xcodebuild flag (`-workspace` or `-project`) for a path."""
    return '-workspace' if project_path.endswith('.xcworkspace') else '-project'


def get_active_scheme(project_path: str) -> Optional[str]:
    """
    Return the scheme Xcode has selected, without opening Xcode.

    Xcode records the selected scheme in its workspace state, so that is read
    first. Only when there is no state to read (the project has never been
    opened) does this fall back to the scheme with the lowest orderHint in
    xcschememanagement.plist — the top of Xcode's scheme menu, which is a guess:
    on one machine it named the wrong scheme for 6 of 26 projects, picking a
    Playground scheme for two workspaces and nothing at all for four.
    Returns None if neither source can answer.
    """
    xcuserstate = find_xcuserstate(project_path)
    if xcuserstate:
        try:
            active_scheme = decode_workspace_state(xcuserstate).get('activeScheme')
        except XCodeMCPError as e:
            print(f"warn: could not read workspace state for {project_path}: {e}", file=sys.stderr)
        else:
            if active_scheme:
                return active_scheme

    return _scheme_with_lowest_order_hint(project_path)


def _scheme_with_lowest_order_hint(project_path: str) -> Optional[str]:
    """
    Return the scheme at the top of Xcode's scheme menu, read from
    xcschememanagement.plist. Returns None if the plist is missing or unreadable
    — a .xcworkspace has none of its own, its member projects hold them.
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


# Decoding runs `swift` on a helper script, which compiles it on each run.
DECODE_TIMEOUT_SECONDS = 10.0


def find_xcuserstate(project_path: str) -> str:
    """Find the most recent UserInterfaceState.xcuserstate for a project.

    Returns the file path, or "" if none exists (project never opened in Xcode).
    """
    if project_path.endswith('.xcodeproj'):
        workspace_dir = os.path.join(project_path, "project.xcworkspace")
    else:
        workspace_dir = project_path

    pattern = os.path.join(workspace_dir, "xcuserdata", "*", "UserInterfaceState.xcuserstate")
    newest_path = ""
    newest_modified_time = -1.0
    for path in glob.glob(pattern):
        try:
            modified_time = os.path.getmtime(path)
        except OSError:
            # A state file can vanish between the glob and the stat (Xcode
            # closing the workspace, xcuserdata being cleaned). One we cannot
            # stat is simply not a candidate; raising here would fail callers
            # for whom this read is only a lookup.
            continue
        if modified_time > newest_modified_time:
            newest_modified_time = modified_time
            newest_path = path
    return newest_path


def decode_workspace_state(
    xcuserstate_path: str,
    timeout_seconds: Optional[float] = None,
) -> Dict:
    """
    Run the Swift decoder over a UserInterfaceState.xcuserstate.

    Returns the decoded object: `activeScheme` (the scheme Xcode has selected,
    "" when the state does not name one) and `destinationsByScheme` (scheme name
    to raw destination identifier). `timeout_seconds` overrides the default
    decode timeout, so a caller polling against a deadline cannot be stalled
    past it. Raises XCodeMCPError naming what went wrong — a toolchain problem
    must not be reported to the caller as an empty result, which reads as "this
    project has never been run".
    """
    swift_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'decode_workspace_state.swift')
    if not os.path.exists(swift_script):
        raise XCodeMCPError(f"Workspace state decoder is missing: {swift_script}")

    try:
        result = subprocess.run(
            ['swift', swift_script, xcuserstate_path],
            capture_output=True, text=True,
            timeout=timeout_seconds if timeout_seconds is not None else DECODE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise XCodeMCPError("Decoding Xcode's workspace state timed out")
    except FileNotFoundError:
        raise XCodeMCPError("`swift` was not found on PATH, so Xcode's workspace state cannot be read")

    if result.returncode != 0:
        raise XCodeMCPError(
            f"Decoding Xcode's workspace state failed ({result.returncode}): {result.stderr.strip()}"
        )

    if not result.stdout.strip():
        raise XCodeMCPError("Decoding Xcode's workspace state produced no output")

    try:
        decoded = json.loads(result.stdout.strip())
    except json.JSONDecodeError as e:
        raise XCodeMCPError(f"Decoding Xcode's workspace state produced invalid JSON: {e}")

    if not isinstance(decoded, dict):
        raise XCodeMCPError(
            f"Decoding Xcode's workspace state produced {type(decoded).__name__}, expected an object"
        )
    return decoded


# Xcode builds the identifier in
# -[IDERunDestination(IDERunContextRecents) _stateSavingIdentifierForDestinationSchemeRunnableIsForWatch:]
# by joining, with '_', whichever of these are present: the device identifier,
# the platform name, an "I" marker for an Apple-internal SDK, the SDK variant
# (written only when it differs from the platform name), the architecture, the
# SDK's first cohort platform, and the identifier of the device this one proxies
# for a watch app.
#
# The architecture is therefore the only open-ended component, and several
# architecture names contain the same '_' the components are joined with
# (arm64_32, x86_64, x86_64h). So the parse anchors on the closed sets below
# and takes whatever remains as the architecture verbatim: a new architecture
# then survives intact instead of being split in half.

# Apple declares an SDK's variants in <SDK>/SDKSettings.plist. macOS is the only
# SDK that has any: "macos" (My Mac) and "iosmac" (Mac Catalyst).
RUN_DESTINATION_SDK_VARIANTS = frozenset({'macos', 'iosmac'})

# Platform names as DVTPlatform reports them, from Platforms/*.platform/Info.plist.
# Used only to recognize a trailing cohort platform, never to validate the
# platform component, so an unlisted platform cannot break the parse.
RUN_DESTINATION_PLATFORMS = frozenset({
    'macosx', 'iphoneos', 'iphonesimulator', 'watchos', 'watchsimulator',
    'appletvos', 'appletvsimulator', 'xros', 'xrsimulator', 'driverkit',
})

# Marker Xcode inserts for an Apple-internal SDK.
_INTERNAL_SDK_MARKER = 'I'


@dataclass(frozen=True)
class RunDestinationIdentifier:
    """
    The components of one run destination identifier stored in Xcode's
    workspace state.

    `sdk` is the SDK name Xcode records ("iphonesimulator", "iphoneos",
    "macosx", ...) — note this is not the same vocabulary as the `platform`
    field `xcodebuild -showdestinations` reports ("iOS Simulator", "macOS").
    `sdk_variant` is the variant component the macOS SDK carries ("macos" for
    My Mac, "iosmac" for Mac Catalyst) and is "" for every other destination —
    including "My Mac (Designed for iPad)", which Xcode records as the Mac's
    device identifier under the iphoneos platform. `sdk` and `architecture` are
    "" when the identifier does not carry them.
    """
    id: str
    sdk: str
    sdk_variant: str
    architecture: str
    identifier: str


def parse_run_destination_identifier(identifier: str) -> Optional[RunDestinationIdentifier]:
    """
    Split one stored run destination identifier into its components.

    Identifiers look like "12521A3C-..._iphonesimulator_arm64",
    "00006040-..._macosx_macos_arm64", a watch app's
    "00008301-..._watchos_arm64_32_<paired iPhone UDID>", or, for a generic
    destination that names neither a device nor an architecture,
    "dvtdevice-DVTiPhonePlaceholder-iphoneos:placeholder_iphoneos".

    Everything left after the components this module recognizes is the
    architecture, verbatim. Returns None only when there is no device
    identifier at all.
    """
    tokens = identifier.split('_')
    if not tokens[0]:
        return None

    remainder = tokens[2:]
    if remainder and remainder[0] == _INTERNAL_SDK_MARKER:
        remainder = remainder[1:]

    sdk_variant = ''
    if remainder and remainder[0] in RUN_DESTINATION_SDK_VARIANTS:
        sdk_variant = remainder[0]
        remainder = remainder[1:]

    # A trailing paired-device identifier always carries a '-' or a ':', and a
    # trailing cohort platform is a known platform name, so neither can be
    # mistaken for part of an architecture.
    if len(remainder) > 1 and ('-' in remainder[-1] or ':' in remainder[-1]):
        remainder = remainder[:-1]
    if len(remainder) > 1 and remainder[-1] in RUN_DESTINATION_PLATFORMS:
        remainder = remainder[:-1]

    return RunDestinationIdentifier(
        id=tokens[0],
        sdk=tokens[1] if len(tokens) > 1 else '',
        sdk_variant=sdk_variant,
        architecture='_'.join(remainder),
        identifier=identifier,
    )


@dataclass(frozen=True)
class WorkspaceRunState:
    """
    What Xcode's workspace state says about running this project: the scheme it
    has selected ("" when the state does not name one) and the run destination
    it last used for each scheme.
    """
    active_scheme: str
    destinations: Dict[str, RunDestinationIdentifier]


def read_workspace_run_state(
    project_path: str,
    timeout_seconds: Optional[float] = None,
) -> WorkspaceRunState:
    """
    Read Xcode's workspace state for a project (no Xcode side effects).

    This is the one reader of that state: callers wanting a particular scheme's
    destination index into `destinations`, and select_active_run_destination
    applies the "which scheme is active" choice on top. `timeout_seconds` caps
    the decode, for callers polling against a deadline. Raises XCodeMCPError
    when the state cannot be read or holds no destinations.
    """
    xcuserstate = find_xcuserstate(project_path)
    if not xcuserstate:
        raise XCodeMCPError(
            "No workspace state file found. The project may not have been "
            "opened in Xcode yet."
        )

    decoded = decode_workspace_state(xcuserstate, timeout_seconds=timeout_seconds)
    raw_destinations = decoded.get('destinationsByScheme') or {}
    if not raw_destinations:
        raise XCodeMCPError(
            "Xcode's workspace state records no run destination. The project "
            "may not have been built or run yet."
        )

    destinations = {}
    for scheme, identifier in raw_destinations.items():
        if not isinstance(identifier, str):
            continue
        destination = parse_run_destination_identifier(identifier)
        if destination is not None:
            destinations[scheme] = destination

    if not destinations:
        raise XCodeMCPError("No usable run destination found in Xcode's workspace state.")

    active_scheme = decoded.get('activeScheme')
    return WorkspaceRunState(
        active_scheme=active_scheme if isinstance(active_scheme, str) else '',
        destinations=destinations,
    )


def select_active_run_destination(
    state: WorkspaceRunState,
    project_path: str,
    scheme: Optional[str] = None,
) -> Tuple[str, RunDestinationIdentifier]:
    """
    Choose which scheme's stored destination to treat as the active one.

    Prefers the given scheme, then the scheme Xcode has selected, then the top
    of the scheme menu, then any stored scheme. Takes an already-read state so
    a caller polling it does not decode twice.
    """
    if scheme and scheme in state.destinations:
        return scheme, state.destinations[scheme]

    if state.active_scheme in state.destinations:
        return state.active_scheme, state.destinations[state.active_scheme]

    menu_top_scheme = _scheme_with_lowest_order_hint(project_path)
    if menu_top_scheme and menu_top_scheme in state.destinations:
        return menu_top_scheme, state.destinations[menu_top_scheme]

    selected_scheme = next(iter(state.destinations))
    return selected_scheme, state.destinations[selected_scheme]


def read_active_run_destination(
    project_path: str,
    scheme: Optional[str] = None,
) -> Tuple[str, RunDestinationIdentifier]:
    """
    Return the (scheme, destination) pair Xcode's workspace state reports as
    active (no Xcode side effects).

    Raises XCodeMCPError when the state cannot be read.
    """
    state = read_workspace_run_state(project_path)
    return select_active_run_destination(state, project_path, scheme)


def resolve_active_destination_id(project_path: str, scheme: Optional[str] = None) -> Optional[str]:
    """
    Return the UDID of the active run destination, or None when it cannot be
    determined — for callers that treat the active destination as a preference
    rather than a requirement.
    """
    try:
        return read_active_run_destination(project_path, scheme)[1].id or None
    except (XCodeMCPError, OSError):
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
