import React, { useState } from 'react';

export interface MatchFactorItemView {
  points: number;
  description: string;
  factor_type?: string;
  dimension?: string;
  formatted_line: string;
}

export interface ExplainableMatchingBreakdownProps {
  positiveFactors: MatchFactorItemView[];
  negativeFactors: MatchFactorItemView[];
  totalScore: number;
  categoryLabel: string;
  formattedText: string;
  onCopyText?: () => void;
}

export const ExplainableMatchingBreakdown: React.FC<ExplainableMatchingBreakdownProps> = ({
  positiveFactors,
  negativeFactors,
  totalScore,
  categoryLabel,
  formattedText,
  onCopyText,
}) => {
  const [copied, setCopied] = useState(false);
  const [showRawText, setShowRawText] = useState(false);

  const handleCopy = () => {
    if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(formattedText);
    }
    setCopied(true);
    onCopyText?.();
    setTimeout(() => setCopied(false), 2000);
  };

  const scoreBadgeColor =
    totalScore >= 85
      ? 'border-emerald-500 bg-emerald-50 text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200'
      : totalScore >= 70
      ? 'border-blue-500 bg-blue-50 text-blue-800 dark:bg-blue-950/40 dark:text-blue-200'
      : totalScore >= 50
      ? 'border-amber-500 bg-amber-50 text-amber-800 dark:bg-amber-950/40 dark:text-amber-200'
      : 'border-rose-500 bg-rose-50 text-rose-800 dark:bg-rose-950/40 dark:text-rose-200';

  return (
    <article
      aria-label="Explainable Match Breakdown"
      data-testid="explainable-matching-breakdown"
      className="mjobs-explainable-breakdown rounded-xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900"
    >
      {/* Header with Score and Category */}
      <div className="mb-6 flex flex-col justify-between gap-3 border-b border-slate-100 pb-4 dark:border-slate-800 sm:flex-row sm:items-center">
        <div>
          <h3 className="text-lg font-bold tracking-tight text-slate-900 dark:text-slate-100">
            Explainable Match Breakdown
          </h3>
          <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
            Line-item factor attribution showing positive bonuses and missing preferred deductions.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div
            data-testid="score-badge"
            className={`inline-flex items-center gap-1.5 rounded-lg border px-3 py-1 text-sm font-bold ${scoreBadgeColor}`}
          >
            <span>{totalScore}/100</span>
            <span>—</span>
            <span>{categoryLabel}</span>
          </div>
        </div>
      </div>

      {/* Itemized Lists */}
      <div className="space-y-5">
        {/* Positive Factors */}
        <div>
          <div className="mb-2 flex items-center justify-between text-xs font-semibold text-emerald-700 dark:text-emerald-400">
            <span>POSITIVE MATCH FACTORS ({positiveFactors.length})</span>
            <span>+{positiveFactors.reduce((sum, f) => sum + f.points, 0)} pts</span>
          </div>
          <div className="space-y-1.5" data-testid="positive-factors-list">
            {positiveFactors.map((factor, idx) => (
              <div
                key={`pos-${idx}`}
                data-testid={`positive-factor-${idx}`}
                className="flex items-center justify-between rounded-md border border-emerald-100 bg-emerald-50/50 px-3 py-1.5 text-xs text-slate-800 transition dark:border-emerald-950/50 dark:bg-emerald-950/20 dark:text-slate-200"
              >
                <div className="flex items-center gap-2">
                  <span className="font-mono font-bold text-emerald-600 dark:text-emerald-400">
                    +{factor.points}
                  </span>
                  <span>{factor.description}</span>
                </div>
                {factor.dimension && (
                  <span className="font-mono text-[10px] text-slate-400 dark:text-slate-500">
                    {factor.dimension}
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* Negative / Missing Preferred Deductions */}
        {negativeFactors.length > 0 && (
          <div>
            <div className="mb-2 flex items-center justify-between text-xs font-semibold text-rose-700 dark:text-rose-400">
              <span>MISSING PREFERRED &amp; DEDUCTIONS ({negativeFactors.length})</span>
              <span>-{negativeFactors.reduce((sum, f) => sum + Math.abs(f.points), 0)} pts</span>
            </div>
            <div className="space-y-1.5" data-testid="negative-factors-list">
              {negativeFactors.map((factor, idx) => (
                <div
                  key={`neg-${idx}`}
                  data-testid={`negative-factor-${idx}`}
                  className="flex items-center justify-between rounded-md border border-rose-100 bg-rose-50/50 px-3 py-1.5 text-xs text-slate-800 transition dark:border-rose-950/50 dark:bg-rose-950/20 dark:text-slate-200"
                >
                  <div className="flex items-center gap-2">
                    <span className="font-mono font-bold text-rose-600 dark:text-rose-400">
                      -{Math.abs(factor.points)}
                    </span>
                    <span>{factor.description}</span>
                  </div>
                  {factor.dimension && (
                    <span className="font-mono text-[10px] text-slate-400 dark:text-slate-500">
                      {factor.dimension}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Raw Canonical Text View Toggle & Display */}
      <div className="mt-6 border-t border-slate-100 pt-4 dark:border-slate-800">
        <div className="flex items-center justify-between">
          <button
            type="button"
            onClick={() => setShowRawText(!showRawText)}
            data-testid="toggle-raw-text-btn"
            className="text-xs font-medium text-slate-500 hover:text-slate-700 dark:text-slate-400 dark:hover:text-slate-200"
          >
            {showRawText ? 'Hide Canonical Text' : 'View Canonical Text'}
          </button>
          <button
            type="button"
            onClick={handleCopy}
            data-testid="copy-breakdown-btn"
            className="rounded bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
          >
            {copied ? 'Copied!' : 'Copy Summary'}
          </button>
        </div>

        {showRawText && (
          <pre
            data-testid="canonical-text-block"
            className="mt-3 overflow-x-auto rounded-lg bg-slate-900 p-3 font-mono text-xs text-slate-200 dark:bg-black"
          >
            {formattedText}
          </pre>
        )}
      </div>
    </article>
  );
};
