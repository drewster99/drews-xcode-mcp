#!/usr/bin/env python3
"""
Tests for the output-trimming applied by list_run_destinations: the optional
platform filter and the omission of architectures that distinguish nothing.

Fixture lines are real `xcodebuild -showdestinations` output shapes, run
through parse_destination_line so the tests see what the tool sees.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from drews_xcode_mcp.exceptions import InvalidParameterError
from drews_xcode_mcp.utils.xcodebuild_query import (
    omit_non_distinguishing_architectures,
    parse_destination_line,
    select_destinations_for_platform,
)

MAC_ID = '00006040-000270260EA0801C'
WATCH_ID = '00008310-001179AC1E10E01E'
SIMULATOR_ID = 'CEC7F357-5D50-4100-9EDE-9A65A87EA1E1'


def _parse(*lines):
    return [parse_destination_line(line) for line in lines]


class SelectDestinationsForPlatformTests(unittest.TestCase):

    def setUp(self):
        self.destinations = _parse(
            f'{{ platform:macOS, arch:arm64, id:{MAC_ID}, name:My Mac }}',
            f'{{ platform:iOS Simulator, arch:arm64, id:{SIMULATOR_ID}, OS:27.2, name:iPhone 17 Pro }}',
            '{ platform:iOS Simulator, arch:arm64, id:AD9A22AF-4095-49BF-A4A1-11BCAD301E46, OS:27.2, name:iPhone Air }',
        )

    def test_returns_only_destinations_of_the_requested_platform(self):
        # Protects: platform filter returns every match and nothing else.
        # Fails if: the comparison becomes a prefix/substring match ("iOS" vs "iOS Simulator") or stops after one match.
        selected = select_destinations_for_platform(self.destinations, 'iOS Simulator')
        self.assertEqual([d['name'] for d in selected], ['iPhone 17 Pro', 'iPhone Air'])

    def test_platform_that_is_a_prefix_of_a_reported_platform_is_rejected(self):
        # Protects: "iOS" (physical devices) never matches "iOS Simulator" entries.
        # Fails if: matching becomes startswith/substring.
        with self.assertRaises(InvalidParameterError):
            select_destinations_for_platform(self.destinations, 'iOS')

    def test_unreported_platform_raises_naming_the_reported_platforms(self):
        # Protects: a misspelled or mis-cased platform is reported, not answered with an empty list.
        # Fails if: the filter returns [] silently, or the message omits the valid names.
        with self.assertRaises(InvalidParameterError) as raised:
            select_destinations_for_platform(self.destinations, 'ios simulator')
        self.assertIn('iOS Simulator', str(raised.exception))
        self.assertIn('macOS', str(raised.exception))

    def test_no_destinations_at_all_raises_saying_none_reported(self):
        # Protects: the empty-input edge case still raises with a coherent message.
        # Fails if: an empty platform list renders as a blank or crashes.
        with self.assertRaises(InvalidParameterError) as raised:
            select_destinations_for_platform([], 'macOS')
        self.assertIn('none', str(raised.exception))


class OmitNonDistinguishingArchitecturesTests(unittest.TestCase):

    def test_arch_is_dropped_when_each_id_has_one_architecture(self):
        # Protects: the common case loses 'arch', including mixed arm64/arm64e across different devices.
        # Fails if: the rule compares architectures across the whole list instead of per id.
        trimmed = omit_non_distinguishing_architectures(_parse(
            f'{{ platform:macOS, arch:arm64, id:{MAC_ID}, name:My Mac }}',
            f'{{ platform:watchOS, arch:arm64e, id:{WATCH_ID}, name:Watch }}',
        ))
        self.assertTrue(all('arch' not in d for d in trimmed))

    def test_arch_is_kept_when_it_distinguishes_entries_sharing_an_id(self):
        # Protects: a device listed under several architectures stays distinguishable.
        # Fails if: arch is dropped unconditionally.
        trimmed = omit_non_distinguishing_architectures(_parse(
            f'{{ platform:macOS, arch:arm64, id:{MAC_ID}, name:My Mac }}',
            f'{{ platform:macOS, arch:x86_64, id:{MAC_ID}, name:My Mac }}',
            f'{{ platform:watchOS, arch:arm64e, id:{WATCH_ID}, name:Watch }}',
        ))
        self.assertEqual([d.get('arch') for d in trimmed], ['arm64', 'x86_64', None])

    def test_arch_is_dropped_when_entries_sharing_an_id_share_the_architecture(self):
        # Protects: "My Mac" and "My Mac (Designed for iPad)" share id and arch; 'variant' tells them apart, not 'arch'.
        # Fails if: the rule keeps arch whenever an id repeats.
        trimmed = omit_non_distinguishing_architectures(_parse(
            f'{{ platform:macOS, arch:arm64, id:{MAC_ID}, name:My Mac }}',
            f'{{ platform:macOS, arch:arm64, variant:Designed for iPad, id:{MAC_ID}, name:My Mac }}',
        ))
        self.assertTrue(all('arch' not in d for d in trimmed))
        self.assertEqual(trimmed[1]['variant'], 'Designed for iPad')

    def test_arch_is_kept_when_another_entry_with_the_id_has_no_arch(self):
        # Protects: an entry with an arch and one without, same id, remain distinguishable.
        # Fails if: missing arch is skipped when collecting the architectures for an id.
        trimmed = omit_non_distinguishing_architectures(_parse(
            f'{{ platform:macOS, arch:arm64, id:{MAC_ID}, name:My Mac }}',
            f'{{ platform:macOS, id:{MAC_ID}, name:My Mac }}',
        ))
        self.assertEqual([d.get('arch') for d in trimmed], ['arm64', None])

    def test_other_fields_and_order_are_preserved_and_input_is_not_mutated(self):
        # Protects: trimming removes only 'arch'; callers' lists are untouched.
        # Fails if: the helper pops 'arch' from the caller's dicts or reorders entries.
        original = _parse(
            f'{{ platform:iOS Simulator, arch:arm64, id:{SIMULATOR_ID}, OS:27.2, name:iPhone 17 Pro }}',
            f'{{ platform:macOS, arch:arm64, id:{MAC_ID}, name:My Mac }}',
        )
        trimmed = omit_non_distinguishing_architectures(original)
        self.assertEqual(trimmed[0], {
            'platform': 'iOS Simulator', 'id': SIMULATOR_ID, 'OS': '27.2', 'name': 'iPhone 17 Pro',
        })
        self.assertEqual(trimmed[1]['name'], 'My Mac')
        self.assertEqual(original[0]['arch'], 'arm64')

    def test_empty_list_returns_empty_list(self):
        # Protects: no destinations is a valid input.
        # Fails if: the helper indexes into an empty list.
        self.assertEqual(omit_non_distinguishing_architectures([]), [])


if __name__ == '__main__':
    unittest.main()
