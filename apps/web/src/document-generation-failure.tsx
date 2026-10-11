import React from 'react';

export const DOCUMENT_GENERATION_FAILURE_CODE = 'DOCUMENT_GENERATION_FAILED';

export interface DocumentGenerationFailureRetry {
  operationStatus: string;
  recovery: string;
  safeToRetry: boolean;
  onRetry: () => void;
}

export interface DocumentGenerationFailureProps {
  /** Supply the durable operation state and callback only for a safe dead-letter retry. */
  retry?: DocumentGenerationFailureRetry;
}

export function DocumentGenerationFailure({ retry }: DocumentGenerationFailureProps) {
  const canRetry = retry?.operationStatus === 'DEAD_LETTERED'
    && retry.recovery === 'RETRY'
    && retry.safeToRetry === true
    && typeof retry.onRetry === 'function';

  return (
    <section className="panel" aria-labelledby="document-generation-failure-title">
      <h2 id="document-generation-failure-title">Document generation failed</h2>
      <div className="alert" role="alert">
        <p><strong>{DOCUMENT_GENERATION_FAILURE_CODE}</strong></p>
        <p>No complete document was produced. Do not use or submit an incomplete file.</p>
      </div>
      {canRetry ? (
        <button type="button" onClick={retry.onRetry}>Retry document generation</button>
      ) : (
        <p>Review the source details before starting another generation.</p>
      )}
    </section>
  );
}
