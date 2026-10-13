"""Pure, bounded saved-search candidate snapshots for explicit advisory work."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from itertools import islice

from sqlalchemy import select

from packages.persistence import models as m
from services.ai.consumer import AIJobInput
from services.ai.cost_controls import CandidateScore
from services.discovery.matching import build_explanation
from services.matching.selected_search_scoring import compose_selected_search_match


def build_inputs(store, profile_id, policy, configuration, search_profile_id=None):
    # Imported locally to keep the existing durable exception contract in one place.
    from services.ai.durable_evaluation import DurableEvaluationError, EVALUATOR_VERSION, _digest
    profile = store.profile(profile_id)
    scope, criteria, evidence = None, None, {}
    options = {}
    minimum = 0
    if search_profile_id is not None:
        with store.engine.connect() as connection:
            search = connection.execute(select(m.search_profiles).where(
                m.search_profiles.c.id == search_profile_id,
                m.search_profiles.c.profile_id == profile_id)).mappings().one_or_none()
        if search is None:
            raise DurableEvaluationError('AI_SEARCH_NOT_FOUND')
        # enabled governs discovery scheduling; disabled saved Jobs filters are
        # still valid for an explicitly requested local evaluation.
        criteria = search['criteria']
        scope = {'search_profile_id': search['id'], 'name': search['name'],
                 'criteria_digest': _digest(criteria), 'updated_at': search['updated_at']}
        filters = criteria.get('job_filters') or {}
        for name in ('q', 'title', 'company', 'location', 'remote_type', 'provider',
                     'experience_level', 'employment_type', 'education_requirement',
                     'clearance_level', 'job_source_id', 'ats_provider',
                     'application_link_status', 'application_pipeline_status', 'application_capability'):
            if filters.get(name):
                options[name] = filters[name]
        for name in ('salary_min', 'salary_max'):
            if filters.get(name):
                options[name] = float(filters[name])
        if filters.get('skill'):
            options['skill_id'] = filters['skill']
        minimum = int(filters.get('min_match_score') or 0)
        if filters.get('posted_within_days'):
            options['posted_before'] = datetime.now(timezone.utc)
            options['posted_after'] = options['posted_before'] - timedelta(days=int(filters['posted_within_days']))
        from services.matching.company_preferences import list_company_preferences
        from services.tracking.jobs import list_job_tracking
        options['exclude_company_ids'] = [item['company_id'] for item in
            list_company_preferences(store, profile_id) if item['status'] == 'BLACKLISTED']

        def visible(rows):
            while batch := list(islice(rows, 100)):
                tracking = list_job_tracking(store, profile_id, [row['id'] for row in batch])
                yield from (row for row in batch if tracking[row['id']]['disposition']
                            not in ('HIDDEN', 'NOT_INTERESTED'))
        options.update(search_criteria=criteria, row_filter=visible)
    rows = store.jobs(profile_id=profile_id, limit=policy.max_input_candidates + 1, **options)
    if len(rows) > policy.max_input_candidates:
        raise DurableEvaluationError('AI_FUNNEL_INPUT_LIMIT')
    candidates = {}
    fingerprints = {}
    from services.ai.input_freshness import job_fingerprint
    for row in rows:
        if criteria is None:
            score = row.get('match_score')
        else:
            baseline = build_explanation(row, profile)
            composed = compose_selected_search_match(row, {'score': baseline['score']}, criteria,
                                                     candidate_profile=profile)
            score = composed['score']
            evidence[row['id']] = {'base_score': baseline['score'], **composed}
        if score is None or score < minimum:
            continue
        candidates[row['id']] = AIJobInput(row['id'], int(float(score)), row['title'],
                                          row['company'], row['description'][:8000])
        fingerprints[row['id']] = job_fingerprint(row)
    plan = policy.plan(CandidateScore(item.job_id, item.deterministic_score) for item in candidates.values())
    snapshot = {'evaluator_version': EVALUATOR_VERSION, 'profile_version': profile['version'],
                'policy': asdict(policy), 'configuration': configuration.to_dict(),
                'job_fingerprints': {job_id: fingerprints[job_id] for job_id in plan.ai_candidate_ids},
                'candidates': [asdict(candidates[job_id]) for job_id in plan.ai_candidate_ids]}
    if scope is not None:
        snapshot.update(search_scope=scope,
                        candidate_evidence={key: evidence[key] for key in plan.ai_candidate_ids})
    return snapshot, plan, len(rows)


def public_preview(snapshot, plan, scanned):
    from services.ai.durable_evaluation import _digest
    return {'policy': snapshot['policy'], 'fingerprint': _digest(snapshot),
            'search_scope': snapshot.get('search_scope'),
            'plan': {'scanned_job_count': scanned, 'scored_candidate_count': plan.received_count,
                     'unique_count': plan.unique_count, 'eligible_count': plan.eligible_count,
                     'ai_candidate_count': len(plan.ai_candidate_ids),
                     'document_candidate_count': len(plan.document_candidate_ids)},
            'candidates': [{'job_id': row['job_id'], 'title': row['title'], 'company': row['company'],
                            'deterministic_score': row['deterministic_score'],
                            'score_evidence': snapshot.get('candidate_evidence', {}).get(row['job_id'])}
                           for row in snapshot['candidates']], 'no_model_calls': True}


def evaluation_history(store, profile_id, limit=20, *, run_id=None):
    """Expose provenance from the already durable private snapshot, owner scoped.

    Legacy runs without a batch/scope remain readable as profile-wide results.
    No criteria or candidate text leaks through generic operation metadata.
    """
    runs = store.ai_evaluation_history(profile_id, limit, run_id=run_id)
    if not runs:
        return runs
    with store.engine.connect() as connection:
        snapshots = {row['id']: row['snapshot'] for row in connection.execute(
            select(m.ai_evaluation_batches.c.id, m.ai_evaluation_batches.c.snapshot).where(
                m.ai_evaluation_batches.c.profile_id == profile_id,
                m.ai_evaluation_batches.c.id.in_([run['run_id'] for run in runs]))).mappings()}
    for run in runs:
        snapshot = snapshots.get(run['run_id'], {})
        run['search_scope'] = snapshot.get('search_scope')
        for item in run['evaluations']:
            item['score_evidence'] = snapshot.get('candidate_evidence', {}).get(item['job_id'])
    return runs
