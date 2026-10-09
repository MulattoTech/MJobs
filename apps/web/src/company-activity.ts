/** Render only the API's allowlisted summaries, never raw audit payloads. */
export type DiscoveryAttemptDetails = {
  company: string | null;
  triggers: string[];
  attemptState: string | null;
  attemptCount: number | null;
  atsProvider: string | null;
  confidence: number | null;
  confidenceBasis: 'ATS detection' | 'verified domain' | null;
  evidence: string[];
  sourceCreated: boolean | null;
  results: {jobsEnumerated: number | null; jobsImported: number | null; matchedJobs: number | null} | null;
  error: string | null;
};
export type DiscoveryActivity = {
  id: string; message: string; timestamp: string | null; details?: DiscoveryAttemptDetails;
};

type ActivityKind =
  | 'COMPANY_DISCOVERED'
  | 'COMPANY_DISCOVERY_ATTEMPT'
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
  COMPANY_DISCOVERY_ATTEMPT: new Set([
    'Company discovery waiting for review.',
    'Company discovery is waiting for review.',
    'Company discovery is in progress.',
    'Company discovery completed.',
    'Company discovery attempt needs review.',
  ]),
  COMPANY_SOURCE_ENROLLED: new Set(['Added a direct employer source']),
  COMPANY_SOURCE_REFRESHED: new Set(['Refreshed the reviewed employer feed.']),
  COMPANY_SOURCE_REFRESH_FAILED: new Set(['The source refresh did not complete; review monitoring status.']),
  COMPANY_SOURCE_MIGRATION_REVIEW: new Set(['Detected changed career infrastructure; the previous source was preserved for review.']),
  COMPANY_SOURCE_MONITORING_REVIEWED: new Set(['Saved the reviewed source monitoring policy.']),
};

const DISCOVERY_STATES = new Set([
  'NEEDS_REVIEW', 'PENDING', 'QUEUED', 'RUNNING', 'SUCCEEDED', 'COMPLETED',
  'FAILED', 'BLOCKED', 'DEAD_LETTERED', 'CANCELLED', 'DISCARDED', 'UNKNOWN',
]);
const DISCOVERY_PROVIDERS = new Set([
  'ADP Recruiting', 'Ashby', 'BambooHR', 'Breezy HR', 'Dayforce', 'Greenhouse',
  'iCIMS', 'Jobvite', 'JazzHR', 'Lever', 'the configured test ATS', 'Oracle Taleo',
  'Recruitee', 'SmartRecruiters', 'SAP SuccessFactors', 'Teamtailor', 'Workable', 'Workday',
]);
const DISCOVERY_ERRORS = new Set([
  'An official employer website could not be identified.',
  'No official employer website could be verified.',
  'The careers system needs a manual review.',
  'This careers system is not supported yet.',
  "The employer's public job feed could not be read.",
  'A requested page did not meet safe browsing rules.',
  'A page redirect did not meet safe browsing rules.',
  'The discovery attempt ended with an error that needs review.',
]);
const TRIGGER_LABELS = new Set(['Previously unseen company', 'High-match job']);
const EVIDENCE_LABELS = new Set(['ATS detected', 'Official domain verified']);

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
  if (kind === 'COMPANY_SOURCE_ENROLLED') {
    const match = /^Added direct employer source for (.+)$/.exec(summary);
    return match && safeName(match[1]) ? summary : null;
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

function attemptDetails(value: unknown): DiscoveryAttemptDetails {
  const input = record(value) ?? {};
  const company = typeof input.company === 'string' && safeName(input.company) ? input.company : null;
  const triggers = Array.isArray(input.triggers)
    ? [...new Set(input.triggers.filter((item): item is string =>
      typeof item === 'string' && TRIGGER_LABELS.has(item)))].slice(0, 2) : [];
  const attemptState = typeof input.attempt_state === 'string' && DISCOVERY_STATES.has(input.attempt_state)
    ? input.attempt_state : null;
  const attemptCount = Number.isSafeInteger(input.attempt_count) && Number(input.attempt_count) >= 0
    && Number(input.attempt_count) <= 1_000_000 ? Number(input.attempt_count) : null;
  const atsProvider = typeof input.ats_provider === 'string' && DISCOVERY_PROVIDERS.has(input.ats_provider)
    ? input.ats_provider : null;
  const confidenceBasis = input.confidence_basis === 'ATS detection' || input.confidence_basis === 'verified domain'
    ? input.confidence_basis : null;
  const confidence = typeof input.confidence === 'number' && Number.isFinite(input.confidence)
    && input.confidence >= 0 && input.confidence <= 1 && confidenceBasis ? input.confidence : null;
  const evidence = Array.isArray(input.evidence)
    ? [...new Set(input.evidence.filter((item): item is string =>
      typeof item === 'string' && EVIDENCE_LABELS.has(item)))].slice(0, 2) : [];
  const sourceCreated = typeof input.source_created === 'boolean' ? input.source_created : null;
  const resultInput = record(input.results);
  const resultCount = (value: unknown): number | null => Number.isSafeInteger(value)
    && Number(value) >= 0 && Number(value) <= 1_000_000_000 ? Number(value) : null;
  const jobsEnumerated = resultCount(resultInput?.jobs_enumerated);
  const jobsImported = resultCount(resultInput?.jobs_imported);
  const matchedJobs = resultCount(resultInput?.matched_jobs);
  const results = jobsEnumerated !== null || jobsImported !== null || matchedJobs !== null
    ? {jobsEnumerated, jobsImported, matchedJobs} : null;
  const error = typeof input.error === 'string' && DISCOVERY_ERRORS.has(input.error) ? input.error : null;
  return {company, triggers, attemptState, attemptCount, atsProvider, confidence,
    confidenceBasis: confidence === null ? null : confidenceBasis, evidence, sourceCreated, results, error};
}

/**
 * The server returns profile-scoped events newest first. Preserve that stable
 * order, including equal timestamps. Prefer the sanitized summary contract;
 * the legacy source provenance contract is reduced through one allowlisted
 * action and bounded counts. Ignore all remaining raw fields.
 */
export function discoveryActivity(events: unknown, maxRows = 20): DiscoveryActivity[] {
  if (!Array.isArray(events)) return [];
  const limit = Number.isSafeInteger(maxRows) && maxRows > 0 ? Math.min(maxRows, 100) : 20;
  const output: DiscoveryActivity[] = [];
  for (const candidate of events.slice(0, limit)) {
    const row = record(candidate);
    if (!row || typeof row.id !== 'string' || !row.id || row.id.length > 128) continue;
    const message = summaryFor(row.event_type, row.summary) ?? recordedSourceSummary(row);
    if (!message) continue;
    output.push({
      id: row.id, message, timestamp: timestamp(row.timestamp),
      ...(row.event_type === 'COMPANY_DISCOVERY_ATTEMPT'
        ? {details: attemptDetails(row.details)} : {}),
    });
  }
  return output;
}
