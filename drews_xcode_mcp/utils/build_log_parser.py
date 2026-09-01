#!/usr/bin/env python3
"""Build log parsing utilities for aggregating warnings across multiple builds"""

import os
import sys
import time
import gzip
import re
import plistlib
import threading
from collections import OrderedDict
from typing import List, Dict, Set, Tuple, Optional

# Seconds between the Unix epoch (1970-01-01) and the Cocoa/CFAbsoluteTime
# epoch (2001-01-01). Xcode's LogStoreManifest.plist stores timeStartedRecording
# in CFAbsoluteTime — subtract this offset from time.time() to compare.
CF_EPOCH_OFFSET = 978307200.0

# Source-file extensions we recognize in xcactivitylog warnings/errors and
# compile records. Ordered longest-first so the regex alternation prefers
# .mm over .m, .cpp over .c, etc.
_SOURCE_EXTS = (
    'swift', 'metal',
    'mm', 'cpp', 'cxx', 'cc', 'hpp',
    'm', 'c', 'h',
)
_SOURCE_EXT_ALT = '|'.join(_SOURCE_EXTS)

# xcactivitylog payloads are SLF0 streams: a 4-byte "SLF0" magic followed by
# back-to-back tokens with no separators. Each token is an ASCII prefix plus a
# one-byte type marker:
#   <decimal>#   integer            <hex>^        IEEE-754 double (raw bits)
#   <decimal>"   string, prefix = payload byte length
#   <decimal>*   JSON blob, same shape as a string (task metrics attachments)
#   <decimal>%   class name, same shape as a string
#   <decimal>@   class-instance reference (no payload)
#   <decimal>(   list header (no payload)
#   -            null (no prefix)
# Diagnostics and compile records live inside `"` string payloads; everything
# else is structure we can skip. Decoding token-by-token is what keeps a path
# or message from bleeding across token boundaries — the byte length bounds
# each string exactly.
_SLF0_MAGIC = b'SLF0'
_SLF0_TOKEN_PREFIX_RE = re.compile(rb'[0-9a-f]*')

# Process-lifetime cache for parse_xcactivitylog results. xcactivitylog files
# are written once by Xcode and never mutated, so a path+stat key is a stable
# cache identity for the file's lifetime. Bounded to keep memory predictable
# under long-running MCP sessions; LRU eviction discards the least-recently
# used entries first.
_XCACTIVITYLOG_CACHE_MAX = 200
_XCACTIVITYLOG_CACHE: "OrderedDict[Tuple[str, int, int], Tuple[List[Dict], Set[str]]]" = OrderedDict()
_XCACTIVITYLOG_CACHE_LOCK = threading.Lock()


def _xcactivitylog_cache_key(log_path: str) -> Optional[Tuple[str, int, int]]:
    """Return a cache key for `log_path`, or None if the file isn't stat-able.

    The key uses mtime_ns + size so that if a path is ever reused (unlikely —
    Xcode names logs by UUID — but cheap insurance) the cache rejects a stale
    hit instead of returning data from the previous occupant.
    """
    try:
        st = os.stat(log_path)
    except OSError:
        return None
    return (log_path, st.st_mtime_ns, st.st_size)


# Path char class: any printable byte except control bytes. We deliberately do
# NOT exclude U+FFFD here — `errors='surrogateescape'` keeps undecodable bytes
# as low-surrogate code points (\udc80–\udcff), so the replacement char
# (U+FFFD, �) only appears if it was literally in the source file. Excluding it
# would silently truncate paths whenever a single non-UTF-8 byte appears nearby.
_PATH_CHAR = r'[^\r\n\x00-\x1f]'

# Warning/error lines:
#   /abs/path/to/File.ext:LINE:COL: warning|error: message
# The non-greedy {path}+? combined with the literal `.ext:digits:digits:`
# naturally terminates the path at the right place even when it contains
# spaces. Limit the message to non-control characters so we don't slurp
# binary trailing bytes.
_MSG_CHAR = r'[^\n\r\x00-\x08\x0b-\x1f]'
_WARNING_PATTERN = re.compile(
    rf'(/{_PATH_CHAR}+?\.(?:{_SOURCE_EXT_ALT})):(\d+):(\d+): warning: ({_MSG_CHAR}+)'
)
_ERROR_PATTERN = re.compile(
    rf'(/{_PATH_CHAR}+?\.(?:{_SOURCE_EXT_ALT})):(\d+):(\d+): error: ({_MSG_CHAR}+)'
)

# SwiftCompile lines:
#   SwiftCompile normal <arch> /abs/path/to/File.swift (in target '...' from project '...')
# The path ends at " (in target" or at end-of-line / control byte.
_SWIFT_COMPILE_PATTERN = re.compile(
    rf'SwiftCompile normal \S+ (/{_PATH_CHAR}+?\.swift)(?= \(in target|\s*[\r\n]|\s*$)'
)

# CompileC / CompileCpp / CompileObjC / CompileObjCpp / CompileMetalFile:
# Format starts with the object-file path then a space then the source path:
#   CompileC <output>.o <source>.m normal arm64 objective-c ...
#   CompileMetalFile <source>.metal ...
# We want the SOURCE path, which is the last filename-with-known-extension.
# The object-file path must allow spaces too (DerivedData lives under the
# user's home, which can contain a space) — `\S+` would break the whole
# match and silently drop the source file from compiled_files, leaving its
# warnings stale. Mirror the source group's space-tolerant style.
_CC_COMPILE_PATTERN = re.compile(
    rf'Compile(?:C|Cpp|ObjC|ObjCpp) /{_PATH_CHAR}+?\.o (/{_PATH_CHAR}+?\.(?:m|mm|c|cc|cpp|cxx))(?= \(in target|\s+normal\s|\s+[a-z]|\s*[\r\n]|\s*$)'
)
_METAL_COMPILE_PATTERN = re.compile(
    rf'CompileMetalFile (/{_PATH_CHAR}+?\.metal)(?= \(in target|\s*[\r\n]|\s*$)'
)


def _strip_surrogates(s: str) -> str:
    """Drop lone surrogates introduced by surrogateescape so output is valid UTF-8."""
    return s.encode('utf-8', errors='replace').decode('utf-8', errors='replace')


def _iter_slf0_string_payloads(data: bytes, log_path: str):
    """
    Walk an SLF0 byte stream and yield each string token's payload, decoded.

    Only `"` string payloads are yielded — class names and JSON metrics
    attachments never carry diagnostics, and structural tokens have no payload.

    A truncated stream (Xcode still writing the file) ends iteration after the
    bytes present; a partially-written final string is still yielded so a
    diagnostic already flushed into it isn't lost. A malformed token — which
    would mean the SLF0 format itself changed — is reported to stderr with its
    offset and stops iteration rather than guessing at a resync point: token
    boundaries are only knowable from the length prefixes, and scanning
    misaligned bytes is exactly the corruption this decoder exists to prevent.
    """
    pos = len(_SLF0_MAGIC)
    n = len(data)
    while pos < n:
        if data[pos] == 0x2d:  # '-' null token, no prefix
            pos += 1
            continue
        prefix_match = _SLF0_TOKEN_PREFIX_RE.match(data, pos)
        prefix = prefix_match.group(0)
        pos = prefix_match.end()
        if pos >= n:
            # Truncated mid-prefix — the writer hasn't finished this token.
            return
        token_type = data[pos]
        pos += 1
        if not prefix:
            print(
                f"Warning: malformed SLF0 token (no prefix) at offset {pos - 1} "
                f"in {log_path}; stopping scan",
                file=sys.stderr,
            )
            return
        if token_type in b'#@(^':
            continue
        if token_type in b'"*%':
            if not prefix.isdigit():
                print(
                    f"Warning: malformed SLF0 length {prefix!r} at offset "
                    f"{pos - 1} in {log_path}; stopping scan",
                    file=sys.stderr,
                )
                return
            length = int(prefix)
            payload = data[pos:pos + length]
            pos += length
            if token_type == 0x22:  # '"'
                yield payload.decode('utf-8', errors='surrogateescape')
            continue
        print(
            f"Warning: unknown SLF0 token type {chr(token_type)!r} at offset "
            f"{pos - 1} in {log_path}; stopping scan",
            file=sys.stderr,
        )
        return


def _scan_log_text(text: str, warnings: List[Dict], compiled_files: Set[str],
                   seen_diagnostics: Set[Tuple]) -> None:
    """
    Scan one chunk of build-log text for diagnostics and compile records,
    appending to `warnings` / `compiled_files` in place.

    Xcode uses \\r as the line separator inside log text (and \\n inside
    multi-line diagnostic strings), so both are treated as line breaks.

    SLF0 stores the same diagnostic in more than one string (a section's full
    text plus per-message copies), so `seen_diagnostics` dedups on the complete
    (file, line, column, type, message) identity across chunks.
    """
    for raw_line in text.replace('\n', '\r').split('\r'):
        if ': warning:' in raw_line:
            for match in _WARNING_PATTERN.finditer(raw_line):
                _append_diagnostic(match, 'warning', warnings, seen_diagnostics)
        if ': error:' in raw_line:
            for match in _ERROR_PATTERN.finditer(raw_line):
                _append_diagnostic(match, 'error', warnings, seen_diagnostics)
        if 'SwiftCompile normal ' in raw_line:
            for match in _SWIFT_COMPILE_PATTERN.finditer(raw_line):
                compiled_files.add(_strip_surrogates(match.group(1)))
        if 'CompileC ' in raw_line or 'CompileCpp ' in raw_line \
                or 'CompileObjC ' in raw_line or 'CompileObjCpp ' in raw_line:
            for match in _CC_COMPILE_PATTERN.finditer(raw_line):
                compiled_files.add(_strip_surrogates(match.group(1)))
        if 'CompileMetalFile ' in raw_line:
            for match in _METAL_COMPILE_PATTERN.finditer(raw_line):
                compiled_files.add(_strip_surrogates(match.group(1)))


def _append_diagnostic(match: "re.Match", diagnostic_type: str,
                       warnings: List[Dict], seen_diagnostics: Set[Tuple]) -> None:
    """Append one matched warning/error unless an identical one was already seen."""
    entry = {
        'file': _strip_surrogates(match.group(1)),
        'line': int(match.group(2)),
        'column': int(match.group(3)),
        'message': _strip_surrogates(match.group(4).strip()),
        'type': diagnostic_type,
    }
    key = (entry['file'], entry['line'], entry['column'], entry['type'], entry['message'])
    if key in seen_diagnostics:
        return
    seen_diagnostics.add(key)
    warnings.append(entry)


class ManifestParseError(Exception):
    """Raised when a LogStoreManifest.plist exists but cannot be parsed.

    Distinct from "the manifest doesn't exist yet" (the function returns []
    in that case), so callers can surface "warnings unavailable: manifest
    corrupted" rather than implying no prior builds happened.
    """


def parse_manifest_plist(manifest_path: str) -> List[Dict]:
    """
    Parse LogStoreManifest.plist and extract build metadata.

    Args:
        manifest_path: Path to LogStoreManifest.plist

    Returns:
        List of build metadata dictionaries, sorted chronologically by
        timeStartedRecording. Each dict contains: uuid, fileName,
        timeStartedRecording, title, scheme_name, errors, warnings, status.

        Returns an empty list when the manifest file is missing (the
        first-build case).

    Raises:
        ManifestParseError: When the manifest exists but cannot be read or
            parsed (corrupted, mid-write, permission denied). Callers should
            surface this distinct from the empty case.
    """
    if not os.path.exists(manifest_path):
        return []

    try:
        with open(manifest_path, 'rb') as f:
            plist_data = plistlib.load(f)
    except (plistlib.InvalidFileException, OSError, ValueError) as e:
        print(f"Error parsing manifest plist {manifest_path}: {e}", file=sys.stderr)
        raise ManifestParseError(str(e)) from e

    builds = []
    logs = plist_data.get('logs', {}) if isinstance(plist_data, dict) else {}

    for uuid, log_entry in logs.items():
        # Defensive: Xcode is the only writer, but a partially-corrupted entry
        # (or a future schema change that nests differently) shouldn't take
        # down the whole parse — skip and continue.
        if not isinstance(log_entry, dict):
            continue
        primary = log_entry.get('primaryObservable', {})
        if not isinstance(primary, dict):
            primary = {}

        scheme_name = log_entry.get('schemeIdentifier-schemeName', '')
        if not isinstance(scheme_name, str):
            scheme_name = ''

        build_info = {
            'uuid': uuid,
            'fileName': log_entry.get('fileName', ''),
            'timeStartedRecording': log_entry.get('timeStartedRecording', 0),
            'title': log_entry.get('title', ''),
            'scheme_name': scheme_name,
            'errors': primary.get('totalNumberOfErrors', 0),
            'warnings': primary.get('totalNumberOfWarnings', 0),
            'status': primary.get('highLevelStatus', 'U')  # S=Success, W=Warning, E=Error, U=Unknown
        }

        builds.append(build_info)

    # Sort chronologically by timeStartedRecording
    builds.sort(key=lambda x: x['timeStartedRecording'])

    return builds


def parse_xcactivitylog(log_path: str) -> Tuple[List[Dict], Set[str]]:
    """
    Parse an .xcactivitylog file to extract warnings and compiled files.

    The log files are gzip-compressed SLF0 token streams. The stream is decoded
    token by token and only string payloads are scanned, so a diagnostic's file
    path can never absorb adjacent binary tokens (scanning the raw stream with
    regexes used to produce warnings whose `file` spanned kilobytes of SLF0
    structure between an unrelated path and the real one). Content without the
    SLF0 magic is scanned as plain text instead.

    Identical diagnostics are deduplicated: SLF0 repeats each message in
    several strings (section text plus per-message copies), and counting them
    more than once would inflate the reported warning totals.

    Results are cached for the process lifetime keyed on path+mtime+size, so
    re-aggregating across the same N immutable build logs after the first call
    is essentially free.

    Args:
        log_path: Path to the .xcactivitylog file

    Returns:
        Tuple of (warnings_list, compiled_files_set)
        - warnings_list: List of dicts with keys: file, line, column, message
        - compiled_files_set: Set of file paths that were compiled in this build
    """
    cache_key = _xcactivitylog_cache_key(log_path)
    if cache_key is not None:
        with _XCACTIVITYLOG_CACHE_LOCK:
            hit = _XCACTIVITYLOG_CACHE.get(cache_key)
            if hit is not None:
                # Move to end so LRU eviction prefers older keys.
                _XCACTIVITYLOG_CACHE.move_to_end(cache_key)
                cached_warnings, cached_compiled = hit
                # Return copies so callers can't mutate the cache. Warnings
                # are dicts; aggregate_warnings_since_clean mutates entries to
                # attach build_uuid/build_time, so a shallow copy of each
                # dict is required.
                return [dict(w) for w in cached_warnings], set(cached_compiled)

    warnings = []
    compiled_files = set()
    seen_diagnostics = set()

    try:
        with gzip.open(log_path, 'rb') as f:
            content = f.read()

        if content.startswith(_SLF0_MAGIC):
            # Decode the SLF0 token stream and scan only string payloads, so
            # a match can never span two tokens (each string's byte length
            # bounds it exactly).
            for payload in _iter_slf0_string_payloads(content, log_path):
                _scan_log_text(payload, warnings, compiled_files, seen_diagnostics)
        else:
            # Not an SLF0 stream. Real Xcode logs always carry the magic, but
            # a future format change shouldn't silently drop all warnings, and
            # the test suite feeds plain-text logs through this path. Scan the
            # whole decoded text with the same line matcher.
            print(
                f"Note: {log_path} has no SLF0 magic; scanning as plain text",
                file=sys.stderr,
            )
            text = content.decode('utf-8', errors='surrogateescape')
            _scan_log_text(text, warnings, compiled_files, seen_diagnostics)

    except (OSError, gzip.BadGzipFile, EOFError) as e:
        print(f"Error parsing xcactivitylog {log_path}: {e}", file=sys.stderr)
        # Don't cache parse failures — a transient read error on a still-
        # finalizing file shouldn't poison subsequent lookups.
        return warnings, compiled_files

    if cache_key is not None:
        with _XCACTIVITYLOG_CACHE_LOCK:
            _XCACTIVITYLOG_CACHE[cache_key] = (warnings, compiled_files)
            _XCACTIVITYLOG_CACHE.move_to_end(cache_key)
            while len(_XCACTIVITYLOG_CACHE) > _XCACTIVITYLOG_CACHE_MAX:
                _XCACTIVITYLOG_CACHE.popitem(last=False)

    # Return shallow copies so callers can attach per-aggregation fields
    # (build_uuid, build_time) without mutating cached values.
    return [dict(w) for w in warnings], set(compiled_files)


def aggregate_warnings_since_clean(
    manifest_path: str,
    logs_dir: str,
    scheme_name: Optional[str] = None,
) -> Dict:
    """
    Aggregate warnings from all Build actions since the last clean.

    Always restricts the analyzed manifest entries to those whose title
    starts with "Build " — Test, Archive, Analyze, Profile, and IndexBuild
    entries are excluded even when no scheme filter is supplied. Their
    compile records would otherwise either drop legitimate Build warnings
    (IndexBuild constantly recompiles files in the background) or leave
    behind stale warnings for test-target-only files that Build never
    recompiles.

    Args:
        manifest_path: Path to LogStoreManifest.plist
        logs_dir: Path to the Logs/Build directory containing .xcactivitylog files
        scheme_name: When provided, additionally restrict both Clean
            detection and Build analysis to entries whose
            schemeIdentifier-schemeName matches. A Clean recorded for a
            different scheme does not reset our scheme's history.

    Returns:
        Dictionary with:
        - summary: Build counts, clean info, warning counts
        - aggregated_warnings: List of warnings (excluding recompiled files)
        - recompiled_files: List of files that were recompiled and excluded
        - builds_analyzed: List of build metadata
    """
    # Parse manifest. Surface "exists but corrupted/unreadable" distinctly
    # from "no prior builds" so the caller can label warnings correctly.
    try:
        builds = parse_manifest_plist(manifest_path)
    except ManifestParseError as e:
        return {
            'summary': {
                'total_builds': 0,
                'error': f'Manifest exists but could not be parsed: {e}',
                'manifest_unreadable': True,
            }
        }

    if not builds:
        return {
            'summary': {
                'total_builds': 0,
                'error': 'No prior builds found',
            }
        }

    def _matches_scheme(b: Dict) -> bool:
        return scheme_name is None or b.get('scheme_name') == scheme_name

    # Find the most recent Clean for our scheme (or any scheme when no filter).
    # Use a prefix match instead of substring 'Clean' so a scheme whose name
    # contains "Clean" (e.g. "CleanRoom") in a Build title isn't misread as
    # a clean operation.
    last_clean_index = -1
    for i in range(len(builds) - 1, -1, -1):
        if builds[i]['title'].startswith('Clean ') and _matches_scheme(builds[i]):
            last_clean_index = i
            break

    if last_clean_index == -1:
        builds_after_clean = builds
        clean_info = (
            f"No matching clean operation found (scheme: {scheme_name or 'any'}) - "
            f"analyzing all matching builds"
        )
    else:
        builds_after_clean = builds[last_clean_index + 1:]
        clean_info = f"Found clean at index {last_clean_index}: {builds[last_clean_index]['title']}"

    # Restrict to Build entries only (excludes Test/Archive/Analyze/Profile/
    # IndexBuild), and additionally restrict by scheme when a filter is set.
    builds_to_analyze = [
        b for b in builds_after_clean
        if b['title'].startswith('Build ') and _matches_scheme(b)
    ]

    print(
        f"Analyzing {len(builds_to_analyze)} builds since last clean "
        f"(scheme filter: {scheme_name or 'any'})",
        file=sys.stderr,
    )

    # Parse each build's xcactivitylog file
    all_warnings = []
    file_last_compiled: Dict[str, float] = {}
    builds_analyzed = []

    for build in builds_to_analyze:
        log_file = os.path.join(logs_dir, build['fileName'])

        if not os.path.exists(log_file):
            print(f"Warning: Log file not found: {log_file}", file=sys.stderr)
            continue

        warnings, compiled_files = parse_xcactivitylog(log_file)

        # Track the latest build time each file was compiled in
        build_time = build['timeStartedRecording']
        for f in compiled_files:
            if f not in file_last_compiled or build_time > file_last_compiled[f]:
                file_last_compiled[f] = build_time

        # Store warnings with build context
        for warning in warnings:
            warning['build_uuid'] = build['uuid']
            warning['build_time'] = build_time
            all_warnings.append(warning)

        builds_analyzed.append({
            'uuid': build['uuid'],
            'title': build['title'],
            'time': build_time,
            'warnings_found': len(warnings),
            'files_compiled': len(compiled_files)
        })

    # Now filter warnings: keep only the LATEST warning for each file
    # Strategy: For each file, keep only warnings from the most recent build that touched that file

    # Group warnings by file
    warnings_by_file = {}
    for warning in all_warnings:
        file_path = warning['file']
        if file_path not in warnings_by_file:
            warnings_by_file[file_path] = []
        warnings_by_file[file_path].append(warning)

    # For each file, keep only warnings from the most recent build
    aggregated_warnings = []
    files_with_multiple_builds = []

    for file_path, file_warnings in warnings_by_file.items():
        # Sort by build time (descending) to get most recent first
        file_warnings.sort(key=lambda x: x['build_time'], reverse=True)

        # Get the most recent build time for this file's warnings
        most_recent_warning_time = file_warnings[0]['build_time']

        # If this file was recompiled in a later build without warnings, the
        # old warnings are stale — the file now compiles cleanly.
        last_compiled = file_last_compiled.get(file_path)
        if last_compiled is not None and last_compiled > most_recent_warning_time:
            continue

        # Keep only warnings from the most recent build
        recent_warnings = [w for w in file_warnings if w['build_time'] == most_recent_warning_time]

        # Track if file had warnings in multiple builds
        unique_build_times = set(w['build_time'] for w in file_warnings)
        if len(unique_build_times) > 1:
            files_with_multiple_builds.append({
                'file': file_path,
                'builds': len(unique_build_times),
                'warnings_excluded': len(file_warnings) - len(recent_warnings)
            })

        aggregated_warnings.extend(recent_warnings)

    # Sort final warnings by file, then line number
    aggregated_warnings.sort(key=lambda x: (x['file'], x['line']))

    # Build summary
    summary = {
        'total_builds': len(builds),
        'builds_since_clean': len(builds_to_analyze),
        'builds_analyzed': len(builds_analyzed),
        'clean_info': clean_info,
        'total_warnings': len(aggregated_warnings),
        'warnings_by_type': {
            'warnings': len([w for w in aggregated_warnings if w['type'] == 'warning']),
            'errors': len([w for w in aggregated_warnings if w['type'] == 'error'])
        },
        'unique_files_with_warnings': len(warnings_by_file),
        'files_recompiled_multiple_times': len(files_with_multiple_builds)
    }

    # Format aggregated warnings for output (remove build context)
    formatted_warnings = []
    for warning in aggregated_warnings:
        formatted_warnings.append({
            'file': warning['file'],
            'line': warning['line'],
            'column': warning['column'],
            'message': warning['message'],
            'type': warning['type']
        })

    return {
        'summary': summary,
        'aggregated_warnings': formatted_warnings,
        'files_with_multiple_builds': files_with_multiple_builds,
        'builds_analyzed': builds_analyzed
    }


def snapshot_build_uuids(manifest_path: str) -> Set[str]:
    """
    Return the set of uniqueIdentifier values currently in the manifest.

    Used to detect which manifest entries already existed before kicking off
    a build, so we can spot the new entry our build creates.

    Returns an empty set if the manifest doesn't exist (first-ever build) or
    can't be parsed — callers treat that as "no snapshot, accept any new entry".
    """
    if not os.path.exists(manifest_path):
        return set()
    try:
        with open(manifest_path, 'rb') as f:
            plist_data = plistlib.load(f)
        return set(plist_data.get('logs', {}).keys())
    except Exception as e:
        print(f"Warning: failed to snapshot manifest {manifest_path}: {e}", file=sys.stderr)
        return set()


def wait_for_new_build_uuid(
    manifest_path: str,
    before_uuids: Set[str],
    unix_start_time: float,
    timeout_seconds: float = 10.0,
    poll_interval: float = 0.25,
    settle_seconds: float = 1.0,
    scheme_name: Optional[str] = None,
) -> Optional[str]:
    """
    Poll the manifest until a new Build entry appears, then wait for the
    matching-entry set to stop growing for `settle_seconds` before returning.

    Returning on the *first* new entry races against Xcode in two ways:
      1. A scheme that builds dependency modules can produce multiple Build
         manifest entries for a single user-triggered build action. The
         dependency entries finalize before the main target's; returning on
         the first means aggregation runs before the main target's
         xcactivitylog is in the manifest, and we read the previous main
         target's stale log instead.
      2. Xcode may finalize manifest writes in batches; a slightly slower
         entry could be missed by an eager return.

    The settle period mitigates both: we wait until no new matching entry
    has appeared for `settle_seconds`, then return the UUID with the
    latest timeStartedRecording.

    Filters applied to each candidate entry:
      - uniqueIdentifier not in before_uuids (must be new since snapshot)
      - title.startswith('Build ') (excludes Clean/Test/Archive/Analyze/IndexBuild)
      - timeStartedRecording at or after our snapshot (rejects stray older entries)
      - schemeIdentifier-schemeName matches scheme_name when provided

    Args:
        manifest_path: Path to LogStoreManifest.plist
        before_uuids: Set returned by snapshot_build_uuids() before the build started
        unix_start_time: time.time() captured before AppleScript was invoked
        timeout_seconds: Maximum wall-clock time to wait
        poll_interval: Sleep between polls
        settle_seconds: Required quiet period (no new matching entries) after
            at least one matching entry has been seen. Set to 0 to disable
            and return on first match.
        scheme_name: When provided, restrict matches to entries whose
            schemeIdentifier-schemeName equals this value.

    Returns:
        The uniqueIdentifier of the matching entry with the latest
        timeStartedRecording, or None on timeout.
    """
    # Filesystem mtimes and CFAbsoluteTime conversion both have sub-second
    # granularity — allow a small slack so a manifest entry written in the
    # same wall-clock second as our start isn't rejected as "too old".
    timestamp_slack_seconds = 1.0
    effective_start_cf = unix_start_time - CF_EPOCH_OFFSET - timestamp_slack_seconds

    deadline = time.time() + timeout_seconds
    last_seen_count = 0
    last_change_time: Optional[float] = None

    while True:
        matching: List[Tuple[float, str]] = []
        try:
            if os.path.exists(manifest_path):
                with open(manifest_path, 'rb') as f:
                    plist_data = plistlib.load(f)

                for log_uuid, entry in plist_data.get('logs', {}).items():
                    if log_uuid in before_uuids:
                        continue
                    if not isinstance(entry, dict):
                        continue

                    title = entry.get('title', '')
                    if not title.startswith('Build '):
                        continue

                    if scheme_name is not None:
                        if entry.get('schemeIdentifier-schemeName') != scheme_name:
                            continue

                    started = entry.get('timeStartedRecording', 0)
                    if not isinstance(started, (int, float)):
                        continue
                    if started < effective_start_cf:
                        continue

                    matching.append((started, log_uuid))
        except Exception as e:
            print(f"Warning: error polling manifest {manifest_path}: {e}", file=sys.stderr)
            matching = []

        now = time.time()
        current_count = len(matching)

        if current_count != last_seen_count:
            last_seen_count = current_count
            last_change_time = now

        # Settle: return latest match once at least one exists AND the
        # matching set hasn't grown for settle_seconds.
        if (
            current_count > 0
            and last_change_time is not None
            and (now - last_change_time) >= settle_seconds
        ):
            matching.sort(key=lambda x: x[0], reverse=True)
            return matching[0][1]

        if now >= deadline:
            return None
        time.sleep(poll_interval)


def get_scheme_name_for_uuid(manifest_path: str, target_uuid: str) -> Optional[str]:
    """
    Return the schemeIdentifier-schemeName for a single manifest entry, or
    None if the entry isn't present or doesn't carry a usable scheme name.

    Used by callers that have already identified "our" build's UUID via
    wait_for_new_build_uuid and need its scheme name for downstream
    aggregation filtering.
    """
    if not os.path.exists(manifest_path):
        return None
    try:
        with open(manifest_path, 'rb') as f:
            plist_data = plistlib.load(f)
    except Exception as e:
        print(f"Warning: failed to read manifest {manifest_path}: {e}", file=sys.stderr)
        return None

    entry = plist_data.get('logs', {}).get(target_uuid)
    if not isinstance(entry, dict):
        return None
    scheme = entry.get('schemeIdentifier-schemeName')
    if isinstance(scheme, str) and scheme:
        return scheme
    return None


def derived_data_matches_project(derived_data_path: str, project_realpath: str) -> Optional[bool]:
    """
    Decide whether a DerivedData directory belongs to a specific project.

    DerivedData directory names are `{ProjectName}-{hash}`, so two distinct
    projects that share a name (in different directories) both match the same
    name prefix. Each DerivedData directory records the real project it was
    built from in `info.plist` under `WorkspacePath`; comparing that against the
    project path disambiguates them.

    Args:
        derived_data_path: Absolute path to a `{ProjectName}-{hash}` directory.
        project_realpath: `os.path.realpath` of the .xcodeproj/.xcworkspace.

    Returns:
        True if info.plist's WorkspacePath identifies this project, False if it
        identifies a different one, or None if info.plist is missing/unreadable
        (so callers can fall back to name-prefix matching instead of discarding
        the candidate).
    """
    info_plist = os.path.join(derived_data_path, "info.plist")
    if not os.path.exists(info_plist):
        return None
    try:
        with open(info_plist, 'rb') as f:
            info = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException) as e:
        print(f"Warning: failed to read {info_plist}: {e}", file=sys.stderr)
        return None

    workspace_path = info.get('WorkspacePath')
    if not isinstance(workspace_path, str) or not workspace_path:
        return None

    wp = os.path.realpath(workspace_path)
    if wp == project_realpath:
        return True
    # A bare .xcodeproj is backed by an implicit workspace at
    # <proj>.xcodeproj/project.xcworkspace, so accept that exact inner/outer
    # relationship in either direction. (Earlier this used arbitrary-depth
    # startswith containment, which could mis-confirm an unrelated same-named
    # project that merely happened to be nested in the tree.)
    if wp == os.path.join(project_realpath, "project.xcworkspace"):
        return True
    if project_realpath == os.path.join(wp, "project.xcworkspace"):
        return True
    return False


def select_derived_data_dirs_for_project(
    derived_dirs: List[Tuple[float, str]], project_realpath: str
) -> List[Tuple[float, str]]:
    """
    Filter name-prefix DerivedData candidates down to those that info.plist
    confirms belong to `project_realpath`.

    If at least one candidate is positively confirmed, only confirmed ones are
    returned. Otherwise the fallback is restricted to candidates whose
    ownership is UNKNOWN (info.plist missing/unreadable, matcher returns None)
    — never to candidates info.plist proves belong to a DIFFERENT same-named
    project (matcher returns False). Including proven mismatches in the fallback
    would defeat the point of the check: a same-named but unrelated project's
    build logs/test results could be returned. When every candidate is a proven
    mismatch the result is empty — there is genuinely no DerivedData for this
    project, and returning nothing is correct (finding none beats returning
    another project's results).

    Args:
        derived_dirs: List of (sort_key, derived_data_path) candidates.
        project_realpath: realpath of the project being looked up.
    """
    confirmed = []
    unknown = []
    for key, path in derived_dirs:
        match = derived_data_matches_project(path, project_realpath)
        if match is True:
            confirmed.append((key, path))
        elif match is None:
            unknown.append((key, path))
    return confirmed if confirmed else unknown


def find_derived_data_for_project(project_path: str) -> Optional[str]:
    """
    Find the DerivedData directory for a given project.

    Xcode regenerates the random-hash suffix when a project moves on disk or
    its Xcode version changes, so multiple matching directories can exist for
    the same project name. Candidates are first disambiguated by info.plist's
    WorkspacePath (so a different project that happens to share a name can't be
    selected); among the remaining matches, pick the one whose
    `Logs/Build/LogStoreManifest.plist` has the most recent mtime (falling
    back to the directory's own mtime when no manifest is present). Picking
    the wrong DerivedData yields the wrong build logs.

    Args:
        project_path: Path to .xcodeproj or .xcworkspace

    Returns:
        Path to the project's DerivedData directory, or None if not found.
    """
    normalized_path = os.path.realpath(project_path)
    project_name = os.path.basename(normalized_path).replace('.xcworkspace', '').replace('.xcodeproj', '')

    derived_data_base = os.path.expanduser("~/Library/Developer/Xcode/DerivedData")

    if not os.path.exists(derived_data_base):
        return None

    try:
        entries = os.listdir(derived_data_base)
    except OSError as e:
        print(f"Error listing DerivedData base {derived_data_base}: {e}", file=sys.stderr)
        return None

    candidates = []
    for derived_dir in entries:
        if not derived_dir.startswith(project_name + "-"):
            continue
        derived_data_path = os.path.join(derived_data_base, derived_dir)
        if not os.path.isdir(derived_data_path):
            continue

        manifest_path = os.path.join(
            derived_data_path, "Logs", "Build", "LogStoreManifest.plist"
        )
        try:
            if os.path.exists(manifest_path):
                mtime = os.path.getmtime(manifest_path)
            else:
                mtime = os.path.getmtime(derived_data_path)
        except OSError:
            mtime = 0.0
        candidates.append((mtime, derived_data_path))

    if not candidates:
        return None

    candidates = select_derived_data_dirs_for_project(candidates, normalized_path)
    # select_... can now return [] when every name-prefix candidate is a proven
    # mismatch (a different same-named project); there is no DerivedData for this
    # project in that case.
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]
