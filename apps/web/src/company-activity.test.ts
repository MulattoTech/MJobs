import { describe, expect, it } from 'vitest';
import { discoveryActivity } from './company-activity';

describe('source-linked discovery activity', () => {
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
    ]).map(row => row.message)).toEqual([
      'Resolved official domain', 'Detected Lever', 'Added a direct employer source',
    ]);
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
