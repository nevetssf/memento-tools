#!/usr/bin/env python3
"""
Tests for weather_query.py

Regression cover for the "weather in Quebec" bug: wttr.in reads a trailing "CA"
as Canada, so "San Francisco, CA" geocoded to Charlesbourg, Quebec.

Run with: python3 -m unittest tests.test_weather_query -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from weather_query import qualify_location, wttr_location


class TestQualifyLocation(unittest.TestCase):
    def test_us_state_code_gets_country(self):
        self.assertEqual(qualify_location("San Francisco, CA"), "San Francisco, CA, USA")
        self.assertEqual(qualify_location("Boulder, CO"), "Boulder, CO, USA")
        self.assertEqual(qualify_location("Washington, DC"), "Washington, DC, USA")

    def test_already_qualified_is_unchanged(self):
        self.assertEqual(qualify_location("San Francisco, CA, USA"), "San Francisco, CA, USA")
        self.assertEqual(qualify_location("Boulder, CO, US"), "Boulder, CO, US")

    def test_non_us_locations_untouched(self):
        self.assertEqual(qualify_location("Thessaloniki, Greece"), "Thessaloniki, Greece")
        self.assertEqual(qualify_location("Billund, Denmark"), "Billund, Denmark")

    def test_no_comma_is_untouched(self):
        # A bare city has nothing to disambiguate; a bare "CA" is too ambiguous to guess.
        self.assertEqual(qualify_location("Boulder"), "Boulder")
        self.assertEqual(qualify_location("CA"), "CA")

    def test_case_insensitive_and_whitespace(self):
        self.assertEqual(qualify_location("boulder, co"), "boulder, co, USA")
        self.assertEqual(qualify_location("New York, NY "), "New York, NY, USA")

    def test_empty_input(self):
        self.assertEqual(qualify_location(""), "")
        self.assertEqual(qualify_location(None), None)

    def test_non_state_two_letter_token_untouched(self):
        # "BC" is a Canadian province, not a US state - leave it alone.
        self.assertEqual(qualify_location("Vancouver, BC"), "Vancouver, BC")


class TestWttrLocation(unittest.TestCase):
    def test_spaces_become_plus(self):
        self.assertEqual(wttr_location("San Francisco, CA"), "San+Francisco,+CA,+USA")
        self.assertEqual(wttr_location("Thessaloniki, Greece"), "Thessaloniki,+Greece")

    def test_comma_is_preserved(self):
        # Dropping the comma made "Boulder, CO" resolve to Springdale, CO (commit 9d215ff).
        self.assertIn(",", wttr_location("Boulder, CO"))


if __name__ == "__main__":
    unittest.main()
