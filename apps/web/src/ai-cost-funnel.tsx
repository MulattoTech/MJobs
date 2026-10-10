import React, { useState } from 'react';

export interface AICostPolicyConfig {
  minimumScore: number;
  maxInputCandidates: number;
  maxAiCandidates: number;
  maxDocumentCandidates: number;
}

export interface FunnelStageMetric {
  stage: 'discovered' | 'deduplicated' | 'filtered' | 'scored' | 'ai_evaluated' | 'documents_generated';
  label: string;
  count?: number;
  cap?: number;
  threshold?: number;
  description: string;
}

export interface AICostFunnelProps {
  policy: AICostPolicyConfig;
  currentMetrics?: {
    receivedCount?: number;
    uniqueCount?: number;
    plausibleCount?: number;
    eligibleCount?: number;
    aiCandidateCount?: number;
    documentCandidateCount?: number;
  };
  onUpdatePolicy?: (updated: AICostPolicyConfig) => void;
  onResetDefaults?: () => void;
}

export const DEFAULT_COST_POLICY: AICostPolicyConfig = {
  minimumScore: 75,
  maxInputCandidates: 5000,
  maxAiCandidates: 75,
  maxDocumentCandidates: 20,
};

export function validateCostPolicy(policy: AICostPolicyConfig): string | null {
  if (
    !Number.isInteger(policy.minimumScore) ||
    policy.minimumScore < 0 ||
    policy.minimumScore > 100
  ) {
    return 'Minimum score threshold must be an integer between 0 and 100.';
  }
  if (
    !Number.isInteger(policy.maxInputCandidates) ||
    policy.maxInputCandidates < 1 ||
    policy.maxInputCandidates > 100000
  ) {
    return 'Maximum input candidates must be between 1 and 100,000.';
  }
  if (
    !Number.isInteger(policy.maxAiCandidates) ||
    policy.maxAiCandidates < 0 ||
    policy.maxAiCandidates > 100000
  ) {
    return 'Maximum AI candidates must be between 0 and 100,000.';
  }
  if (
    !Number.isInteger(policy.maxDocumentCandidates) ||
    policy.maxDocumentCandidates < 0 ||
    policy.maxDocumentCandidates > policy.maxAiCandidates
  ) {
    return 'Maximum document candidates cannot exceed the maximum AI candidate ceiling.';
  }
  return null;
}

export const AICostFunnelView: React.FC<AICostFunnelProps> = ({
  policy,
  currentMetrics,
  onUpdatePolicy,
  onResetDefaults,
}) => {
  const [minScore, setMinScore] = useState<number>(policy.minimumScore);
  const [maxInput, setMaxInput] = useState<number>(policy.maxInputCandidates);
  const [maxAi, setMaxAi] = useState<number>(policy.maxAiCandidates);
  const [maxDocs, setMaxDocs] = useState<number>(policy.maxDocumentCandidates);
  const [error, setError] = useState<string | null>(null);
  const [isSaved, setIsSaved] = useState<boolean>(false);

  const handleSave = () => {
    const draft: AICostPolicyConfig = {
      minimumScore: minScore,
      maxInputCandidates: maxInput,
      maxAiCandidates: maxAi,
      maxDocumentCandidates: maxDocs,
    };
    const validationError = validateCostPolicy(draft);
    if (validationError) {
      setError(validationError);
      setIsSaved(false);
      return;
    }
    setError(null);
    if (onUpdatePolicy) {
      onUpdatePolicy(draft);
      setIsSaved(true);
      setTimeout(() => setIsSaved(false), 3000);
    }
  };

  const handleReset = () => {
    setMinScore(DEFAULT_COST_POLICY.minimumScore);
    setMaxInput(DEFAULT_COST_POLICY.maxInputCandidates);
    setMaxAi(DEFAULT_COST_POLICY.maxAiCandidates);
    setMaxDocs(DEFAULT_COST_POLICY.maxDocumentCandidates);
    setError(null);
    if (onResetDefaults) {
      onResetDefaults();
    }
  };

  const stages: FunnelStageMetric[] = [
    {
      stage: 'discovered',
      label: '1. Inbound Discovery',
      count: currentMetrics?.receivedCount,
      cap: maxInput,
      description: 'Raw inbound job postings received across discovery sources.',
    },
    {
      stage: 'deduplicated',
      label: '2. Deterministic Deduplication',
      count: currentMetrics?.uniqueCount,
      description: 'Removes duplicate postings across title, company identity, and URL.',
    },
    {
      stage: 'filtered',
      label: '3. Plausible Pre-Filters',
      count: currentMetrics?.plausibleCount,
      description: 'Applies deterministic location, clearance, and hard requirements.',
    },
    {
      stage: 'scored',
      label: '4. Deterministic Score Gate',
      count: currentMetrics?.eligibleCount,
      threshold: minScore,
      description: `Jobs meeting or exceeding the transparent match threshold (${minScore}/100).`,
    },
    {
      stage: 'ai_evaluated',
      label: '5. AI Candidate Evaluation',
      count: currentMetrics?.aiCandidateCount,
      cap: maxAi,
      description: 'Advisory LLM evaluation strictly capped to avoid excessive API expenditure.',
    },
    {
      stage: 'documents_generated',
      label: '6. Review-Gated Documents',
      count: currentMetrics?.documentCandidateCount,
      cap: maxDocs,
      description: 'Tailored resume and cover letter generation strictly capped and review-gated.',
    },
  ];

  return (
    <section
      aria-label="AI Cost Controls Funnel"
      className="mjobs-ai-cost-funnel rounded-xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900"
    >
      <div className="mb-6 flex flex-col justify-between gap-3 border-b border-slate-100 pb-4 dark:border-slate-800 sm:flex-row sm:items-center">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-slate-900 dark:text-slate-100">
            AI Candidate Cost Funnel
          </h2>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Enforce multi-stage deterministic gating before invoking AI providers, bounding token volume and cost.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="inline-flex items-center rounded-full bg-emerald-50 px-2.5 py-1 text-xs font-semibold text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300">
            Deterministic First Policy Active
          </span>
        </div>
      </div>

      {/* Visual Funnel Representation */}
      <div className="space-y-3" data-testid="funnel-stage-container">
        {stages.map((stage, idx) => (
          <div
            key={stage.stage}
            data-testid={`funnel-stage-${stage.stage}`}
            className="relative flex flex-col justify-between rounded-lg border border-slate-200 bg-slate-50/50 p-4 transition-all dark:border-slate-800 dark:bg-slate-850/50 sm:flex-row sm:items-center"
          >
            <div className="flex-1">
              <div className="flex items-center gap-2">
                <span className="flex h-5 w-5 items-center justify-center rounded-full bg-indigo-100 text-xs font-bold text-indigo-700 dark:bg-indigo-900/60 dark:text-indigo-300">
                  {idx + 1}
                </span>
                <h3 className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                  {stage.label}
                </h3>
                {stage.threshold !== undefined && (
                  <span className="rounded bg-indigo-50 px-2 py-0.5 text-xs font-medium text-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-300">
                    Threshold: &ge; {stage.threshold}
                  </span>
                )}
                {stage.cap !== undefined && (
                  <span className="rounded bg-slate-200 px-2 py-0.5 text-xs font-medium text-slate-700 dark:bg-slate-700 dark:text-slate-200">
                    Cap: &le; {stage.cap.toLocaleString()}
                  </span>
                )}
              </div>
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                {stage.description}
              </p>
            </div>

            <div className="mt-2 sm:mt-0 sm:text-right">
              <div className="text-sm font-mono font-bold text-slate-800 dark:text-slate-200">
                {stage.count !== undefined ? stage.count.toLocaleString() : '—'}
              </div>
              <span className="text-xs text-slate-400 dark:text-slate-500">Candidates</span>
            </div>
          </div>
        ))}
      </div>

      {/* Threshold Configuration Controls */}
      <div className="mt-6 rounded-lg border border-slate-200 bg-slate-50/30 p-5 dark:border-slate-800 dark:bg-slate-900/40">
        <h3 className="text-sm font-bold text-slate-900 dark:text-slate-100">
          Configure Funnel Caps &amp; Thresholds
        </h3>
        <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
          Tune deterministic admission thresholds and batch size ceilings to bound LLM token usage.
        </p>

        <div className="mt-4 grid gap-4 sm:grid-cols-2 md:grid-cols-4">
          <div>
            <label
              htmlFor="min-score-input"
              className="block text-xs font-semibold text-slate-700 dark:text-slate-300"
            >
              Min Match Score (0–100)
            </label>
            <input
              id="min-score-input"
              data-testid="min-score-input"
              type="number"
              min={0}
              max={100}
              value={minScore}
              onChange={e => {
                setMinScore(parseInt(e.target.value, 10) || 0);
                setError(null);
              }}
              className="mt-1 block w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-mono text-slate-900 shadow-sm focus:border-indigo-500 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
            />
          </div>

          <div>
            <label
              htmlFor="max-input-candidates"
              className="block text-xs font-semibold text-slate-700 dark:text-slate-300"
            >
              Max Input Candidates
            </label>
            <input
              id="max-input-candidates"
              data-testid="max-input-candidates"
              type="number"
              min={1}
              max={100000}
              value={maxInput}
              onChange={e => {
                setMaxInput(parseInt(e.target.value, 10) || 1);
                setError(null);
              }}
              className="mt-1 block w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-mono text-slate-900 shadow-sm focus:border-indigo-500 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
            />
          </div>

          <div>
            <label
              htmlFor="max-ai-candidates"
              className="block text-xs font-semibold text-slate-700 dark:text-slate-300"
            >
              Max AI Evaluated
            </label>
            <input
              id="max-ai-candidates"
              data-testid="max-ai-candidates"
              type="number"
              min={0}
              max={100000}
              value={maxAi}
              onChange={e => {
                setMaxAi(parseInt(e.target.value, 10) || 0);
                setError(null);
              }}
              className="mt-1 block w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-mono text-slate-900 shadow-sm focus:border-indigo-500 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
            />
          </div>

          <div>
            <label
              htmlFor="max-doc-candidates"
              className="block text-xs font-semibold text-slate-700 dark:text-slate-300"
            >
              Max Document Candidates
            </label>
            <input
              id="max-doc-candidates"
              data-testid="max-doc-candidates"
              type="number"
              min={0}
              max={maxAi}
              value={maxDocs}
              onChange={e => {
                setMaxDocs(parseInt(e.target.value, 10) || 0);
                setError(null);
              }}
              className="mt-1 block w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-mono text-slate-900 shadow-sm focus:border-indigo-500 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
            />
          </div>
        </div>

        {error && (
          <p
            role="alert"
            data-testid="funnel-validation-error"
            className="mt-3 text-xs font-medium text-rose-600 dark:text-rose-400"
          >
            {error}
          </p>
        )}

        {isSaved && (
          <p
            role="status"
            data-testid="funnel-saved-status"
            className="mt-3 text-xs font-medium text-emerald-600 dark:text-emerald-400"
          >
            Funnel thresholds updated and applied successfully.
          </p>
        )}

        <div className="mt-4 flex items-center gap-3">
          <button
            type="button"
            onClick={handleSave}
            data-testid="save-funnel-policy-btn"
            className="rounded bg-indigo-600 px-3.5 py-1.5 text-xs font-semibold text-white shadow-sm hover:bg-indigo-500"
          >
            Save Thresholds
          </button>
          <button
            type="button"
            onClick={handleReset}
            data-testid="reset-funnel-policy-btn"
            className="rounded bg-slate-100 px-3.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
          >
            Reset to Recommended Defaults
          </button>
        </div>
      </div>
    </section>
  );
};
