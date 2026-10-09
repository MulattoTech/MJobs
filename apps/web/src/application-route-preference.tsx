import React from 'react';

export type ApplicationRoutePreferenceValue = 'PREFER_VERIFIED' | 'USER_CHOICE';

export type ApplicationRoutePreferenceProps = {
  value: ApplicationRoutePreferenceValue | undefined;
  onValueChange: (value:ApplicationRoutePreferenceValue) => void;
  disabled?: boolean;
};

export function ApplicationRoutePreference({ value = 'PREFER_VERIFIED', onValueChange, disabled = false }:ApplicationRoutePreferenceProps) {
  return <fieldset className="panel" aria-labelledby="application-route-preference-heading">
    <legend id="application-route-preference-heading">Application route preference</legend>
    <p className="muted">A verified employer or ATS route is preferred when its identity and route evidence match. A provider name alone does not verify a route. This setting prepares documents only; it never submits an application.</p>
    <label>
      <input type="radio" name="application-route-preference" value="PREFER_VERIFIED"
        checked={value === 'PREFER_VERIFIED'} disabled={disabled}
        onChange={() => onValueChange('PREFER_VERIFIED')}/>
      Prefer a verified employer or ATS route when evidence supports it
    </label>
    <label>
      <input type="radio" name="application-route-preference" value="USER_CHOICE"
        checked={value === 'USER_CHOICE'} disabled={disabled}
        onChange={() => onValueChange('USER_CHOICE')}/>
      Ask me to choose a route for each job
    </label>
  </fieldset>;
}
