export type ApplicationFormReviewField = {
  name: string;
  type: string;
  required: boolean;
  reviewRequired: boolean;
};

export type ApplicationFormReviewProps = {
  status: string;
  fields: readonly ApplicationFormReviewField[];
  onInspect: () => void;
};

const SAFE_FIELD_TYPES = new Set([
  'text', 'email', 'tel', 'number', 'url', 'textarea', 'select', 'radio',
  'checkbox', 'file', 'date', 'datetime-local', 'password', 'other',
]);

function safeFieldName(value: string): string {
  const withoutContactValues = value
    .replace(/[\p{L}\p{N}._%+-]+@[\p{L}\p{N}.-]+\.[\p{L}]{2,}/giu, ' [redacted] ')
    .replace(/\+?\d[\d\s().-]{6,}\d/gu, ' [redacted] ');
  const cleaned = withoutContactValues
    .replace(/[^\p{L}\p{N}_ -]/gu, ' ')
    .replace(/\s+/gu, ' ')
    .trim()
    .slice(0, 80);
  return cleaned || 'Unnamed field';
}

function safeFieldType(value: string): string {
  const normalized = value.trim().toLowerCase();
  return SAFE_FIELD_TYPES.has(normalized) ? normalized : 'Other';
}

export function ApplicationFormReview({
  status,
  fields,
  onInspect,
}: ApplicationFormReviewProps) {
  const knownStatus = ['idle', 'pending', 'completed', 'unavailable', 'error'].includes(status);
  const isPending = status === 'pending';
  const canInspect = status === 'idle' || status === 'completed' || status === 'error';

  return (
    <section className="panel" aria-label="Application form review">
      <h2>Application form review</h2>
      <p className="muted">
        Inspect the current form structure in read-only mode. Field values are never displayed or changed.
      </p>

      {status === 'pending' && (
        <p role="status" aria-live="polite">Inspecting the application form. No values will be stored or submitted.</p>
      )}
      {status === 'unavailable' && (
        <p role="status" aria-live="polite">
          Form inspection is unavailable for this application. Review the form manually in its original window.
        </p>
      )}
      {(status === 'error' || !knownStatus) && (
        <p role="alert" aria-live="assertive">
          Inspection could not be completed. Try again. No form values or raw error details were shown.
        </p>
      )}
      {status === 'completed' && (
        <div aria-label="Inspected form fields">
          <h3>Form fields</h3>
          {fields.length === 0 ? (
            <p>No field metadata is available for review.</p>
          ) : (
            <ul>
              {fields.map((field, index) => (
                <li key={`${index}-${field.name}`}>
                  <strong>{safeFieldName(field.name)}</strong>
                  <dl>
                    <dt>Type</dt><dd>{safeFieldType(field.type)}</dd>
                    <dt>Requirement</dt><dd>{field.required ? 'Required' : 'Optional'}</dd>
                    <dt>Review</dt><dd>{field.reviewRequired ? 'Review required' : 'No additional review flagged'}</dd>
                  </dl>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <button type="button" className="primary" disabled={!canInspect || isPending} onClick={onInspect}>
        Inspect form fields (read-only)
      </button>
    </section>
  );
}
