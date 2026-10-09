import { describe, expect, it } from 'vitest';
import { discoveryActivity } from './company-activity';

describe('source-linked discovery activity', () => {
  it('converts recorded feed import counts into a safe readable summary', () => {
    const rows = discoveryActivity([{
      id: 'legacy-import', action: 'COMPANY_FEED_IMPORT_COMPLETE',
      timestamp: '2026-10-02T10:00:00Z',
      details: {jobs_imported: 3, jobs_enumerated: 5, private_payload: 'PRIVATE'},
    }]);

    expect(rows.map(row => row.message)).toEqual(['Imported 3 jobs from 5 feed records.']);
    expect(JSON.stringify(rows)).not.toContain('PRIVATE');
  });

  it('maps known event types to safe summaries and ignores raw event details', () => {
    const rows = discoveryActivity([
      {
        id: 'evt-discovered', event_type: 'COMPANY_DISCOVERED',
        summary: 'Discovered Acme from Remote Rocketship',
        timestamp: '2026-10-08T12:00:00Z',
        details: {company_name: 'PRIVATE', payload: 'PRIVATE', profile_id: 'other-profile'},
      },
      {
        id: 'evt-import', event_type: 'FEED_IMPORT_COMPLETE',
        summary: 'Imported 4 active openings', timestamp: '2026-10-08T11:00:00Z',
        details: {jobs_imported: 4, matched_jobs: 99, profile_id: 'other-profile'},
      },
      {
        id: 'evt-matches', event_type: 'COMPANY_FEED_MATCHES',
        summary: '6 openings match this profile', timestamp: '2026-10-08T10:00:00Z',
      },
    ]);

    expect(rows.map(({id, message}) => [id, message])).toEqual([
      ['evt-discovered', 'Discovered Acme from Remote Rocketship'],
      ['evt-import', 'Imported 4 active openings'],
      ['evt-matches', '6 openings match this profile'],
    ]);
    expect(JSON.stringify(rows)).not.toContain('PRIVATE');
    expect(JSON.stringify(rows)).not.toContain('other-profile');
    expect(JSON.stringify(rows)).not.toContain('99');
  });

  it('omits events with unknown types or unsafe summaries', () => {
    expect(discoveryActivity([
      {id: 'unknown', event_type: 'SECRET_DUMP', summary: 'PRIVATE details', timestamp: '2026-10-08T12:00:00Z'},
      {id: 'control', event_type: 'COMPANY_SOURCE_ENROLLED', summary: 'Added\nPRIVATE source', timestamp: '2026-10-08T11:00:00Z'},
      {id: 'missing-summary', event_type: 'FEED_IMPORT_COMPLETE', timestamp: '2026-10-08T10:00:00Z'},
    ])).toEqual([]);
  });

  it('formats resolved domain, detected ATS, and direct source enrollment events', () => {
    expect(discoveryActivity([
      {id: 'domain', event_type: 'COMPANY_DOMAIN_RESOLVED', summary: 'Resolved official domain', timestamp: null},
      {id: 'ats', event_type: 'COMPANY_ATS_DETECTED', summary: 'Detected Lever', timestamp: null},
      {id: 'source', event_type: 'COMPANY_SOURCE_ENROLLED', summary: 'Added a direct employer source', timestamp: null},
      {id: 'profile-source', event_type: 'COMPANY_SOURCE_ENROLLED', summary: 'Added direct employer source for Acme Systems', timestamp: null},
    ]).map(row => row.message)).toEqual([
      'Resolved official domain', 'Detected Lever', 'Added a direct employer source',
      'Added direct employer source for Acme Systems',
    ]);
  });

  it('normalizes bounded discovery attempt details and drops unknown values', () => {
    const rows = discoveryActivity([{
      id: 'attempt', event_type: 'COMPANY_DISCOVERY_ATTEMPT',
      summary: 'Company discovery attempt needs review.',
      timestamp: '2026-10-08T12:00:00Z',
      details: {
        company: 'Example Employer', triggers: ['Previously unseen company', 'PRIVATE'],
        attempt_state: 'DEAD_LETTERED', attempt_count: 2, ats_provider: 'Greenhouse',
        confidence: 0.94, confidence_basis: 'ATS detection',
        evidence: ['ATS detected', 'Official domain verified', 'PRIVATE'],
        source_created: false,
        results: {jobs_enumerated: 5, jobs_imported: 3, matched_jobs: 2, private: 'SECRET'},
        error: 'No official employer website could be verified.',
        private_url: 'https://private.invalid/?token=secret',
      },
    }]);
    expect(rows).toEqual([{
      id: 'attempt', message: 'Company discovery attempt needs review.',
      timestamp: '2026-10-08T12:00:00.000Z',
      details: {
        company: 'Example Employer', triggers: ['Previously unseen company'],
        attemptState: 'DEAD_LETTERED', attemptCount: 2, atsProvider: 'Greenhouse',
        confidence: 0.94, confidenceBasis: 'ATS detection',
        evidence: ['ATS detected', 'Official domain verified'],
        sourceCreated: false,
        results: {jobsEnumerated: 5, jobsImported: 3, matchedJobs: 2},
        error: 'No official employer website could be verified.',
      },
    }]);
    expect(JSON.stringify(rows)).not.toContain('private.invalid');
    expect(JSON.stringify(rows)).not.toContain('secret');
  });

  it('rejects unsafe company names, errors, states, and out-of-range confidence', () => {
    const [row] = discoveryActivity([{
      id: 'attempt', event_type: 'COMPANY_DISCOVERY_ATTEMPT',
      summary: 'Company discovery is in progress.', timestamp: null,
      details: {
        company: '<script>bad</script>', triggers: ['High-match job'],
        attempt_state: 'UNTRUSTED_STATE', attempt_count: 2.5, ats_provider: 'private',
        confidence: 5, confidence_basis: 'secret', evidence: ['arbitrary'],
        source_created: 'yes', results: {jobs_enumerated: -1}, error: 'raw exception with secret',
      },
    }]);
    expect(row?.details).toEqual({
      company: null, triggers: ['High-match job'], attemptState: null,
      attemptCount: null, atsProvider: null, confidence: null,
      confidenceBasis: null, evidence: [], sourceCreated: null, results: null, error: null,
    });
  });

  it('keeps the server-provided newest-first order and stable equal-time order', () => {
    const rows = discoveryActivity([
      {id: 'newest', event_type: 'COMPANY_DISCOVERED', summary: 'Discovered Acme', timestamp: '2026-10-08T12:00:00Z'},
      {id: 'tie-first', event_type: 'COMPANY_SOURCE_ENROLLED', summary: 'Added a direct employer source', timestamp: '2026-10-08T11:00:00Z'},
      {id: 'tie-second', event_type: 'FEED_IMPORT_COMPLETE', summary: 'Imported 2 active openings', timestamp: '2026-10-08T11:00:00Z'},
    ]);
    expect(rows.map(row => row.id)).toEqual(['newest', 'tie-first', 'tie-second']);
    expect(rows.map(row => row.timestamp)).toEqual([
      '2026-10-08T12:00:00.000Z', '2026-10-08T11:00:00.000Z', '2026-10-08T11:00:00.000Z',
    ]);
  });

  it('does not invent counts, accepts zero only from a recorded summary, and bounds history', () => {
    expect(discoveryActivity([
      {id: 'absent', event_type: 'FEED_IMPORT_COMPLETE', summary: 'Feed import completed.', timestamp: null},
      {id: 'zero', event_type: 'FEED_IMPORT_COMPLETE', summary: 'Imported 0 active openings', timestamp: null},
      {id: 'invalid-count', event_type: 'FEED_IMPORT_COMPLETE', summary: 'Imported -1 active openings', timestamp: null},
    ]).map(row => row.message)).toEqual(['Feed import completed.', 'Imported 0 active openings']);

    const history = Array.from({length: 30}, (_, index) => ({
      id: `event-${index}`, event_type: 'COMPANY_SOURCE_ENROLLED',
      summary: 'Added a direct employer source', timestamp: 'invalid',
    }));
    const bounded = discoveryActivity(history);
    expect(bounded).toHaveLength(20);
    expect(bounded.map(row => row.id)).toEqual(history.slice(0, 20).map(row => row.id));
    expect(bounded[0].timestamp).toBeNull();
  });
});
