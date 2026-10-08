export type JobRouteProvenanceProps = {
  postingSources?: readonly (string | null | undefined)[] | null;
  routeProvider?: string | null;
};

function sourceLabel(value: string | null | undefined): string | null {
  if (typeof value !== 'string' || !value.trim()) return null;
  const trimmed = value.trim();
  if (!/^https?:\/\//i.test(trimmed)) return trimmed;

  try {
    const parsed = new URL(trimmed);
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) return null;
    const path = parsed.pathname === '/' ? '' : parsed.pathname;
    return `${parsed.hostname}${parsed.port ? `:${parsed.port}` : ''}${path}`;
  } catch {
    return null;
  }
}

function discoverySourceLabels(values: JobRouteProvenanceProps['postingSources']): string[] {
  const labels: string[] = [];
  const seen = new Set<string>();
  for (const value of values || []) {
    const label = sourceLabel(value);
    const key = label?.toLowerCase();
    if (label && key && !seen.has(key)) {
      seen.add(key);
      labels.push(label);
    }
  }
  return labels;
}

function applicationProviderLabel(value: string | null | undefined): string {
  return sourceLabel(value) || 'Unknown';
}

export function JobRouteProvenance({postingSources, routeProvider}: JobRouteProvenanceProps) {
  const sources = discoverySourceLabels(postingSources);
  return <span className="job-route-provenance" aria-label="Job route provenance">
    <span><strong>DISCOVERY SOURCE</strong> <span>{sources.length ? sources.join(', ') : 'Not recorded'}</span></span>
    <span><strong>APPLICATION PROVIDER</strong> <span>{applicationProviderLabel(routeProvider)}</span></span>
    <span className="muted">Route-provider metadata does not prove verified ATS support.</span>
  </span>;
}
