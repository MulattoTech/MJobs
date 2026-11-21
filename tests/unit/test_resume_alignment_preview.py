from copy import deepcopy
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

    def test_preview_matches_only_selected_facts_and_rejects_foreign_or_ineligible_ids(self):
        selected = build_alignment_preview(
            self.profile, self.job, self.profile['version'], target_fingerprint(self.job),
            selected_fact_ids=['duty'])

        self.assertEqual(selected['matches']['skills'][0]['fact_ids'], [])
        self.assertIn('Python', selected['unmatched']['skills'])
        self.assertEqual(selected['matches']['responsibilities'][0]['fact_ids'], ['duty'])
        self.assertEqual(selected['selected_fact_ids'], ['duty'])
        self.assertEqual(selected['selection_mode'], 'EXPLICIT_SELECTION')

        empty = build_alignment_preview(
            self.profile, self.job, self.profile['version'], target_fingerprint(self.job),
            selected_fact_ids=[])
        self.assertTrue(all(not row['fact_ids'] for row in empty['matches']['skills']))
        self.assertIn('Python', empty['unmatched']['skills'])
        self.assertEqual(empty['selected_fact_ids'], [])

        for ids in (['missing'], ['pending'], ['python', 'python'], ['python', 3]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                build_alignment_preview(
                    self.profile, self.job, self.profile['version'], target_fingerprint(self.job),
                    selected_fact_ids=ids)

    def test_preview_preserves_each_supported_alias_in_either_target_order(self):
        for labels, fact_text in (
            (['AWS', 'Amazon Web Services'], 'Maintained Amazon Web Services infrastructure.'),
            (['Kubernetes', 'K8s'], 'Operated Kubernetes clusters.'),
            (['Microsoft 365', 'Office 365', 'M365'], 'Administered Office 365.'),
            (['PostgreSQL', 'Postgres'], 'Backed up PostgreSQL databases.'),
            (['C++', 'CPP'], 'Developed C++ applications.'),
        ):
            self.profile['facts'][0]['text'] = fact_text
            for ordered_labels in (labels, list(reversed(labels))):
                with self.subTest(labels=ordered_labels):
                    job = {**self.job, 'skills': ordered_labels}
                    before = deepcopy((self.profile, job))
                    result = build_alignment_preview(
                        self.profile, job, self.profile['version'], target_fingerprint(job))
                    self.assertEqual(result['matches']['skills'], [
                        {'term': label, 'fact_ids': ['python']} for label in ordered_labels
                    ])
                    self.assertEqual(result['unmatched']['skills'], [])
                    self.assertEqual((self.profile, job), before)
                    self.assertTrue(result['read_only'])

    def test_alias_evidence_is_limited_to_selected_eligible_facts(self):
        self.profile['facts'][0]['text'] = 'Amazon Web Services'
        self.profile['facts'][1]['text'] = 'Built AWS infrastructure.'
        self.profile['facts'][2]['text'] = 'AWS'
        job = {**self.job, 'skills': ['AWS', 'Amazon Web Services']}
        for selected, expected in ((None, ['python', 'duty']), (['duty'], ['duty']), ([], [])):
            with self.subTest(selected=selected):
                result = build_alignment_preview(
                    self.profile, job, self.profile['version'], target_fingerprint(job),
                    selected_fact_ids=selected)
                self.assertEqual(result['matches']['skills'], [
                    {'term': 'AWS', 'fact_ids': expected},
                    {'term': 'Amazon Web Services', 'fact_ids': expected},
                ])
                self.assertNotIn('pending', str(result))
                self.assertEqual(result['unmatched']['skills'], [] if expected else job['skills'])
        with self.assertRaisesRegex(ValueError, 'missing or no longer approved'):
            build_alignment_preview(
                self.profile, job, self.profile['version'], target_fingerprint(job),
                selected_fact_ids=['pending'])

    def test_alias_matching_does_not_relax_fact_eligibility(self):
        job = {**self.job, 'skills': ['AWS', 'Amazon Web Services']}
        source = {**self.profile['facts'][0], 'text': 'AWS'}
        for ineligible in ({'verified': False}, {'user_approved': False},
                           {'confidence': 79}, {'source': ' '}):
            with self.subTest(ineligible=ineligible):
                profile = {**self.profile, 'facts': [{**source, **ineligible}]}
                result = build_alignment_preview(
                    profile, job, profile['version'], target_fingerprint(job))
                self.assertEqual(result['selected_fact_ids'], [])
                self.assertEqual(result['unmatched']['skills'], job['skills'])
                self.assertTrue(all(not row['fact_ids'] for row in result['matches']['skills']))

    def test_alias_matching_preserves_display_deduplication_and_unknown_boundaries(self):
        self.profile['facts'][0]['text'] = 'AWS, JavaScript, and ZetaCloud applications.'
        job = {**self.job, 'skills': [
            ' aws ', 'AWS', 'Amazon Web Services', ' amazon web services ',
            'JS', 'JavaScript', 'Java', 'ZetaCloud', 'zetacloud', 'Zeta', 'Cloud', 'ZetaCloud Pro',
        ]}
        result = build_alignment_preview(
            self.profile, job, self.profile['version'], target_fingerprint(job))
        self.assertEqual(result['matches']['skills'], [
            {'term': 'aws', 'fact_ids': ['python']},
            {'term': 'Amazon Web Services', 'fact_ids': ['python']},
            {'term': 'JS', 'fact_ids': ['python']},
            {'term': 'JavaScript', 'fact_ids': ['python']},
            {'term': 'Java', 'fact_ids': []},
            {'term': 'ZetaCloud', 'fact_ids': ['python']},
            {'term': 'Zeta', 'fact_ids': []},
            {'term': 'Cloud', 'fact_ids': []},
            {'term': 'ZetaCloud Pro', 'fact_ids': []},
        ])
        self.assertEqual(result['unmatched']['skills'], ['Java', 'Zeta', 'Cloud', 'ZetaCloud Pro'])

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
