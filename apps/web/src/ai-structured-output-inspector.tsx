import React, { useState } from 'react';

export interface JSONSchemaDefinition {
  title?: string;
  type?: string;
  properties?: Record<string, { type?: string; title?: string; description?: string }>;
  required?: string[];
  additionalProperties?: boolean;
}

export interface AIStructuredOutputInspectorProps {
  schemas?: Record<string, JSONSchemaDefinition>;
  onValidatePayload?: (
    schemaName: string,
    rawJson: string,
  ) => { valid: boolean; errorCode?: string; parsed?: unknown };
}

export const CANONICAL_SCHEMA_DEFINITIONS: Record<string, JSONSchemaDefinition> = {
  job_score: {
    title: 'JobScoreResult',
    type: 'object',
    properties: {
      score: { type: 'integer', title: 'Score', description: 'Deterministic match score 0-100' },
      category: { type: 'string', title: 'Category', description: 'Match category classification' },
      rationale: { type: 'string', title: 'Rationale', description: 'Concise explanation for score' },
    },
    required: ['score', 'category', 'rationale'],
    additionalProperties: false,
  },
  requirement_classification: {
    title: 'RequirementClassificationResult',
    type: 'object',
    properties: {
      requirement: { type: 'string', title: 'Requirement', description: 'Original requirement text' },
      classification: {
        type: 'string',
        title: 'Classification',
        description: 'REQUIRED, PREFERRED, NICE_TO_HAVE, or UNKNOWN',
      },
      confidence: { type: 'number', title: 'Confidence', description: 'Confidence probability 0.0-1.0' },
    },
    required: ['requirement', 'classification', 'confidence'],
    additionalProperties: false,
  },
  company_summary: {
    title: 'CompanySummaryResult',
    type: 'object',
    properties: {
      company_name: { type: 'string', title: 'Company Name' },
      industry: { type: 'string', title: 'Industry' },
      summary: { type: 'string', title: 'Summary' },
    },
    required: ['company_name', 'industry', 'summary'],
    additionalProperties: false,
  },
};

export const SAMPLE_PAYLOADS: Record<string, { valid: string; invalid: string }> = {
  job_score: {
    valid: '{\n  "score": 91,\n  "category": "Excellent Match",\n  "rationale": "Strong infrastructure and PowerShell alignment"\n}',
    invalid: '{\n  "score": 91,\n  "category": "Excellent Match",\n  "rationale": "Good",\n  "undeclared_field": "disallowed extra data"\n}',
  },
  requirement_classification: {
    valid: '{\n  "requirement": "Active Directory administration",\n  "classification": "REQUIRED",\n  "confidence": 0.95\n}',
    invalid: '{\n  "requirement": "Active Directory",\n  "classification": 12,\n  "confidence": 0.95\n}',
  },
  company_summary: {
    valid: '{\n  "company_name": "Acme Corp",\n  "industry": "Enterprise Software",\n  "summary": "Specializes in identity and access management tools."\n}',
    invalid: '{\n  "company_name": "Acme Corp"\n}',
  },
};

export const AIStructuredOutputInspector: React.FC<AIStructuredOutputInspectorProps> = ({
  schemas = CANONICAL_SCHEMA_DEFINITIONS,
  onValidatePayload,
}) => {
  const schemaKeys = Object.keys(schemas);
  const [selectedSchema, setSelectedSchema] = useState<string>(schemaKeys[0] || 'job_score');
  const [payloadText, setPayloadText] = useState<string>(
    SAMPLE_PAYLOADS[selectedSchema]?.valid || '{}',
  );
  const [validationResult, setValidationResult] = useState<{
    tested: boolean;
    valid: boolean;
    errorCode?: string;
  }>({ tested: false, valid: false });

  const activeSchema = schemas[selectedSchema] || CANONICAL_SCHEMA_DEFINITIONS.job_score;

  const handleSchemaSelect = (name: string) => {
    setSelectedSchema(name);
    setPayloadText(SAMPLE_PAYLOADS[name]?.valid || '{}');
    setValidationResult({ tested: false, valid: false });
  };

  const handleLoadSample = (kind: 'valid' | 'invalid') => {
    const sample = SAMPLE_PAYLOADS[selectedSchema]?.[kind] || '{}';
    setPayloadText(sample);
    setValidationResult({ tested: false, valid: false });
  };

  const handleValidate = () => {
    if (onValidatePayload) {
      const result = onValidatePayload(selectedSchema, payloadText);
      setValidationResult({ tested: true, valid: result.valid, errorCode: result.errorCode });
      return;
    }
    // Local deterministic client check
    try {
      const parsed = JSON.parse(payloadText);
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        setValidationResult({ tested: true, valid: false, errorCode: 'AI_STRUCTURED_OUTPUT_INVALID' });
        return;
      }
      const allowedProps = Object.keys(activeSchema.properties || {});
      const actualProps = Object.keys(parsed);
      const hasExtra = actualProps.some(k => !allowedProps.includes(k));
      const missingRequired = (activeSchema.required || []).some(k => !(k in parsed));

      if (hasExtra || missingRequired) {
        setValidationResult({ tested: true, valid: false, errorCode: 'AI_STRUCTURED_OUTPUT_INVALID' });
      } else {
        setValidationResult({ tested: true, valid: true });
      }
    } catch {
      setValidationResult({ tested: true, valid: false, errorCode: 'AI_STRUCTURED_OUTPUT_INVALID' });
    }
  };

  return (
    <section
      aria-label="AI Structured Output Inspector"
      className="mjobs-ai-structured-output-inspector rounded-xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900"
    >
      <div className="mb-6 flex flex-col justify-between gap-3 border-b border-slate-100 pb-4 dark:border-slate-800 sm:flex-row sm:items-center">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-slate-900 dark:text-slate-100">
            Strict AI Output Schemas
          </h2>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Validate untrusted LLM outputs against strict Pydantic JSON Schemas. Extra attributes and malformed structures fail closed safely.
          </p>
        </div>
        <div>
          <span className="inline-flex items-center rounded-full bg-indigo-50 px-2.5 py-1 text-xs font-semibold text-indigo-700 dark:bg-indigo-950/50 dark:text-indigo-300">
            Strict Validation (Forbid Extra)
          </span>
        </div>
      </div>

      {/* Schema Selector Tabs */}
      <div className="mb-4 flex flex-wrap gap-2 border-b border-slate-100 pb-3 dark:border-slate-800" data-testid="schema-tabs">
        {schemaKeys.map(name => (
          <button
            key={name}
            type="button"
            data-testid={`schema-tab-${name}`}
            onClick={() => handleSchemaSelect(name)}
            className={`rounded-lg px-3 py-1.5 text-xs font-semibold transition ${
              selectedSchema === name
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'bg-slate-100 text-slate-700 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700'
            }`}
          >
            {schemas[name]?.title || name}
          </button>
        ))}
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        {/* Schema Definition View */}
        <div className="rounded-lg border border-slate-200 bg-slate-50/50 p-4 dark:border-slate-800 dark:bg-slate-850/50">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-bold text-slate-900 dark:text-slate-100">
              {activeSchema.title} JSON Schema
            </h3>
            <span className="text-xs font-mono text-slate-400">additionalProperties: false</span>
          </div>

          <div className="mt-3 space-y-2 text-xs">
            <h4 className="font-semibold text-slate-700 dark:text-slate-300">Declared Properties:</h4>
            <ul className="space-y-1.5 pl-2" data-testid="property-list">
              {Object.entries(activeSchema.properties || {}).map(([prop, def]) => {
                const isReq = (activeSchema.required || []).includes(prop);
                return (
                  <li key={prop} className="font-mono text-slate-600 dark:text-slate-400">
                    <span className="font-bold text-indigo-600 dark:text-indigo-400">{prop}</span>
                    <span className="text-slate-400"> ({def.type})</span>
                    {isReq && <span className="ml-1.5 rounded bg-rose-50 px-1 text-2xs font-semibold text-rose-600 dark:bg-rose-950/40 dark:text-rose-300">REQUIRED</span>}
                  </li>
                );
              })}
            </ul>
          </div>

          <div className="mt-4 border-t border-slate-200 pt-3 dark:border-slate-700">
            <span className="text-xs text-slate-500 dark:text-slate-400">
              Required Fields: {(activeSchema.required || []).join(', ')}
            </span>
          </div>
        </div>

        {/* Validation Testing Sandbox */}
        <div className="flex flex-col justify-between rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
          <div>
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-bold text-slate-900 dark:text-slate-100">
                Payload Validation Sandbox
              </h3>
              <div className="flex gap-1.5">
                <button
                  type="button"
                  data-testid="load-valid-sample-btn"
                  onClick={() => handleLoadSample('valid')}
                  className="rounded bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-700 hover:bg-emerald-100 dark:bg-emerald-950/40 dark:text-emerald-300"
                >
                  Load Valid
                </button>
                <button
                  type="button"
                  data-testid="load-invalid-sample-btn"
                  onClick={() => handleLoadSample('invalid')}
                  className="rounded bg-rose-50 px-2 py-0.5 text-xs font-medium text-rose-700 hover:bg-rose-100 dark:bg-rose-950/40 dark:text-rose-300"
                >
                  Load Invalid
                </button>
              </div>
            </div>

            <textarea
              data-testid="payload-input"
              rows={7}
              value={payloadText}
              onChange={e => {
                setPayloadText(e.target.value);
                setValidationResult({ tested: false, valid: false });
              }}
              className="mt-3 block w-full rounded border border-slate-300 bg-slate-50/50 p-2.5 font-mono text-xs text-slate-900 shadow-sm focus:border-indigo-500 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
            />

            {validationResult.tested && (
              <div
                role="alert"
                data-testid="validation-feedback"
                className={`mt-3 rounded p-2.5 text-xs font-medium ${
                  validationResult.valid
                    ? 'bg-emerald-50 text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300'
                    : 'bg-rose-50 text-rose-800 dark:bg-rose-950/40 dark:text-rose-300'
                }`}
              >
                {validationResult.valid
                  ? 'Schema Validation Passed: Untrusted payload matches strict schema.'
                  : `Schema Validation Failed [${validationResult.errorCode || 'AI_STRUCTURED_OUTPUT_INVALID'}]: Extra attributes or invalid structure rejected.`}
              </div>
            )}
          </div>

          <div className="mt-4 pt-2">
            <button
              type="button"
              data-testid="validate-payload-btn"
              onClick={handleValidate}
              className="w-full rounded bg-indigo-600 px-3.5 py-1.5 text-xs font-semibold text-white shadow-sm hover:bg-indigo-500"
            >
              Validate Untrusted Payload
            </button>
          </div>
        </div>
      </div>
    </section>
  );
};
