#!/usr/bin/env python3
"""
Regression tests for xcactivitylog parsing.

Two coverage gaps caused persistent stale warnings in build_project output:

1. The path regex required [a-zA-Z0-9_/.-] only, silently dropping any project
   path containing a space (very common: "/Users/x/My Project/..."). Files
   missing from compiled_files can never be cleared from the stale-warning set,
   so old warnings linger across incremental builds until the user does a clean.

2. Only SwiftCompile lines and .swift warnings/errors were matched. Mixed-language
   projects (ObjC/C/C++/Metal) were invisible to the parser, so their warnings
   never appeared in aggregation and recompiles never cleared anything.
"""

import gzip
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import drews_xcode_mcp.utils.build_log_parser as build_log_parser
from drews_xcode_mcp.utils.build_log_parser import (
    parse_xcactivitylog,
    select_derived_data_dirs_for_project,
)


def _write_gzipped_log(path: str, text: str) -> None:
    """xcactivitylog files are gzipped — write a fake one."""
    with gzip.open(path, 'wb') as f:
        f.write(text.encode('utf-8'))


class SelectDerivedDataDirsTests(unittest.TestCase):
    """The name-prefix candidate list must collapse to confirmed dirs, and when
    none are confirmed fall back ONLY to dirs of unknown ownership — never to
    ones info.plist proved belong to a different same-named project."""

    def setUp(self):
        self._orig = build_log_parser.derived_data_matches_project

    def tearDown(self):
        build_log_parser.derived_data_matches_project = self._orig

    def _patch(self, verdicts):
        build_log_parser.derived_data_matches_project = (
            lambda path, project_realpath: verdicts[path]
        )

    def test_confirmed_only_when_any_confirmed(self):
        self._patch({'/c1': True, '/c2': True, '/mm': False, '/uk': None})
        dirs = [(0.0, '/c1'), (0.0, '/c2'), (0.0, '/mm'), (0.0, '/uk')]
        self.assertEqual(
            select_derived_data_dirs_for_project(dirs, '/p'),
            [(0.0, '/c1'), (0.0, '/c2')],
        )

    def test_falls_back_to_unknown_excluding_mismatch(self):
        self._patch({'/mm': False, '/uk': None})
        dirs = [(0.0, '/mm'), (0.0, '/uk')]
        self.assertEqual(
            select_derived_data_dirs_for_project(dirs, '/p'),
            [(0.0, '/uk')],
        )

    def test_all_mismatch_returns_empty(self):
        self._patch({'/mm1': False, '/mm2': False})
        dirs = [(0.0, '/mm1'), (0.0, '/mm2')]
        self.assertEqual(select_derived_data_dirs_for_project(dirs, '/p'), [])

    def test_find_derived_data_returns_none_when_all_mismatch(self):
        """find_derived_data_for_project must return None (not IndexError) when
        the selector filters every candidate out as a proven mismatch."""
        from unittest import mock
        with tempfile.TemporaryDirectory() as base:
            os.makedirs(os.path.join(base, "MyApp-abc123"))
            # Every name-prefix dir is a proven mismatch -> selector returns [].
            build_log_parser.derived_data_matches_project = (
                lambda path, project_realpath: False
            )
            with mock.patch.object(
                build_log_parser.os.path, "expanduser", return_value=base
            ):
                result = build_log_parser.find_derived_data_for_project(
                    "/somewhere/MyApp.xcodeproj"
                )
        self.assertIsNone(result)


class PathsWithSpacesTests(unittest.TestCase):
    def test_swift_file_with_space_in_path_is_tracked_as_compiled(self):
        """A path with a space must be recognized in SwiftCompile lines."""
        log_text = (
            "Some preamble\r"
            "SwiftCompile normal arm64 /Users/test/My Project/Sources/Foo.swift "
            "(in target 'App' from project 'App')\r"
            "more output\r"
        )
        with tempfile.NamedTemporaryFile(suffix='.xcactivitylog', delete=False) as tmp:
            tmp_path = tmp.name
        try:
            _write_gzipped_log(tmp_path, log_text)
            warnings, compiled = parse_xcactivitylog(tmp_path)
            self.assertIn('/Users/test/My Project/Sources/Foo.swift', compiled,
                          f"compiled files were: {compiled}")
        finally:
            os.unlink(tmp_path)

    def test_warning_on_path_with_space_is_extracted(self):
        log_text = (
            "/Users/test/My Project/Sources/Foo.swift:42:5: warning: "
            "variable 'x' was never used\r"
        )
        with tempfile.NamedTemporaryFile(suffix='.xcactivitylog', delete=False) as tmp:
            tmp_path = tmp.name
        try:
            _write_gzipped_log(tmp_path, log_text)
            warnings, _ = parse_xcactivitylog(tmp_path)
            files = [w['file'] for w in warnings]
            self.assertIn('/Users/test/My Project/Sources/Foo.swift', files,
                          f"warnings were: {warnings}")
        finally:
            os.unlink(tmp_path)


class MixedLanguageTests(unittest.TestCase):
    def test_objc_compilec_line_tracks_source_file(self):
        """CompileC <obj> <source> — we want the source path tracked."""
        log_text = (
            "CompileC /tmp/Build/Foo.o /Users/test/MyProj/Foo.m "
            "normal arm64 objective-c\r"
        )
        with tempfile.NamedTemporaryFile(suffix='.xcactivitylog', delete=False) as tmp:
            tmp_path = tmp.name
        try:
            _write_gzipped_log(tmp_path, log_text)
            _, compiled = parse_xcactivitylog(tmp_path)
            self.assertIn('/Users/test/MyProj/Foo.m', compiled,
                          f"compiled files were: {compiled}")
        finally:
            os.unlink(tmp_path)

    def test_objc_compilec_with_space_in_object_path_tracks_source(self):
        """The object-file path (under DerivedData) may contain a space; the
        source file must still be tracked rather than dropped."""
        log_text = (
            "CompileC /Users/test/My Project/Build/Foo.o "
            "/Users/test/My Project/Foo.m normal arm64 objective-c\r"
        )
        with tempfile.NamedTemporaryFile(suffix='.xcactivitylog', delete=False) as tmp:
            tmp_path = tmp.name
        try:
            _write_gzipped_log(tmp_path, log_text)
            _, compiled = parse_xcactivitylog(tmp_path)
            self.assertIn('/Users/test/My Project/Foo.m', compiled,
                          f"compiled files were: {compiled}")
        finally:
            os.unlink(tmp_path)

    def test_objc_warning_is_extracted(self):
        log_text = (
            "/Users/test/MyProj/Foo.m:10:3: warning: "
            "'someAPI' is deprecated\r"
        )
        with tempfile.NamedTemporaryFile(suffix='.xcactivitylog', delete=False) as tmp:
            tmp_path = tmp.name
        try:
            _write_gzipped_log(tmp_path, log_text)
            warnings, _ = parse_xcactivitylog(tmp_path)
            self.assertTrue(
                any(w['file'] == '/Users/test/MyProj/Foo.m' for w in warnings),
                f"warnings were: {warnings}"
            )
        finally:
            os.unlink(tmp_path)


class ExistingBehaviorPreservedTests(unittest.TestCase):
    def test_plain_swift_path_still_works(self):
        log_text = (
            "SwiftCompile normal arm64 /Users/test/Proj/Sources/Foo.swift "
            "(in target 'App' from project 'App')\r"
            "/Users/test/Proj/Sources/Foo.swift:99:1: warning: shadows\r"
        )
        with tempfile.NamedTemporaryFile(suffix='.xcactivitylog', delete=False) as tmp:
            tmp_path = tmp.name
        try:
            _write_gzipped_log(tmp_path, log_text)
            warnings, compiled = parse_xcactivitylog(tmp_path)
            self.assertIn('/Users/test/Proj/Sources/Foo.swift', compiled)
            self.assertEqual(len(warnings), 1)
            self.assertEqual(warnings[0]['line'], 99)
            self.assertEqual(warnings[0]['column'], 1)
            self.assertEqual(warnings[0]['type'], 'warning')
        finally:
            os.unlink(tmp_path)

    def test_error_extracted_with_type_error(self):
        log_text = "/Users/test/Proj/Sources/Foo.swift:5:1: error: bad thing\r"
        with tempfile.NamedTemporaryFile(suffix='.xcactivitylog', delete=False) as tmp:
            tmp_path = tmp.name
        try:
            _write_gzipped_log(tmp_path, log_text)
            warnings, _ = parse_xcactivitylog(tmp_path)
            self.assertEqual(len(warnings), 1)
            self.assertEqual(warnings[0]['type'], 'error')
        finally:
            os.unlink(tmp_path)


def _slf0_string(text: str) -> bytes:
    payload = text.encode('utf-8')
    return str(len(payload)).encode() + b'"' + payload


def _slf0_bytes(*tokens: bytes) -> bytes:
    return b'SLF0' + b''.join(tokens)


def _parse_slf0(body: bytes):
    """Write an SLF0 stream to a gzipped temp log and parse it."""
    with tempfile.NamedTemporaryFile(suffix='.xcactivitylog', delete=False) as tmp:
        tmp_path = tmp.name
    try:
        with gzip.open(tmp_path, 'wb') as f:
            f.write(body)
        return parse_xcactivitylog(tmp_path)
    finally:
        os.unlink(tmp_path)


class SLF0DecodingTests(unittest.TestCase):
    """The regression that motivated token-level decoding: scanning the raw
    SLF0 stream let a diagnostic's path capture backtrack across token
    boundaries, absorbing kilobytes of binary structure between an unrelated
    compile-record path and the real warning path."""

    WARNING = ('/Users/test/Proj/Cells/MHMFORMTextSelectProxyCell.swift:371:27: '
               'warning: will never be executed\n            completion(false)\n'
               '                          ^')

    def test_path_cannot_absorb_adjacent_tokens(self):
        """Reconstruction of the observed corruption: a compile-record string,
        then int/classref/double/JSON/classname tokens, then the diagnostic.
        The old blob scan produced a `file` spanning all of it."""
        body = _slf0_bytes(
            _slf0_string("/Users/test/Proj/ChoicePopoverViewController.swift "
                         "(in target 'App' from project 'App')"),
            b'1(', b'4@',
            b'53%com.apple.dt.ActivityLogSectionAttachment.TaskMetrics',
            b'1#', b'0#',
            b'99*' + b'{"stime":55016,"wcDuration":353006,"wcStartTime":809981675620911,"maxRSS":219430912,"utime":21747}',
            b'9984cf75ab23c841^', b'-',
            _slf0_string(self.WARNING),
        )
        warnings, _ = _parse_slf0(body)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertEqual(
            warnings[0]['file'],
            '/Users/test/Proj/Cells/MHMFORMTextSelectProxyCell.swift',
        )
        self.assertEqual(warnings[0]['line'], 371)
        self.assertEqual(warnings[0]['column'], 27)
        self.assertEqual(warnings[0]['message'], 'will never be executed')

    def test_duplicate_diagnostic_across_strings_counted_once(self):
        """SLF0 repeats a diagnostic in the section text and per-message
        strings; identical copies must not inflate warning totals."""
        body = _slf0_bytes(_slf0_string(self.WARNING), b'2#',
                           _slf0_string(self.WARNING))
        warnings, _ = _parse_slf0(body)
        self.assertEqual(len(warnings), 1)

    def test_compile_record_extracted_from_string_token(self):
        body = _slf0_bytes(_slf0_string(
            "SwiftCompile normal arm64 /Users/test/My Proj/Foo.swift "
            "(in target 'App' from project 'App')"))
        _, compiled = _parse_slf0(body)
        self.assertEqual(compiled, {'/Users/test/My Proj/Foo.swift'})

    def test_string_payload_containing_token_lookalikes(self):
        """Payload bytes are length-bounded, so text that resembles SLF0
        tokens inside a string must not desynchronize the decoder."""
        body = _slf0_bytes(
            _slf0_string('log mentions 12#tokens and 5"quoted lengths'),
            _slf0_string(self.WARNING),
        )
        warnings, _ = _parse_slf0(body)
        self.assertEqual(len(warnings), 1)

    def test_non_utf8_byte_in_payload_survives(self):
        """A stray non-UTF-8 byte in one string must not lose the diagnostic
        in a later string, and output must be valid UTF-8."""
        raw = str(len(self.WARNING.encode()) + 1).encode() + b'"\xff' + self.WARNING.encode()
        body = _slf0_bytes(raw)
        warnings, _ = _parse_slf0(body)
        self.assertEqual(len(warnings), 1)
        warnings[0]['message'].encode('utf-8')

    def test_empty_string_token(self):
        body = _slf0_bytes(b'0"', _slf0_string(self.WARNING))
        warnings, _ = _parse_slf0(body)
        self.assertEqual(len(warnings), 1)

    def test_truncated_mid_payload_keeps_flushed_diagnostic(self):
        """Xcode may still be writing: a final string whose declared length
        exceeds the bytes present is scanned as far as it goes."""
        complete = _slf0_string(self.WARNING)
        body = _slf0_bytes(complete + b'500"only the beginning was written')
        warnings, _ = _parse_slf0(body)
        self.assertEqual(len(warnings), 1)

    def test_truncated_mid_prefix_returns_cleanly(self):
        body = _slf0_bytes(_slf0_string(self.WARNING), b'12')
        warnings, _ = _parse_slf0(body)
        self.assertEqual(len(warnings), 1)

    def test_unknown_token_type_stops_without_crashing(self):
        body = _slf0_bytes(_slf0_string(self.WARNING), b'3!xyz',
                           _slf0_string(self.WARNING.replace(':371:', ':999:')))
        warnings, _ = _parse_slf0(body)
        # Tokens before the unknown type are kept; scanning stops at it.
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]['line'], 371)

    def test_non_decimal_length_stops_without_crashing(self):
        body = _slf0_bytes(_slf0_string(self.WARNING), b'ff"AB')
        warnings, _ = _parse_slf0(body)
        self.assertEqual(len(warnings), 1)

    def test_magicless_content_scanned_as_plain_text(self):
        """The plain-text fallback (also exercised by the older tests above)
        must keep working for content without the SLF0 magic."""
        with tempfile.NamedTemporaryFile(suffix='.xcactivitylog', delete=False) as tmp:
            tmp_path = tmp.name
        try:
            _write_gzipped_log(tmp_path, self.WARNING + '\r')
            warnings, _ = parse_xcactivitylog(tmp_path)
            self.assertEqual(len(warnings), 1)
        finally:
            os.unlink(tmp_path)


if __name__ == '__main__':
    unittest.main(verbosity=2)
