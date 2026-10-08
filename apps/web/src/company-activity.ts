/** Render only the API's allowlisted summaries, never raw audit payloads. */
export type DiscoveryActivity = {id: string; message: string; timestamp: string | null};

type ActivityKind =
  | 'COMPANY_DISCOVERED'
  | 'COMPANY_SOURCE_ENROLLED'
  | 'COMPANY_DOMAIN_RESOLVED'
  | 'COMPANY_ATS_DETECTED'
  | 'FEED_IMPORT_COMPLETE'
  | 'COMPANY_FEED_MATCHES'
  | 'COMPANY_SOURCE_REFRESHED'
  | 'COMPANY_SOURCE_REFRESH_FAILED'
  | 'COMPANY_SOURCE_MIGRATION_REVIEW'
  | 'COMPANY_SOURCE_MONITORING_REVIEWED';

const FIXED_SUMMARIES: Partial<Record<ActivityKind, ReadonlySet<string>>> = {
  COMPANY_SOURCE_ENROLLED: new Set(['Added a direct employer source']),
  COMPANY_SOURCE_REFRESHED: new Set(['Refreshed the reviewed employer feed.']),
  COMPANY_SOURCE_REFRESH_FAILED: new Set(['The source refresh did not complete; review monitoring status.']),
  COMPANY_SOURCE_MIGRATION_REVIEW: new Set(['Detected changed career infrastructure; the previous source was preserved for review.']),
  COMPANY_SOURCE_MONITORING_REVIEWED: new Set(['Saved the reviewed source monitoring policy.']),
};

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function timestamp(value: unknown): string | null {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null;
  const milliseconds = Date.parse(value);
  return Number.isFinite(milliseconds) ? new Date(milliseconds).toISOString() : null;
}

function safeName(value: string): boolean {
  return value.length > 0 && value.length <= 80 && /^[\p{L}\p{N}][\p{L}\p{N} .&'()_-]*$/u.test(value);
}

function summaryFor(kind: unknown, summary: unknown): string | null {
  if (typeof kind !== 'string' || typeof summary !== 'string' || summary.length > 220 || /[\r\n\t\u0000-\u001f\u007f<>]/.test(summary)) return null;
  if (FIXED_SUMMARIES[kind as ActivityKind]?.has(summary)) return summary;

  if (kind === 'COMPANY_DISCOVERED') {
    const match = /^Discovered (.+?)(?: from (.+))?$/.exec(summary);
    return match && safeName(match[1]) && (match[2] === undefined || safeName(match[2])) ? summary : null;
  }
  if (kind === 'COMPANY_DOMAIN_RESOLVED') return summary === 'Resolved official domain' ? summary : null;
  if (kind === 'COMPANY_ATS_DETECTED') {
    const match = /^Detected (.+)$/.exec(summary);
    return match && safeName(match[1]) ? summary : null;
  }
  if (kind === 'FEED_IMPORT_COMPLETE') {
    if (summary === 'Feed import completed.') return summary;
    const match = /^Imported (0|[1-9]\d{0,9}) active openings$/.exec(summary);
    return match && Number(match[1]) <= 1_000_000_000 ? summary : null;
  }
  if (kind === 'COMPANY_FEED_MATCHES') {
    const match = /^(0|[1-9]\d{0,9}) openings match this profile$/.exec(summary);
    return match && Number(match[1]) <= 1_000_000_000 ? summary : null;
  }
  return null;
}

function recordedSourceSummary(row: Record<string, unknown>): string | null {
  if (row.action !== 'COMPANY_FEED_IMPORT_COMPLETE') return null;
  const details = record(row.details);
  const imported = details?.jobs_imported;
  const enumerated = details?.jobs_enumerated;
  const isCount = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0 && Number(value) <= 1_000_000_000;
  if (!isCount(imported) || !isCount(enumerated)) return 'Feed import completed.';
  return `Imported ${imported} jobs from ${enumerated} feed records.`;
}

/**
 * The server returns profile-scoped events newest first. Preserve that stable
 * order, including equal timestamps. Prefer the sanitized summary contract;
 * the legacy source provenance contract is reduced through one allowlisted
 * action and bounded counts. Ignore all remaining raw fields.
 */
export function discoveryActivity(events: unknown): DiscoveryActivity[] {
  if (!Array.isArray(events)) return [];
  const output: DiscoveryActivity[] = [];
  for (const candidate of events.slice(0, 20)) {
    const row = record(candidate);
    if (!row || typeof row.id !== 'string' || !row.id || row.id.length > 128) continue;
    const message = summaryFor(row.event_type, row.summary) ?? recordedSourceSummary(row);
    if (!message) continue;
    output.push({id: row.id, message, timestamp: timestamp(row.timestamp)});
  }
  return output;
}
