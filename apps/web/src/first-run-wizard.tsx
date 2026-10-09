import React, { useState } from 'react';

export type FirstRunWizardStep = {
  id: string;
  label: string;
  description: string;
  complete: boolean;
  action: 'navigate' | 'acknowledge' | 'search';
  actionLabel: string;
  skipLabel?: string;
};

export type FirstRunWizardProps = {
  profileId: string;
  steps: FirstRunWizardStep[];
  sources: Array<{ id: string; name: string; provider: string }>;
  searches: Array<{ id: string; name: string }>;
  selectedSourceId: string;
  selectedSearchId: string;
  canRunSearch: boolean;
  busy: boolean;
  onNavigate: (stepId: string) => void;
  onAcknowledge: (stepId: string) => void;
  onSelectSource: (sourceId: string) => void;
  onSelectSearch: (searchId: string) => void;
  onRunSearch: () => void;
};

export function FirstRunWizard({
  profileId,
  steps,
  sources,
  searches,
  selectedSourceId,
  selectedSearchId,
  canRunSearch,
  busy,
  onNavigate,
  onAcknowledge,
  onSelectSource,
  onSelectSearch,
  onRunSearch,
}: FirstRunWizardProps) {
  const storageKey = `mjobs-first-run-wizard-hidden:${profileId || 'new-profile'}`;
  const [hidden, setHidden] = useState(() => localStorage.getItem(storageKey) === '1');
  const completed = steps.filter(step => step.complete).length;

  if (hidden) return <button className="quiet" onClick={() => {
    localStorage.removeItem(storageKey);
    setHidden(false);
  }}>Resume first-run setup ({completed}/{steps.length})</button>;

  return <section className="panel" aria-label="First-run setup">
    <div className="toolbar">
      <div>
        <h2>First-run setup</h2>
        <p>{completed} of {steps.length} steps complete. Progress follows saved product data and setup choices saved in this browser for this profile.</p>
      </div>
      <button className="quiet" onClick={() => {
        localStorage.setItem(storageKey, '1');
        setHidden(true);
      }}>Set up later</button>
    </div>
    <p className="safety"><strong>Manual review only.</strong> External submission stays off. MJobs will not submit applications for you.</p>
    <ol aria-label="First-run steps">
      {steps.map((step, index) => <li key={step.id}>
        <div>
          <strong>{index + 1}. {step.label}</strong>
          <p>{step.description}</p>
          <span className={'status ' + (step.complete ? 'good' : 'warn')}>
            {step.complete ? 'Complete' : 'Needs attention'}
          </span>
        </div>
        <div>
          {step.action === 'navigate' && <button disabled={busy} onClick={() => onNavigate(step.id)}>{step.actionLabel}</button>}
          {step.action === 'acknowledge' && !step.complete && <button disabled={busy} onClick={() => onAcknowledge(step.id)}>{step.actionLabel}</button>}
          {step.skipLabel && !step.complete && <button className="quiet" disabled={busy} onClick={() => onAcknowledge(step.id)}>{step.skipLabel}</button>}
          {step.action === 'search' && <>
            <label htmlFor="first-run-search-profile">First-run saved search profile</label>
            <select id="first-run-search-profile" value={selectedSearchId} onChange={event => onSelectSearch(event.target.value)} disabled={busy || !searches.length}>
                <option value="">Choose an enabled saved search</option>
                {searches.map(search => <option key={search.id} value={search.id}>{search.name}</option>)}
            </select>
            <label htmlFor="first-run-source">Enabled source for this search</label>
            <select id="first-run-source" value={selectedSourceId} onChange={event => onSelectSource(event.target.value)} disabled={busy || !sources.length}>
                <option value="">Choose an enabled source</option>
                {sources.map(source => <option key={source.id} value={source.id}>{source.name} ({source.provider})</option>)}
            </select>
            <button disabled={busy || !canRunSearch || !selectedSourceId || !selectedSearchId} onClick={onRunSearch}>{step.actionLabel}</button>
          </>}
        </div>
      </li>)}
    </ol>
  </section>;
}
