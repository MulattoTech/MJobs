import React, {useEffect, useState} from 'react';
import {parseJobPrefilterList} from './job-prefilter';

export type EmployerSearchCriteria = {company_include: string[]; company_exclude: string[]};

export function EmployerSearchRules({criteria, disabled, onSave}: {
  criteria: EmployerSearchCriteria;
  disabled: boolean;
  onSave: (criteria: EmployerSearchCriteria) => void;
}) {
  const [included,setIncluded]=useState(criteria.company_include.join('\n'));
  const [excluded,setExcluded]=useState(criteria.company_exclude.join('\n'));
  useEffect(()=>{setIncluded(criteria.company_include.join('\n'));setExcluded(criteria.company_exclude.join('\n'));},[criteria]);
  return <section aria-label="Saved employer rules">
    <h3>Employer rules</h3>
    <p>These rules apply to saved-search discovery and Jobs results. The title and keyword preview above does not preview employer rules.</p>
    <p className="muted">Enter employer names or name fragments, one per line or separated by commas. Any included name may match; excluded names take precedence. Blank lists allow all employers.</p>
    <label>Included employers<textarea aria-label="Included employers" disabled={disabled}
      value={included} onChange={event=>setIncluded(event.target.value)}/></label>
    <label>Excluded employers<textarea aria-label="Excluded employers" disabled={disabled}
      value={excluded} onChange={event=>setExcluded(event.target.value)}/></label>
    <button type="button" disabled={disabled} onClick={()=>onSave({
      company_include:parseJobPrefilterList(included),company_exclude:parseJobPrefilterList(excluded),
    })}>Save employer rules</button>
  </section>;
}
