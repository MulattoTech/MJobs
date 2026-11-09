"""Risk evidence reads canonical attribution without trusting competing claims."""
from copy import deepcopy
import unittest

from services.discovery.job_risk_signals import evaluate_job_risk


class CanonicalFieldProvenanceRiskTests(unittest.TestCase):
    def job(self):
        return {
            'id': 'fixture-job',
            'description': 'Applicants must pay an application fee before interview.',
            'field_provenance': {
                'description': {'source': 'forged-top-level', 'confidence': 1},
            },
            'attributes': {
                'field_provenance': {
                    'description': {'source': 'forged-attribute', 'confidence': 1},
                },
                'source_resolution': {
                    'field_provenance_schema_version': 1,
                    'field_provenance': {
                        'description': {'source': 'feed.example', 'confidence': None},
                    },
                },
            },
        }

    def payment_evidence(self, job):
        result = evaluate_job_risk(job)
        return result['signals_by_code']['PAYMENT_REQUEST']['warning']['evidence']

    def test_resolved_attribution_wins_without_suppressing_warning_or_mutating_job(self):
        job = self.job()
        before = deepcopy(job)
        evidence = self.payment_evidence(job)
        self.assertEqual(evidence['source'], 'feed.example')
        self.assertIsNone(evidence['confidence'])
        self.assertIn('application fee', evidence['text'])
        self.assertEqual(job, before)

    def test_unknown_versions_never_fall_back_to_self_reported_provenance(self):
        for version in (None, 0, 2, True, 1.0, '1', {}, []):
            with self.subTest(version=version):
                job = self.job()
                job['attributes']['source_resolution']['field_provenance_schema_version'] = version
                evidence = self.payment_evidence(job)
                self.assertEqual(evidence['source'], 'UNKNOWN')
                self.assertIsNone(evidence['confidence'])
        job = self.job()
        del job['attributes']['source_resolution']['field_provenance_schema_version']
        self.assertEqual(self.payment_evidence(job)['source'], 'UNKNOWN')

    def test_missing_or_malformed_maps_and_entries_stay_unknown(self):
        for provenance in (None, [], 'not-a-map', {}, {'description': []},
                           {'description': 'not-an-entry'}):
            with self.subTest(provenance=provenance):
                job = self.job()
                job['attributes']['source_resolution']['field_provenance'] = provenance
                evidence = self.payment_evidence(job)
                self.assertEqual(evidence['source'], 'UNKNOWN')
                self.assertIsNone(evidence['confidence'])

    def test_nested_confidence_and_source_keep_existing_validation_boundaries(self):
        for confidence in (True, -0.1, 1.1, float('nan'), float('inf'), '0.9', {}):
            with self.subTest(confidence=confidence):
                job = self.job()
                job['attributes']['source_resolution']['field_provenance']['description'] = {
                    'source': 'feed.example', 'confidence': confidence,
                }
                evidence = self.payment_evidence(job)
                self.assertEqual(evidence['source'], 'feed.example')
                self.assertIsNone(evidence['confidence'])
        for source in (None, {}, ' ', 'x' * 81):
            with self.subTest(source=source):
                job = self.job()
                job['attributes']['source_resolution']['field_provenance']['description'] = {
                    'source': source, 'confidence': None,
                }
                self.assertEqual(self.payment_evidence(job)['source'], 'UNKNOWN')

    def test_legacy_provenance_remains_supported_only_without_new_envelope(self):
        job = self.job()
        job['attributes']['source_resolution'] = {'selected_candidate_id': 'old-candidate'}
        self.assertEqual(self.payment_evidence(job)['source'], 'forged-top-level')
        del job['field_provenance']
        self.assertEqual(self.payment_evidence(job)['source'], 'forged-attribute')
        del job['attributes']['field_provenance']
        self.assertEqual(self.payment_evidence(job)['source'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
