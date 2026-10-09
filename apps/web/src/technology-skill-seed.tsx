import React from 'react';

export const DEFAULT_TECHNOLOGY_SKILLS = [
  { id: 'ACTIVE_DIRECTORY', label: 'Active Directory' },
  { id: 'WINDOWS_SERVER', label: 'Windows Server' },
  { id: 'POWERSHELL', label: 'PowerShell' },
  { id: 'MICROSOFT_365', label: 'Microsoft 365' },
  { id: 'ENTRA_ID', label: 'Entra ID' },
  { id: 'AZURE', label: 'Azure' },
  { id: 'GROUP_POLICY', label: 'Group Policy' },
  { id: 'VMWARE', label: 'VMware' },
  { id: 'HYPER_V', label: 'Hyper-V' },
  { id: 'DNS', label: 'DNS' },
  { id: 'DHCP', label: 'DHCP' },
  { id: 'INTUNE', label: 'Intune' },
  { id: 'MECM', label: 'SCCM' },
  { id: 'RUBRIK', label: 'Rubrik' },
  { id: 'SAN', label: 'SAN' },
  { id: 'NAS', label: 'NAS' },
] as const;

const labelById: Map<string, string> = new Map(DEFAULT_TECHNOLOGY_SKILLS.map(skill => [skill.id, skill.label]));

export function displayTechnologySkillPreferences(values: string[] | undefined): string[] {
  return (values || []).map(value => labelById.get(value) || value);
}

export function TechnologySkillSeed({
  values,
  disabled = false,
  onChange,
}: {
  values: string[];
  disabled?: boolean;
  onChange: (values: string[]) => void;
}) {
  const selected = new Set(values);
  return <section className="panel" aria-label="Optional technology preferences">
    <h3>Optional technology preferences</h3>
    <p>These are opt-in search preferences, not verified career facts. Optional skill matches are advisory and do not hide jobs without these skills.</p>
    <div className="toolbar">
      <button type="button" disabled={disabled} onClick={() => onChange(
        DEFAULT_TECHNOLOGY_SKILLS.map(skill => skill.label))}>
        Use default technology seed
      </button>
      <button type="button" className="quiet" disabled={disabled || values.length === 0}
        onClick={() => onChange([])}>Clear technology preferences</button>
    </div>
    <div className="two-col">
      {DEFAULT_TECHNOLOGY_SKILLS.map(skill => <label key={skill.id}>
        <input type="checkbox" aria-label={skill.label} disabled={disabled}
          checked={selected.has(skill.label)} onChange={event => {
            const next = new Set(values);
            if (event.currentTarget.checked) next.add(skill.label);
            else next.delete(skill.label);
            onChange([...next]);
          }}/>
        {skill.label}
      </label>)}
    </div>
  </section>;
}
