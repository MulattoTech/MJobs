"""Relative source dates require an explicit, stable observation reference."""
from datetime import datetime, timezone
import unittest

from services.discovery.search_semantics import (
    evaluate_search, normalize_reported_timestamp, parse_timestamp,
)


class RelativePostedTimestampTests(unittest.TestCase):
    def test_relative_units_need_explicit_utc_reference_and_keep_granularity(self):
        reference = '2026-10-18T20:00:00+00:00'
        for wording, expected, precision in (
            ('2 hours ago', '2026-10-18T18:00:00+00:00', 'HOUR'),
            ('1 day ago', '2026-10-17T20:00:00+00:00', 'DAY'),
            ('30 minutes ago', '2026-10-18T19:30:00+00:00', 'MINUTE'),
        ):
            with self.subTest(wording=wording):
                self.assertIsNone(parse_timestamp(wording))
                result = normalize_reported_timestamp(wording, reference_at=reference)
                self.assertEqual(result['reported_posted_at'], expected)
                self.assertEqual(result['reported_posted_at_raw'], wording)
                self.assertEqual(result['reported_posted_at_precision'], precision)
                self.assertEqual(result['reported_posted_at_reference_at'], reference)
                self.assertEqual(normalize_reported_timestamp(wording,
                    reference_at='2026-10-20T20:00:00Z', previous=result), result)

    def test_changed_age_label_does_not_double_age_or_refresh_the_same_posting(self):
        original = normalize_reported_timestamp('2 hours ago', reference_at='2026-10-18T20:00:00Z')
        later = normalize_reported_timestamp('3 hours ago', reference_at='2026-10-18T21:00:00Z',
                                             previous=original)
        self.assertEqual(later, original)
        self.assertEqual(later['reported_posted_at'], '2026-10-18T18:00:00+00:00')
        self.assertEqual(normalize_reported_timestamp('2 hours ago',
            reference_at='2026-10-20T20:00:00Z', previous=later), original)
        absolute = normalize_reported_timestamp('2026-10-18T18:00:00Z',
                                                reference_at='2026-10-18T20:00:00Z')
        self.assertEqual(normalize_reported_timestamp('2 hours ago',
            reference_at='2026-10-20T20:00:00Z', previous=absolute), absolute)

    def test_invalid_overflow_and_future_wording_do_not_become_posting_dates(self):
        for value in ('unknown', 'now', 'in 2 hours', '-2 hours ago', '1.5 hours ago',
                      '999999999999999999999999 days ago', '999999 days ago',
                      '2026-02-30', '0001-01-01T00:00:00+23:00'):
            with self.subTest(value=value):
                self.assertIsNone(parse_timestamp(value, reference_at='2026-10-18T20:00:00Z'))

    def test_valid_absolute_dates_are_unchanged_and_future_dates_do_not_match(self):
        self.assertEqual(parse_timestamp('2026-10-18T21:00:00+03:00'),
                         datetime(2026, 10, 18, 18, tzinfo=timezone.utc))
        job = {'attributes': {'reported_posted_at': '2026-10-19T00:00:00Z',
                              'first_seen_at': '2026-10-18T20:00:00Z'}}
        self.assertFalse(evaluate_search({'date_window': 'LAST_24_HOURS'}, job,
                                        at='2026-10-18T20:30:00Z')['matched'])

    def test_legacy_relative_claim_uses_first_observation_not_query_time(self):
        job = {'attributes': {'reported_posted_at': '2 hours ago',
                              'first_seen_at': '2026-10-18T20:00:00Z',
                              'last_seen_at': '2026-10-20T20:00:00Z'}}
        for window in ('LAST_HOUR', 'TODAY', 'LAST_24_HOURS', 'LAST_3_DAYS',
                       'LAST_7_DAYS', 'LAST_30_DAYS'):
            with self.subTest(window=window):
                self.assertEqual(evaluate_search({'date_window': window}, job,
                    at='2026-10-18T20:30:00Z')['matched'], window != 'LAST_HOUR')
        self.assertFalse(evaluate_search({'date_window': 'CUSTOM',
            'date_start': '2026-10-18T18:01:00Z', 'date_end': '2026-10-18T20:30:00Z'},
            job, at='2026-10-18T20:30:00Z')['matched'])

    def test_invalid_claim_is_unknown_instead_of_falling_back_to_recent_first_seen(self):
        for value in ('not a date', 0, False, [], {}):
            with self.subTest(value=value):
                job = {'attributes': {**normalize_reported_timestamp(value,
                    reference_at='2026-10-18T20:00:00Z'),
                    'first_seen_at': '2026-10-18T20:00:00Z'}}
                self.assertIn('DATE_UNKNOWN', evaluate_search({'date_window': 'LAST_HOUR'}, job,
                    at='2026-10-18T20:30:00Z')['reasons'])

    def test_supported_rss_dates_require_a_valid_known_timezone_and_normalize_to_utc(self):
        for raw in ('Tue, 06 Oct 2026 12:00:00 +0000', '06 Oct 2026 17:30:00 +0530',
                    'Tue, 06 Oct 2026 12:00:00 GMT', 'Tue, 06 Oct 2026 12:00 UT'):
            with self.subTest(raw=raw):
                normalized = normalize_reported_timestamp(raw, reference_at='2026-10-18T20:00:00Z')
                self.assertEqual(normalized['reported_posted_at'], '2026-10-06T12:00:00+00:00')
                self.assertEqual(normalized['reported_posted_at_raw'], raw)
                self.assertEqual(normalized['reported_posted_at_precision'], 'SOURCE')
                self.assertIsNone(normalized['reported_posted_at_reference_at'])
        for raw in ('Tue, 32 Oct 2026 12:00:00 +0000', 'Tue, 06 Oct 2026 25:00:00 +0000',
                    'Tue, 06 Oct 2026 12:00:00 +2500', 'Tue, 06 Oct 2026 12:00:00',
                    'Tue, 06 Oct 2026 12:00:00 -0000', 'Tue, 06 Oct 2026 12:00:00 UNKNOWN',
                    'not-a-date Tue, 06 Oct 2026 12:00:00 +0000'):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_timestamp(raw))
        future = {'attributes': {'reported_posted_at': 'Tue, 06 Oct 2099 12:00:00 +0000'}}
        self.assertFalse(evaluate_search({'date_window': 'LAST_30_DAYS'}, future,
                                        at='2026-10-18T20:00:00Z')['matched'])

    def test_preupgrade_absolute_date_needs_no_precision_to_override_a_relative_claim(self):
        previous = {'reported_posted_at': '2026-10-01T12:00:00Z'}
        result = normalize_reported_timestamp('2 hours ago',
            reference_at='2026-10-18T20:00:00Z', previous=previous)
        self.assertEqual(result['reported_posted_at'], '2026-10-01T12:00:00+00:00')
        self.assertEqual(result['reported_posted_at_precision'], 'SOURCE')
        self.assertIsNone(result['reported_posted_at_reference_at'])
        self.assertIsNone(result['reported_posted_at_raw'])

    def test_preupgrade_relative_date_uses_recorded_observation_before_older_first_seen(self):
        previous = {'reported_posted_at': '2 hours ago', 'observed_at': '2026-10-18T20:00:00Z',
            'first_seen_at': '2026-10-18T15:00:00Z', 'last_seen_at': '2026-10-21T20:00:00Z'}
        result = normalize_reported_timestamp('3 hours ago',
            reference_at='2026-10-18T21:00:00Z', previous=previous)
        self.assertEqual(result['reported_posted_at'], '2026-10-18T18:00:00+00:00')
        self.assertEqual(result['reported_posted_at_raw'], '2 hours ago')
        self.assertEqual(result['reported_posted_at_reference_at'], '2026-10-18T20:00:00+00:00')
        self.assertEqual(result['reported_posted_at_precision'], 'HOUR')
        self.assertEqual(normalize_reported_timestamp('2 hours ago',
            reference_at='2026-10-28T20:00:00Z', previous=result), result)
        fallback = {**previous, 'last_collected_at': previous['observed_at']}
        fallback.pop('observed_at')
        self.assertEqual(normalize_reported_timestamp('3 hours ago',
            reference_at='2026-10-18T21:00:00Z', previous=fallback), result)

    def test_invalid_previous_claim_cannot_anchor_a_first_valid_relative_observation(self):
        previous = {'reported_posted_at': None, 'reported_posted_at_raw': 'not a date',
            'reported_posted_at_reference_at': '2026-10-01T20:00:00Z',
            'reported_posted_at_precision': 'UNKNOWN'}
        result = normalize_reported_timestamp('2 hours ago',
            reference_at='2026-10-18T20:00:00Z', previous=previous)
        self.assertEqual(result['reported_posted_at'], '2026-10-18T18:00:00+00:00')
        self.assertEqual(result['reported_posted_at_reference_at'], '2026-10-18T20:00:00+00:00')
