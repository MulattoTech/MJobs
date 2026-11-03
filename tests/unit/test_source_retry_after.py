"""Bounded, conservative provider cooldown parsing."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import unittest

from services.discovery.sources.retry_after import (
    DISABLE_RETRY_SECONDS, MAX_RETRY_SECONDS, retry_after_seconds,
)


class RetryAfterParserTests(unittest.TestCase):
    instant = datetime(2026, 11, 3, 4, 0, 0, 500000, tzinfo=timezone.utc)

    def test_numeric_values_and_existing_provider_fallbacks(self):
        for raw, expected in ((None, 60), ("45", 45), ("0", 1), ("0009", 9),
                              (" \t45\t ", 45), ("", 60), ("-1", 60), ("+1", 60),
                              ("1.5", 60), ("1e6", 60), ("１２", 60), ("١٢", 60),
                              (True, 60), (45, 60), ("45,60", 60)):
            with self.subTest(raw=raw):
                self.assertEqual(retry_after_seconds(raw, now=self.instant), expected)
        self.assertEqual(retry_after_seconds(None, minimum=60, invalid_default=86400), 60)
        self.assertEqual(retry_after_seconds("invalid", minimum=60, invalid_default=86400), 86400)
        self.assertEqual(retry_after_seconds("1", default=300, minimum=300), 300)

    def test_all_three_http_date_forms_round_up_and_past_dates_keep_minimum(self):
        for raw in ("Tue, 03 Nov 2026 04:00:02 GMT",
                    "Tuesday, 03-Nov-26 04:00:02 GMT",
                    "Tue Nov  3 04:00:02 2026"):
            with self.subTest(raw=raw):
                self.assertEqual(retry_after_seconds(raw, now=self.instant), 2)
        self.assertEqual(retry_after_seconds("Tue, 03 Nov 2026 04:00:01 GMT", now=self.instant), 1)
        self.assertEqual(retry_after_seconds("Tue, 03 Nov 2026 03:00:00 GMT",
                                            now=self.instant, minimum=60), 60)

    def test_two_digit_years_follow_fifty_year_rule_instead_of_fixed_1969_cutoff(self):
        # 2070 is within fifty years of2026; 2090 is interpreted as1990.
        self.assertEqual(retry_after_seconds("Monday, 03-Nov-70 04:00:00 GMT",
                                            now=self.instant),
                         int((datetime(2070, 11, 3, 4, tzinfo=timezone.utc) - self.instant).total_seconds()) + 1)
        self.assertEqual(retry_after_seconds("Friday, 03-Nov-90 04:00:00 GMT",
                                            now=self.instant), 1)
        near_century = datetime(2099, 12, 31, 23, 59, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds("Friday, 01-Jan-00 00:00:00 GMT",
                                            now=near_century), 60)

    def test_malformed_dates_never_leak_or_accept_trailing_or_timezone_text(self):
        values = ("Tue, 03 Nov 2026 04:00:02 GMT private=canary",
                  "Tue, 03 Nov 2026 04:00:02 +0100",
                  "Tue, 03 Nov 2026 04:00:02",
                  "Tue, 31 Feb 2026 04:00:02 GMT",
                  "Tue, 03 Nov 2026 25:00:02 GMT",
                  "Tue, 03 Nov 2026 04:00:02 GMT, Tue, 03 Nov 2026 06:00:02 GMT",
                  "\r\n120", "\u00a0120", "private-canary@example.invalid")
        for raw in values:
            with self.subTest(raw=raw):
                self.assertEqual(retry_after_seconds(raw, now=self.instant), 60)

    def test_unrepresentable_intervals_disable_instead_of_shortening_retry(self):
        for raw in (str(MAX_RETRY_SECONDS + 1), "9" * 128, "9" * 10000,
                    "private" * 30, "Wed, 03 Nov 9999 04:00:00 GMT"):
            with self.subTest(length=len(raw)):
                self.assertGreater(retry_after_seconds(raw, now=self.instant), MAX_RETRY_SECONDS)
        self.assertEqual(retry_after_seconds(str(MAX_RETRY_SECONDS)), MAX_RETRY_SECONDS)
        self.assertEqual(retry_after_seconds("0" * 100 + "45"), 45)

    def test_aware_receipt_clock_can_use_non_utc_offset(self):
        local = self.instant.astimezone(timezone(timedelta(hours=3)))
        future = self.instant + timedelta(seconds=8.5)
        self.assertEqual(retry_after_seconds(format_datetime(future, usegmt=True), now=local), 9)

    def test_valid_leap_second_dates_do_not_fall_back_to_an_early_retry(self):
        instant = datetime(2016, 12, 31, 22, tzinfo=timezone.utc)
        for raw in ("Sat, 31 Dec 2016 23:59:60 GMT",
                    "Saturday, 31-Dec-16 23:59:60 GMT",
                    "Sat Dec 31 23:59:60 2016"):
            with self.subTest(raw=raw):
                self.assertEqual(retry_after_seconds(raw, now=instant), 7200)

    def test_valid_extreme_delays_are_preserved_and_oversized_input_blocks(self):
        self.assertEqual(retry_after_seconds("9" * 128), int("9" * 128))
        self.assertEqual(retry_after_seconds(str(MAX_RETRY_SECONDS + 1)), MAX_RETRY_SECONDS + 1)
        self.assertEqual(retry_after_seconds("9" * 10000), DISABLE_RETRY_SECONDS)
        self.assertEqual(retry_after_seconds("Fri, 31 Dec 9999 23:59:60 GMT",
                                            now=self.instant), DISABLE_RETRY_SECONDS)

    def test_offset_clock_at_fifty_year_cutoff_uses_utc_calendar_components(self):
        instant = datetime(2026, 11, 3, 4, tzinfo=timezone.utc)
        local = instant.astimezone(timezone(timedelta(hours=3)))
        raw = "Tuesday, 03-Nov-76 04:00:01 GMT"
        self.assertEqual(retry_after_seconds(raw, now=local), 1)
        self.assertEqual(retry_after_seconds(raw, now=instant), 1)
