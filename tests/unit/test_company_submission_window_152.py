"""Company caps expire proven completed submissions, never uncertain outcomes."""

from datetime import datetime, timedelta, timezone
import unittest

from services.applications.submission_windows import company_submission_reserves_capacity


NOW = datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(days=1)


class CompanySubmissionWindowTests(unittest.TestCase):
    def reserves(self, submitted_at, state="CONFIRMED", clock=NOW):
        return company_submission_reserves_capacity(
            state, submitted_at, evaluated_at=clock)

    def test_completed_submission_expires_only_after_a_full_rolling_day(self):
        self.assertFalse(self.reserves((CUTOFF - timedelta(microseconds=1)).isoformat()))
        self.assertTrue(self.reserves(CUTOFF.isoformat()))
        self.assertTrue(self.reserves((CUTOFF + timedelta(microseconds=1)).isoformat()))
        self.assertTrue(self.reserves((NOW + timedelta(days=1)).isoformat()))

    def test_equivalent_instants_are_not_ordered_as_raw_timestamp_strings(self):
        for offset in (-12, -6, 0, 5.5, 14):
            zone = timezone(timedelta(hours=offset))
            with self.subTest(offset=offset):
                self.assertTrue(self.reserves(CUTOFF.astimezone(zone).isoformat()))
                self.assertFalse(self.reserves(
                    (CUTOFF - timedelta(microseconds=1)).astimezone(zone).isoformat()))
        self.assertTrue(self.reserves(CUTOFF.isoformat().replace("+00:00", "Z")))

    def test_missing_malformed_and_timezone_naive_times_cannot_prove_expiry(self):
        for value in (None, "", " ", "not-a-date", "2026-02-30T00:00:00+00:00",
                      "2020-01-01T00:00:00", "2020-01-01", "0" * 65,
                      0, False, {}, datetime(2020, 1, 1, tzinfo=timezone.utc)):
            with self.subTest(value=value):
                self.assertTrue(self.reserves(value))

    def test_unresolved_reservations_do_not_expire_with_age(self):
        for state in ("SUBMITTING", "SUBMISSION_UNCONFIRMED"):
            for value in (None, "", "invalid", "2020-01-01T00:00:00+00:00",
                          CUTOFF.isoformat()):
                with self.subTest(state=state, timestamp=value):
                    self.assertTrue(self.reserves(value, state=state))

    def test_non_submitted_states_do_not_reserve_capacity(self):
        for state in ("PREPARED", "READY_TO_SUBMIT", "FAILED", "CANCELLED"):
            with self.subTest(state=state):
                self.assertFalse(self.reserves(NOW.isoformat(), state=state))

    def test_clock_must_establish_an_unambiguous_instant(self):
        for clock in (None, NOW.isoformat(), datetime(2026, 11, 9)):
            with self.subTest(clock=clock), self.assertRaises(ValueError):
                self.reserves("2020-01-01T00:00:00+00:00", clock=clock)

    def test_non_utc_clock_uses_the_same_rolling_window(self):
        clock = NOW.astimezone(timezone(timedelta(hours=-6)))
        self.assertTrue(self.reserves(CUTOFF.isoformat(), clock=clock))
        self.assertFalse(self.reserves(
            (CUTOFF - timedelta(microseconds=1)).isoformat(), clock=clock))

    def test_extreme_aware_timestamps_do_not_release_uncertain_future_rows(self):
        self.assertTrue(self.reserves("9999-12-31T23:59:59-23:59"))
        self.assertFalse(self.reserves("0001-01-01T00:00:00+23:59"))


if __name__ == "__main__":
    unittest.main()
