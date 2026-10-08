import unittest

from services.documents.alignment_preview import build_alignment_preview
from services.documents.tailor import target_fingerprint


class ResumeAlignmentPreviewTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            'id': 'profile-a', 'version': 'a' * 64,
            'facts': [
                {'fact_id': 'python', 'kind': 'skill', 'text': 'Python', 'source': 'fixture',
                 'confidence': 100, 'verified': True, 'user_approved': True},
                {'fact_id': 'duty', 'kind': 'responsibility',
                 'text': 'Administer Linux servers and restore production services.',
                 'source': 'fixture', 'confidence': 100, 'verified': True, 'user_approved': True},
                {'fact_id': 'pending', 'kind': 'skill', 'text': 'Kubernetes', 'source': 'fixture',
                 'confidence': 100, 'verified': True, 'user_approved': False},
            ],
        }
        self.job = {
            'id': 'j' * 64, 'title': 'Systems Administrator', 'skills': ['Python', 'Kubernetes'],
            'description': 'Responsibilities:\n- Administer Linux servers and restore production services.\n'
                '- Coordinate cross-team incident communication and status updates.\n'
                'Requirements:\n- AWS certification and deep leadership experience.',
        }

    def test_preview_maps_exact_supported_terms_only_to_eligible_fact_references(self):
        result = build_alignment_preview(
            self.profile, self.job, self.profile['version'], target_fingerprint(self.job))

        self.assertEqual(result['matches']['skills'], [
            {'term': 'Python', 'fact_ids': ['python']},
            {'term': 'Kubernetes', 'fact_ids': []},
        ])
        self.assertTrue(any(row['term'] == 'administer linux servers and restore production services'
                            and row['fact_ids'] == ['duty'] for row in result['matches']['responsibilities']))
        self.assertIn('Kubernetes', result['unmatched']['skills'])
        self.assertTrue(any('coordinate cross team incident communication' in term
                            for term in result['unmatched']['responsibilities']))
        self.assertNotIn('pending', str(result))

    def test_preview_fails_closed_for_stale_profile_or_job_snapshot(self):
        for profile_version, job_fingerprint in (
            ('x' * 64, target_fingerprint(self.job)),
            (self.profile['version'], 'y' * 64),
        ):
            with self.subTest(profile_version=profile_version[0], job_fingerprint=job_fingerprint[0]):
                with self.assertRaises(ValueError):
                    build_alignment_preview(self.profile, self.job, profile_version, job_fingerprint)

    def test_preview_fails_closed_when_saved_job_skill_input_is_unsupported(self):
        malformed = {**self.job, 'skills': ['Python', None]}
        with self.assertRaises(ValueError):
            build_alignment_preview(
                self.profile, malformed, self.profile['version'], target_fingerprint(malformed))


if __name__ == '__main__':
    unittest.main()
