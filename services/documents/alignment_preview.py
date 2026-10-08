"""Read-only, deterministic resume alignment preview over saved snapshots."""
from services.documents import tailor


def _version(value):
    return type(value) is str and len(value) == 64 and all(char in '0123456789abcdef' for char in value)


def build_alignment_preview(profile, job, expected_profile_version, expected_job_fingerprint):
    """Map saved target terms to eligible approved source fact IDs without generating content."""
    if not isinstance(profile, dict) or not _version(expected_profile_version):
        raise ValueError('A current profile snapshot is required')
    if profile.get('version') != expected_profile_version:
        raise ValueError('Profile changed; reload approved facts before previewing')
    if not isinstance(job, dict) or type(job.get('id')) is not str:
        raise ValueError('A saved target job is required')
    if (type(expected_job_fingerprint) is not str or len(expected_job_fingerprint) != 64 or
            any(char not in '0123456789abcdef' for char in expected_job_fingerprint)):
        raise ValueError('A current target job snapshot is required')
    try:
        actual_job_fingerprint = tailor.target_fingerprint(job)
    except (KeyError, TypeError, ValueError):
        raise ValueError('Target job snapshot is unsupported') from None
    if actual_job_fingerprint != expected_job_fingerprint:
        raise ValueError('Target job changed; reload the saved job before previewing')
    skills = job.get('skills')
    if (type(skills) is not list or any(type(term) is not str or not term.strip() or len(term) > 100
                                        for term in skills)):
        raise ValueError('Target job skill terms are unsupported')

    eligible = tailor.eligible_rows(profile)
    skill_rows = [dict(row, fact_id=row.get('fact_id', row.get('id'))) for row in eligible]
    skill_matches = tailor.matched_labels(skill_rows, {'skills': skills})
    responsibility_terms = tailor.target_responsibilities(job)
    responsibility_matches = tailor.matched_responsibility_phrases(skill_rows, responsibility_terms)

    skill_terms = []
    seen = set()
    for term in skills:
        if term.strip().casefold() not in seen:
            seen.add(term.strip().casefold())
            skill_terms.append(term.strip())
    mapped_skills = [
        {'term': term, 'fact_ids': [row['fact_id'] for row in skill_rows
                                   if term in skill_matches.get(row['fact_id'], [])]}
        for term in skill_terms
    ]
    mapped_responsibilities = [
        {'term': term, 'fact_ids': [row['fact_id'] for row in skill_rows
                                    if term in responsibility_matches.get(row['fact_id'], [])]}
        for term in responsibility_terms
    ]
    return {
        'profile_id': profile['id'],
        'profile_version': expected_profile_version,
        'job_id': job['id'],
        'job_fingerprint': actual_job_fingerprint,
        'matches': {
            'skills': mapped_skills,
            'responsibilities': mapped_responsibilities,
        },
        'unmatched': {
            'skills': [row['term'] for row in mapped_skills if not row['fact_ids']],
            'responsibilities': [row['term'] for row in mapped_responsibilities if not row['fact_ids']],
        },
        'read_only': True,
    }
