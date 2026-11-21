import React from 'react';

export type CompanyDiscoveryPolicyValue = {
  company_discovery_enabled?: boolean;
  minimum_match_for_discovery?: number;
  priority_match_for_discovery?: number;
  max_new_company_discoveries_per_hour?: number;
  employer_verification?: {
    version: 1;
    mode: 'DISABLED' | 'GREENHOUSE_RECIPROCAL_V1';
    activation_id: string | null;
  };
};

export type CompanyDiscoveryPolicyProps = {
  value: CompanyDiscoveryPolicyValue;
  onValueChange: (value: CompanyDiscoveryPolicyValue) => void;
  disabled?: boolean;
};

const DEFAULTS = {
  company_discovery_enabled: true,
  minimum_match_for_discovery: 60,
  priority_match_for_discovery: 80,
  max_new_company_discoveries_per_hour: 12,
} as const;

export function CompanyDiscoveryPolicy({ value, onValueChange, disabled = false }:CompanyDiscoveryPolicyProps) {
  const policy = { ...DEFAULTS, ...value };
  const verificationEnabled = policy.company_discovery_enabled
    && value.employer_verification?.version === 1
    && value.employer_verification.mode === 'GREENHOUSE_RECIPROCAL_V1';
  const update = (key:keyof typeof DEFAULTS, next:boolean|number) => {
    onValueChange({ ...value, [key]:next,
      ...(key === 'company_discovery_enabled' && next === false
        ? { employer_verification:{ version:1, mode:'DISABLED', activation_id:null } as const } : {}),
    });
  };
  const updateVerification = (enabled:boolean) => {
    onValueChange({ ...value, employer_verification:{
      version:1, mode:enabled ? 'GREENHOUSE_RECIPROCAL_V1' : 'DISABLED', activation_id:null,
    } });
  };

  return <section className="panel" aria-labelledby="company-discovery-policy-heading">
    <h2 id="company-discovery-policy-heading">Company discovery</h2>
    <p className="muted">These controls decide when the local discovery queue looks for company career sources. Priority scores move eligible work earlier in that queue. This never submits applications.</p>
    <label>
      <input type="checkbox" checked={policy.company_discovery_enabled} disabled={disabled}
        onChange={event => update('company_discovery_enabled', event.currentTarget.checked)}/>
      Enable company discovery
    </label>
    <label>
      <input type="checkbox" aria-label="Verify employer links from imported jobs"
        checked={verificationEnabled} disabled={disabled || !policy.company_discovery_enabled}
        onChange={event => updateVerification(event.currentTarget.checked)}/>
      Verify employer links from imported jobs
    </label>
    <p className="muted">Allow bounded public checks of Greenhouse links against the employer site after saving this profile. Site rules and existing collection limits still apply.</p>
    <div className="two-col">
      <label>Minimum match score for discovery
        <input aria-label="Minimum match score for discovery" type="number" min="0" max={policy.priority_match_for_discovery}
          value={policy.minimum_match_for_discovery} disabled={disabled}
          onChange={event => update('minimum_match_for_discovery', Number(event.currentTarget.value))}/>
        <span className="muted">A job meeting this score may request company-source discovery.</span>
      </label>
      <label>Match score for priority discovery
        <input aria-label="Match score for priority discovery" type="number" min={policy.minimum_match_for_discovery} max="100"
          value={policy.priority_match_for_discovery} disabled={disabled}
          onChange={event => update('priority_match_for_discovery', Number(event.currentTarget.value))}/>
        <span className="muted">Priority requests are processed ahead of normal queued discovery.</span>
      </label>
    </div>
    <label>Maximum new company discoveries per hour
      <input aria-label="Maximum new company discoveries per hour" type="number" min="1" max="100"
        value={policy.max_new_company_discoveries_per_hour} disabled={disabled}
        onChange={event => update('max_new_company_discoveries_per_hour', Number(event.currentTarget.value))}/>
      <span className="muted">Limits newly queued requests for this profile. Existing requests can still be coalesced.</span>
    </label>
  </section>;
}
