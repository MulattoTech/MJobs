import React, { useEffect, useState } from 'react';

/** Search-profile fields supported by the inexpensive title/keyword pass. */
export type JobPrefilterCriteria = {
  include_titles: string[];
  exclude_titles: string[];
  required_keywords: string[];
  excluded_keywords: string[];
  exact_phrases: string[];
};

/** Counts are included only when the API actually ran that pipeline stage. */
export type JobPrefilterStageCounts = {
  captured: number;
  prefiltered: number;
};

/** Sanitized preview response: rows remain in the source catalogue. */
export type JobPrefilterResponse = {
  stage_counts: JobPrefilterStageCounts;
  rejected_count?: number;
  deferred_count?: number;
};

export type JobPrefilterProps = {
  criteria: JobPrefilterCriteria;
  disabled?: boolean;
  onChange: (criteria: JobPrefilterCriteria) => void;
  onPreview: (criteria: JobPrefilterCriteria) => Promise<JobPrefilterResponse>;
  result?: JobPrefilterResponse | null;
};

const criteriaFields = [
  ['include_titles', 'Included job titles'],
  ['exclude_titles', 'Excluded job titles'],
  ['required_keywords', 'Required keywords'],
  ['excluded_keywords', 'Excluded keywords'],
  ['exact_phrases', 'Exact phrases'],
] as const satisfies ReadonlyArray<readonly [keyof JobPrefilterCriteria, string]>;

export function parseJobPrefilterList(input: string): string[] {
  const seen = new Set<string>();
  return input.split(/[\n,]/).map(value => value.trim()).filter(value => {
    if (!value || seen.has(value.toLocaleLowerCase())) return false;
    seen.add(value.toLocaleLowerCase());
    return true;
  });
}

function count(value: number) {
  return new Intl.NumberFormat('en-US').format(value);
}

export function JobPrefilter({ criteria, disabled = false, onChange, onPreview, result }: JobPrefilterProps) {
  const [drafts, setDrafts] = useState(() => ({
    include_titles: criteria.include_titles.join('\n'),
    exclude_titles: criteria.exclude_titles.join('\n'),
    required_keywords: criteria.required_keywords.join('\n'),
    excluded_keywords: criteria.excluded_keywords.join('\n'),
    exact_phrases: criteria.exact_phrases.join('\n'),
  }));
  useEffect(() => {
    setDrafts(previous => {
      const next = {...previous};
      let changed = false;
      for (const [field] of criteriaFields) {
        // Our own parsed onChange echoes must not erase a trailing newline
        // or comma. A different saved search still replaces its field values.
        if (JSON.stringify(parseJobPrefilterList(previous[field])) !== JSON.stringify(criteria[field])) {
          next[field] = criteria[field].join('\n');
          changed = true;
        }
      }
      return changed ? next : previous;
    });
  }, [criteria]);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState('');
  const [latestResult, setLatestResult] = useState<JobPrefilterResponse | null>(null);
  const displayedResult = result === undefined ? latestResult : result;

  function update(field: keyof JobPrefilterCriteria, value: string) {
    setDrafts(previous => ({...previous, [field]: value}));
    onChange({ ...criteria, [field]: parseJobPrefilterList(value) });
  }

  async function preview() {
    setPreviewing(true);
    setPreviewError('');
    try {
      setLatestResult(await onPreview({
        include_titles: [...criteria.include_titles],
        exclude_titles: [...criteria.exclude_titles],
        required_keywords: [...criteria.required_keywords],
        excluded_keywords: [...criteria.excluded_keywords],
        exact_phrases: [...criteria.exact_phrases],
      }));
    } catch (failure) {
      setPreviewError(String(failure));
    } finally {
      setPreviewing(false);
    }
  }

  return <section aria-label="Cheap job title and keyword prefilter">
    <h3>Title and keyword prefilter</h3>
    <p>Captured postings are retained. These filters only narrow current candidate work.</p>
    <p className="muted">Enter one value per line or separate values with commas. Blank lists do not restrict results.</p>
    {criteriaFields.map(([field, label]) => <label key={field}>{label}
      <textarea aria-label={label} value={drafts[field]} disabled={disabled || previewing}
        onChange={event => update(field, event.target.value)} />
    </label>)}
    <button type="button" disabled={disabled || previewing} onClick={() => void preview()}>
      {previewing ? 'Filtering candidates…' : 'Preview title and keyword filter'}
    </button>
    {previewError && <p role="alert">Could not preview candidates: {previewError}</p>}
    {displayedResult && <section aria-label="Prefilter stage counts" aria-live="polite">
      <h4>Current candidate work</h4>
      <p>Captured: {count(displayedResult.stage_counts.captured)}</p>
      <p>Prefiltered candidates: {count(displayedResult.stage_counts.prefiltered)}</p>
      {displayedResult.rejected_count !== undefined && <p>Rejected by prefilter: {count(displayedResult.rejected_count)}</p>}
      {displayedResult.deferred_count !== undefined && <p>Deferred by candidate limit: {count(displayedResult.deferred_count)}</p>}
    </section>}
  </section>;
}
