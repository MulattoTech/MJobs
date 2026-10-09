export type JobSourceOption = {
  id: string;
  name: string;
  provider: string;
  tenant: string;
};

type Props = {
  value: string;
  sources: JobSourceOption[];
  disabled: boolean;
  onChange: (sourceId: string) => void;
};

export function JobSourceFilter({ value, sources, disabled, onChange }: Props) {
  const knownSources = [...sources].sort((left, right) =>
    left.name.localeCompare(right.name) || left.provider.localeCompare(right.provider) ||
    left.tenant.localeCompare(right.tenant) || left.id.localeCompare(right.id));

  return <label>
    Job source
    <select aria-label="Job source" value={value} disabled={disabled}
      onChange={event => onChange(event.currentTarget.value)}>
      <option value="">All job sources</option>
      {knownSources.map(source => <option key={source.id} value={source.id}>
        {source.name} ({source.provider} · {source.tenant})
      </option>)}
    </select>
    <small>Filters saved jobs by their recorded collection source; it does not start or schedule collection.</small>
  </label>;
}
