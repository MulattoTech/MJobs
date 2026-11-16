/** Honest read-only source health summary (MJ-REQ-181).
 *
 * Some requested metrics are not yet persisted per source. Never infer counts,
 * durations, or scheduling guarantees from unrelated job-search operations.
 */
import React from 'react';
import type { CompanySourceView } from './company-sources';

export type GlobalSourceHealth = {
  id: string;
  name: string;
  provider: string;
  strategy: string;
  enabled: boolean;
  health_status: string;
  last_success: string | null;
  last_attempt?: string | null;
  last_error?: string | null;
  consecutive_failures?: number;
  backoff_until?: string | null;
  jobs_seen: number;
};

export type CompanySourceHealth = Pick<CompanySourceView,
  'id' | 'company_name' | 'provider' | 'collection_strategy' | 'enabled' |
  'status' | 'jobs_found' | 'last_success' | 'next_check' | 'monitoring'>;

type Props = {
  globalSources: GlobalSourceHealth[];
  companySources: CompanySourceHealth[];
};

type HealthRow = {
  id: string;
  name: string;
  type: string;
  strategy: string;
  health: string;
  lastSuccess: string;
  lastFailure: string;
  jobsSeen: string;
  newJobs: string;
  duplicates: string;
  duration: string;
  nextRun: string;
};

const unrecorded = 'Not recorded';

function timestamp(value: string | null | undefined): string {
  if (!value) return unrecorded;
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.valueOf())) return unrecorded;
  return parsed.toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
}

function count(value: number): string {
  return Number.isSafeInteger(value) && value >= 0 ? String(value) : unrecorded;
}

function globalRows(sources: GlobalSourceHealth[]): HealthRow[] {
  return sources.map(source => ({
    id: source.id,
    name: source.name,
    type: 'Global',
    strategy: source.strategy || unrecorded,
    health: source.enabled ? (source.health_status || 'Unknown') : 'Disabled',
    lastSuccess: timestamp(source.last_success),
    lastFailure: source.consecutive_failures && source.consecutive_failures > 0
      ? timestamp(source.last_attempt) : unrecorded,
    jobsSeen: count(source.jobs_seen),
    newJobs: unrecorded,
    duplicates: unrecorded,
    duration: unrecorded,
    nextRun: !source.enabled ? 'Paused'
      : source.backoff_until ? 'Earliest retry ' + timestamp(source.backoff_until)
      : 'Schedule not exposed',
  }));
}

function companyRows(sources: CompanySourceHealth[]): HealthRow[] {
  return sources.map(source => ({
    id: source.id,
    name: source.company_name,
    type: 'Company',
    strategy: source.collection_strategy || unrecorded,
    health: source.enabled ? (source.status || 'Unknown') : 'Disabled',
    lastSuccess: timestamp(source.last_success),
    lastFailure: source.monitoring?.status === 'FAILED'
      ? timestamp(source.monitoring.last_attempt) : unrecorded,
    jobsSeen: count(source.jobs_found) === unrecorded
      ? unrecorded : count(source.jobs_found) + ' (currently found)',
    newJobs: unrecorded,
    duplicates: unrecorded,
    duration: unrecorded,
    nextRun: !source.enabled || source.monitoring?.enabled === false ? 'Paused'
      : source.next_check ? timestamp(source.next_check)
      : source.monitoring?.next_due ? timestamp(source.monitoring.next_due)
      : 'Not scheduled',
  }));
}

const columns = [
  ['name', 'Source'], ['type', 'Type'], ['strategy', 'Strategy'],
  ['health', 'Health'], ['lastSuccess', 'Last Success'],
  ['lastFailure', 'Last Failure'], ['jobsSeen', 'Jobs Seen'],
  ['newJobs', 'New Jobs'], ['duplicates', 'Duplicates'],
  ['duration', 'Duration'], ['nextRun', 'Next Run'],
] as const;

function HealthTable({label, rows, empty}: {label: string; rows: HealthRow[]; empty: string}) {
  return <section aria-label={label + ' health'}>
    <h3>{label}</h3>
    {rows.length === 0 ? <p role="status">{empty}</p> :
      <div style={{overflowX: 'auto'}}>
        <table aria-label={label + ' health table'}>
          <thead><tr>{columns.map(([, title]) =>
            <th key={title} scope="col">{title}</th>)}</tr></thead>
          <tbody>{rows.map(row =>
            <tr key={row.id}>{columns.map(([key]) =>
              <td key={key}>{row[key]}</td>)}</tr>)}</tbody>
        </table>
      </div>}
  </section>;
}

export function SourceHealthDashboard({globalSources, companySources}: Props) {
  return <section className="panel" aria-label="Source health dashboard">
    <h2>Source health dashboard</h2>
    <p className="muted">Read-only snapshot from configured sources. Unmeasured fields are not zero,
      and an earliest retry is not a promised next run.</p>
    <HealthTable label="Global Sources" rows={globalRows(globalSources)}
      empty="No global sources configured."/>
    <HealthTable label="Company Sources" rows={companyRows(companySources)}
      empty="No company sources enrolled."/>
    <p className="muted">Global Jobs Seen is the configured collector counter; company Jobs Seen
      shows currently discovered jobs only. New Jobs, Duplicates, and Duration are not
      recorded per source. Historical failures without a supported timestamp stay unknown.</p>
  </section>;
}
