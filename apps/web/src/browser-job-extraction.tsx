import { useState } from 'react';

const MAX_VISIBLE_TEXT_CHARS = 10_000;

export type BrowserCaptureDraft = {
  structuredCapture: string;
  selectedText: string;
  visibleText: string;
};

export type BrowserJobFieldName = 'title' | 'company' | 'location' | 'description' | 'application_url';
export type BrowserJobFieldSource =
  | 'JOB_POSTING_SCHEMA'
  | 'PROVIDER_STRUCTURED_DATA'
  | 'SELECTED_TEXT'
  | 'VISIBLE_TEXT_FALLBACK'
  | 'UNKNOWN';
export type BrowserJobFieldConfidence = 'HIGH' | 'MEDIUM' | 'LOW' | 'UNKNOWN';

export type BrowserExtractedJobField = {
  value: string | null;
  source: BrowserJobFieldSource;
  confidence: BrowserJobFieldConfidence;
  requires_confirmation: boolean;
};

export type BrowserJobExtractionResult = {
  fields: Record<BrowserJobFieldName, BrowserExtractedJobField>;
};

type FieldCandidate = Pick<BrowserExtractedJobField, 'value' | 'source' | 'confidence'>;
type JsonObject = Record<string, unknown>;

const FIELD_PRESENTATION: ReadonlyArray<readonly [BrowserJobFieldName, string]> = [
  ['title', 'Title'],
  ['company', 'Company'],
  ['location', 'Location'],
  ['description', 'Description'],
  ['application_url', 'Application route'],
];

function objectValue(value: unknown): JsonObject | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonObject
    : null;
}

function stringValue(value: unknown, maxLength = 20_000): string | null {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  return trimmed.length > 0 && trimmed.length <= maxLength ? trimmed : null;
}

function applicationUrl(value: unknown): string | null {
  const candidate = stringValue(value, 2_000);
  if (!candidate) return null;
  try {
    const parsed = new URL(candidate);
    if ((parsed.protocol !== 'https:' && parsed.protocol !== 'http:') || parsed.username || parsed.password) {
      return null;
    }
    return parsed.href;
  } catch {
    return null;
  }
}

function schemaObjects(value: unknown): JsonObject[] {
  if (Array.isArray(value)) return value.flatMap(schemaObjects);
  const object = objectValue(value);
  if (!object) return [];
  const graph = object['@graph'];
  const nested = [
    ...(Array.isArray(graph) ? graph : graph ? [graph] : []),
    ...(object.jobPosting ? [object.jobPosting] : []),
  ].flatMap(schemaObjects);
  return [object, ...nested];
}

function isJobPostingSchema(value: JsonObject): boolean {
  const types = Array.isArray(value['@type']) ? value['@type'] : [value['@type']];
  return types.some(type => typeof type === 'string' &&
    (type === 'JobPosting' || type.endsWith('/JobPosting') || type.endsWith('#JobPosting')));
}

function nestedText(value: unknown, ...keys: string[]): string | null {
  let current = value;
  for (const key of keys) {
    if (Array.isArray(current)) current = current[0];
    const object = objectValue(current);
    if (!object) return null;
    current = object[key];
  }
  if (Array.isArray(current)) current = current[0];
  const object = objectValue(current);
  return stringValue(object ? object.name : current);
}

function candidate(value: unknown, source: BrowserJobFieldSource, confidence: BrowserJobFieldConfidence,
                   validate: (value: unknown) => string | null = stringValue): FieldCandidate | undefined {
  const normalized = validate(value);
  return normalized ? { value: normalized, source, confidence } : undefined;
}

function schemaCandidates(object: JsonObject | undefined): Partial<Record<BrowserJobFieldName, FieldCandidate>> {
  if (!object) return {};
  const hiringOrganization = objectValue(object.hiringOrganization);
  const address = nestedText(object.jobLocation, 'address', 'addressLocality');
  const region = nestedText(object.jobLocation, 'address', 'addressRegion');
  const postal = nestedText(object.jobLocation, 'address', 'postalCode');
  const country = nestedText(object.jobLocation, 'address', 'addressCountry');
  const locations = [address, region, postal, country].filter((part): part is string => !!part);
  const locationValue = locations.length ? [...new Set(locations)].join(', ') :
    nestedText(object.jobLocation, 'name');
  const schemaDescription = stringValue(object.description);
  return {
    title: candidate(object.title, 'JOB_POSTING_SCHEMA', 'HIGH'),
    company: candidate(hiringOrganization?.name, 'JOB_POSTING_SCHEMA', 'HIGH'),
    location: candidate(locationValue, 'JOB_POSTING_SCHEMA', 'HIGH'),
    description: schemaDescription
      ? { value: schemaDescription, source: 'JOB_POSTING_SCHEMA', confidence: 'HIGH' }
      : undefined,
    application_url: candidate(object.applicationUrl ?? object.application_url ?? object.applyUrl,
      'JOB_POSTING_SCHEMA', 'HIGH', applicationUrl),
  };
}

function providerCandidates(root: JsonObject | undefined): Partial<Record<BrowserJobFieldName, FieldCandidate>> {
  const provider = objectValue(root?.provider_data);
  if (!provider) return {};
  return {
    title: candidate(provider.title ?? provider.job_title, 'PROVIDER_STRUCTURED_DATA', 'HIGH'),
    company: candidate(provider.company_name ?? provider.company ?? provider.employer,
      'PROVIDER_STRUCTURED_DATA', 'HIGH'),
    location: candidate(provider.location, 'PROVIDER_STRUCTURED_DATA', 'HIGH'),
    description: candidate(provider.description, 'PROVIDER_STRUCTURED_DATA', 'HIGH'),
    application_url: candidate(provider.application_url ?? provider.apply_url,
      'PROVIDER_STRUCTURED_DATA', 'HIGH', applicationUrl),
  };
}

function labeledTextCandidates(text: string, source: BrowserJobFieldSource,
                               confidence: BrowserJobFieldConfidence,
                               allowFirstLineTitle: boolean): Partial<Record<BrowserJobFieldName, FieldCandidate>> {
  const lines = text.slice(0, MAX_VISIBLE_TEXT_CHARS).split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  const find = (pattern: RegExp) => lines.map(line => line.match(pattern)?.[1]?.trim())
    .find((value): value is string => !!value);
  const urlValue = find(/^(?:application\s+url|application\s+link|apply(?:\s+url)?|apply\s+at)\s*:\s*(\S+)$/i);
  const titleLabeled = find(/^(?:(?:job\s+)?title|position)\s*:\s*(.+)$/i);
  const company = find(/^(?:company|employer|hiring\s+organization)\s*:\s*(.+)$/i);
  const location = find(/^(?:location|job\s+location)\s*:\s*(.+)$/i);
  const description = find(/^(?:description|job\s+description)\s*:\s*(.+)$/i);
  const fallbackTitle = allowFirstLineTitle && !titleLabeled ? lines[0] : null;
  return {
    title: candidate(titleLabeled ?? fallbackTitle, source, confidence),
    company: candidate(company, source, confidence),
    location: candidate(location, source, confidence),
    description: candidate(description, source, confidence),
    application_url: candidate(urlValue, source, confidence, applicationUrl),
  };
}

function unknownField(): BrowserExtractedJobField {
  return { value: null, source: 'UNKNOWN', confidence: 'UNKNOWN', requires_confirmation: false };
}

/** Deterministic, local-only cascade over text the user explicitly supplies. */
export function extractBrowserJobFields(draft: BrowserCaptureDraft): BrowserJobExtractionResult {
  let root: JsonObject | undefined;
  let captureObjects: JsonObject[] = [];
  if (draft.structuredCapture.trim()) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(draft.structuredCapture);
    } catch {
      throw new Error('Enter valid JSON for the structured capture.');
    }
    captureObjects = schemaObjects(parsed);
    root = objectValue(parsed) ?? undefined;
    if (!root && !captureObjects.length) throw new Error('Structured capture must be a JSON object or JobPosting list.');
  }

  const schema = captureObjects.find(isJobPostingSchema);
  const provider = providerCandidates(root);
  const selected = labeledTextCandidates(draft.selectedText, 'SELECTED_TEXT', 'MEDIUM', false);
  const visible = labeledTextCandidates(draft.visibleText, 'VISIBLE_TEXT_FALLBACK', 'LOW', true);
  const primary = schemaCandidates(schema);
  const fields = {} as Record<BrowserJobFieldName, BrowserExtractedJobField>;
  for (const [name] of FIELD_PRESENTATION) {
    const field = primary[name] || provider[name] || selected[name] || visible[name];
    const result: BrowserExtractedJobField = field
      ? { ...field, requires_confirmation: field.confidence === 'LOW' &&
          (name === 'title' || name === 'company' || name === 'application_url') }
      : unknownField();
    fields[name] = result;
  }
  return { fields };
}

export function browserJobExtractionPreviewButtonText(hasError: boolean) {
  return hasError ? 'Retry extraction preview' : 'Preview extracted fields';
}

export function previewBrowserCaptureDraft(draft: BrowserCaptureDraft) {
  try {
    return { draft, result: extractBrowserJobFields(draft), error: '' };
  } catch (error) {
    return {
      draft,
      result: null,
      error: error instanceof Error ? error.message : 'Could not preview captured fields.',
    };
  }
}

export type BrowserJobExtractionPanelProps = {
  draft: BrowserCaptureDraft;
  result?: BrowserJobExtractionResult | null;
  previewError?: string;
  confirmedFields?: BrowserJobFieldName[];
  onDraftChange: (draft: BrowserCaptureDraft) => void;
  onPreview: () => void;
  onConfirm: (fields: BrowserJobExtractionResult['fields']) => void;
  onConfirmField?: (field: BrowserJobFieldName, confirmed: boolean) => void;
};

function sourceLabel(source: BrowserJobFieldSource) {
  switch (source) {
    case 'JOB_POSTING_SCHEMA': return 'JobPosting schema';
    case 'PROVIDER_STRUCTURED_DATA': return 'Provider structured data';
    case 'SELECTED_TEXT': return 'Selected text';
    case 'VISIBLE_TEXT_FALLBACK': return 'Visible text fallback';
    default: return 'Unknown';
  }
}

export function BrowserJobExtractionPanel({
  draft,
  result = null,
  previewError = '',
  confirmedFields = [],
  onDraftChange,
  onPreview,
  onConfirm,
  onConfirmField,
}: BrowserJobExtractionPanelProps) {
  const pendingReview = FIELD_PRESENTATION.filter(([name]) =>
    !!result?.fields[name].requires_confirmation && !confirmedFields.includes(name));
  const canConfirm = !!result && pendingReview.length === 0;
  const update = (field: keyof BrowserCaptureDraft, value: string) =>
    onDraftChange({ ...draft, [field]: value });

  return <section aria-label="Browser job extraction preview">
    <h3>Review a locally captured job</h3>
    <p>Paste structured capture or text you selected. MJobs does not fetch or open the page.</p>
    <p className="muted">Page text is data only. Instructions inside captured text are never followed.</p>
    <label>Structured capture (JSON)
      <textarea aria-label="Structured capture (JSON)" maxLength={250_000}
        value={draft.structuredCapture} onChange={event => update('structuredCapture', event.target.value)} />
    </label>
    <label>Selected page text
      <textarea aria-label="Selected page text" maxLength={MAX_VISIBLE_TEXT_CHARS}
        value={draft.selectedText} onChange={event => update('selectedText', event.target.value)} />
    </label>
    <label>Visible page text fallback (up to {MAX_VISIBLE_TEXT_CHARS.toLocaleString()} characters)
      <textarea aria-label="Visible page text fallback" maxLength={MAX_VISIBLE_TEXT_CHARS}
        value={draft.visibleText} onChange={event => update('visibleText', event.target.value)} />
    </label>
    <button type="button" onClick={onPreview}>{browserJobExtractionPreviewButtonText(!!previewError)}</button>
    {previewError && <p role="alert">{previewError}</p>}
    {result && <>
      <details><summary>Original capture is retained locally</summary>
        <pre>{draft.structuredCapture}</pre><pre>{draft.selectedText}</pre><pre>{draft.visibleText}</pre>
      </details>
      <dl aria-label="Extracted job fields">
        {FIELD_PRESENTATION.map(([name, label]) => {
          const field = result.fields[name];
          return <div key={name}>
            <dt>{label}</dt>
            <dd>{field.value ?? 'Unknown'}</dd>
            <dd>{sourceLabel(field.source)} · {field.confidence} confidence</dd>
            {field.requires_confirmation && <label>
              <input type="checkbox" aria-label={`Confirm low-confidence ${name === 'application_url' ? 'application route' : name}`}
                checked={confirmedFields.includes(name)} onChange={event => onConfirmField?.(name, event.target.checked)} />
              Confirm low-confidence {name === 'application_url' ? 'application route' : name}
            </label>}
          </div>;
        })}
      </dl>
      <button type="button" disabled={!canConfirm} onClick={() => onConfirm(result.fields)}>
        Use reviewed job fields
      </button>
      {pendingReview.length > 0 && <p className="muted">Review each low-confidence title, company, and application route before continuing.</p>}
    </>}
  </section>;
}

export function BrowserJobExtraction({ onConfirm }: { onConfirm: BrowserJobExtractionPanelProps['onConfirm'] }) {
  const [draft, setDraft] = useState<BrowserCaptureDraft>({
    structuredCapture: '', selectedText: '', visibleText: '',
  });
  const [result, setResult] = useState<BrowserJobExtractionResult | null>(null);
  const [previewError, setPreviewError] = useState('');
  const [confirmedFields, setConfirmedFields] = useState<BrowserJobFieldName[]>([]);

  function preview() {
    setConfirmedFields([]);
    const previewResult = previewBrowserCaptureDraft(draft);
    setResult(previewResult.result);
    setPreviewError(previewResult.error);
  }

  function confirmField(field: BrowserJobFieldName, confirmed: boolean) {
    setConfirmedFields(previous => confirmed
      ? previous.includes(field) ? previous : [...previous, field]
      : previous.filter(value => value !== field));
  }

  return <BrowserJobExtractionPanel
    draft={draft}
    result={result}
    previewError={previewError}
    confirmedFields={confirmedFields}
    onDraftChange={value => { setDraft(value); setResult(null); setConfirmedFields([]); }}
    onPreview={preview}
    onConfirm={onConfirm}
    onConfirmField={confirmField}
  />;
}
