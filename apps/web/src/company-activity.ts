/** Present only recorded source activity; missing or corrupt counts are never invented. */
export type DiscoveryActivity = {id: string; message: string; timestamp: string | null};
function record(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}
function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 && value <= 1_000_000_000
    ? value : null;
}
function eventTime(value: unknown): string | null {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return null;
  const milliseconds=Date.parse(value);
  return Number.isFinite(milliseconds) ? new Date(milliseconds).toISOString() : null;
}
export function discoveryActivity(events: unknown): DiscoveryActivity[] {
  if (!Array.isArray(events)) return [];
  const output: DiscoveryActivity[]=[];
  for (const candidate of events.slice(0,20)) {
    const row=record(candidate);
    if (!row || typeof row.id !== 'string' || !row.id) continue;
    const details=record(row.details) || {};
    let message: string;
    if (row.action === 'COMPANY_SOURCE_DISCOVERED') {
      const provider=typeof details.provider === 'string' && /^[a-z0-9_-]{1,50}$/i.test(details.provider)
        ? ` (${details.provider})` : '';
      message=`Added a careers source${provider}.`;
    } else if (row.action === 'COMPANY_FEED_IMPORT_COMPLETE') {
      const imported=count(details.jobs_imported), enumerated=count(details.jobs_enumerated);
      if (imported === null) message='Feed import recorded; job count unavailable.';
      else if (enumerated !== null && imported > enumerated) message='Feed import recorded; inconsistent counts need review.';
      else message=`Imported ${imported} jobs${enumerated === null ? '' : ` from ${enumerated} feed records`}.`;
    } else if (row.action === 'COMPANY_SOURCE_REFRESHED') {
      message='Refreshed the reviewed employer feed.';
    } else if (row.action === 'COMPANY_SOURCE_REFRESH_FAILED') {
      message='The source refresh did not complete; review monitoring status.';
    } else if (row.action === 'COMPANY_SOURCE_MIGRATION_REVIEW') {
      message='Detected changed career infrastructure; the previous source was preserved for review.';
    } else if (row.action === 'COMPANY_SOURCE_MONITORING_REVIEWED') {
      message='Saved the reviewed source monitoring policy.';
    } else continue;
    output.push({id:row.id.slice(0,128), message, timestamp:eventTime(row.timestamp)});
  }
  return output;
}
