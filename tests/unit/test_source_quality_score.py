from datetime import datetime, timedelta, timezone
import unittest

from services.company_discovery.quality_score import quality_score


AT = datetime(2025, 1, 31, 12, 0, tzinfo=timezone.utc)


def observation(at, **values):
    return {"observed_at": at.isoformat(), **values}


class SourceQualityScoreTests(unittest.TestCase):
    def test_aggregates_named_components_and_equal_weight_score(self):
        rows = [
            observation(AT - timedelta(days=1), succeeded=True, available=True,
                        enumerated=10, accepted=8, completeness_sum=4,
                        salary_known=6, links_checked=3, links_accurate=2),
            observation(AT - timedelta(days=2), succeeded=False, available=False,
                        enumerated=2, accepted=1, completeness_sum=0.5,
                        salary_known=1, links_checked=1, links_accurate=1),
        ]
        result = quality_score(rows, at=AT.isoformat())
        components = result["components"]
        self.assertAlmostEqual(components["freshness"], 96.666666, places=4)
        self.assertEqual(components["completeness"], 50.0)
        self.assertEqual(components["availability"], 100.0)
        self.assertEqual(components["parser_reliability"], 75.0)
        self.assertAlmostEqual(components["salary_completeness"], 77.777777, places=4)
        self.assertEqual(components["application_link_accuracy"], 75.0)
        self.assertEqual(components["historical_uptime"], 50.0)
        self.assertAlmostEqual(result["score"], sum(components.values()) / 7, places=5)
        self.assertEqual(result["coverage"], 100.0)
        self.assertEqual(result["sample_counts"]["parser_reliability"], 2)
        self.assertEqual(result["window"]["observation_count"], 2)

    def test_uses_latest_nonnull_availability_and_excludes_old_future_and_overflow(self):
        rows = [
            observation(AT - timedelta(days=31), succeeded=True, available=False),
            observation(AT + timedelta(minutes=1), succeeded=True, available=True),
            observation(AT - timedelta(days=1), succeeded=True, available=False),
            observation(AT - timedelta(days=2), succeeded=True, available=None),
        ]
        rows.extend(observation(AT - timedelta(hours=3, seconds=i), succeeded=True, available=True)
                    for i in range(250))
        result = quality_score(rows, at=AT)
        self.assertEqual(result["components"]["availability"], 100.0)
        self.assertEqual(result["components"]["historical_uptime"], 100.0)
        self.assertEqual(result["window"]["observation_count"], 200)
        self.assertEqual(result["window"]["excluded_future"], 1)
        self.assertGreaterEqual(result["window"]["excluded_outside_days"], 1)

    def test_no_history_has_no_score_and_partial_metrics_reduce_coverage(self):
        empty = quality_score([], at=AT)
        self.assertTrue(all(value is None for value in empty["components"].values()))
        self.assertIsNone(empty["score"])
        self.assertEqual(empty["coverage"], 0.0)
        result = quality_score([observation(AT - timedelta(hours=1), succeeded=True, available=True)], at=AT)
        self.assertIsNotNone(result["components"]["freshness"])
        self.assertIsNotNone(result["components"]["availability"])
        self.assertIsNone(result["components"]["completeness"])
        self.assertAlmostEqual(result["coverage"], 3 / 7 * 100, places=5)

    def test_rejects_malformed_counts_timestamps_and_nan(self):
        bad_rows = [
            {"observed_at": AT.isoformat(), "succeeded": True, "enumerated": True},
            {"observed_at": "2025-01-31T12:00:00", "succeeded": True},
            {"observed_at": AT.isoformat(), "succeeded": True, "accepted": -1},
            {"observed_at": AT.isoformat(), "succeeded": True, "completeness_sum": float("nan")},
            {"observed_at": AT.isoformat(), "succeeded": True, "accepted": 1},
            {"observed_at": AT.isoformat(), "succeeded": True, "enumerated": 1, "salary_known": 1},
            {"observed_at": AT.isoformat(), "succeeded": True, "accepted": 1, "links_checked": 2},
            {"observed_at": AT.isoformat(), "succeeded": True, "links_accurate": 1},
        ]
        for row in bad_rows:
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    quality_score([row], at=AT)

    def test_same_timestamp_availability_conservatively_prefers_false(self):
        rows = [
            observation(AT - timedelta(hours=1), succeeded=True, available=True),
            observation(AT - timedelta(hours=1), succeeded=True, available=False),
        ]
        self.assertEqual(quality_score(rows, at=AT)["components"]["availability"], 0.0)

    def test_limitations_explain_measurement_boundaries(self):
        result = quality_score([observation(AT - timedelta(hours=1), succeeded=True, available=True)], at=AT)
        limitations = " ".join(result["limitations"])
        for phrase in ("collection age", "field presence", "observed attempt availability",
                       "not continuous uptime", "accuracy is unknown", "equal-weight",
                       "not authority or probability"):
            self.assertIn(phrase, limitations)


if __name__ == "__main__":
    unittest.main()
