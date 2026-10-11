import React, { useState } from 'react';

export interface WeightDimensions {
  title: number;
  skills: number;
  experience: number;
  location: number;
  salary: number;
  education: number;
  company: number;
  industry: number;
}

export const CANONICAL_DEFAULT_WEIGHTS: WeightDimensions = {
  title: 20,
  skills: 25,
  experience: 15,
  location: 15,
  salary: 10,
  education: 5,
  company: 5,
  industry: 5,
};

export const WEIGHT_PRESETS: Record<string, { label: string; weights: WeightDimensions }> = {
  balanced_default: {
    label: 'Balanced Default (Canonical Spec)',
    weights: CANONICAL_DEFAULT_WEIGHTS,
  },
  technical_skills: {
    label: 'Technical Skills Focus',
    weights: {
      title: 15,
      skills: 40,
      experience: 15,
      location: 10,
      salary: 10,
      education: 5,
      company: 2.5,
      industry: 2.5,
    },
  },
  seniority_experience: {
    label: 'Seniority & Experience Focus',
    weights: {
      title: 20,
      skills: 20,
      experience: 30,
      location: 10,
      salary: 10,
      education: 5,
      company: 2.5,
      industry: 2.5,
    },
  },
  remote_compensation: {
    label: 'Remote & Compensation Focus',
    weights: {
      title: 15,
      skills: 20,
      experience: 10,
      location: 25,
      salary: 20,
      education: 5,
      company: 2.5,
      industry: 2.5,
    },
  },
};

export interface MatchWeightConfigurationProps {
  initialWeights?: WeightDimensions;
  onSaveWeights?: (weights: WeightDimensions) => void;
  onResetWeights?: () => void;
}

export const MatchWeightConfiguration: React.FC<MatchWeightConfigurationProps> = ({
  initialWeights = CANONICAL_DEFAULT_WEIGHTS,
  onSaveWeights,
  onResetWeights,
}) => {
  const [weights, setWeights] = useState<WeightDimensions>(initialWeights);
  const [selectedPreset, setSelectedPreset] = useState<string>('balanced_default');

  const totalSum = Object.values(weights).reduce((a, b) => a + b, 0);
  const isNormalized = Math.abs(totalSum - 100) < 0.1;

  const handleDimensionChange = (dim: keyof WeightDimensions, value: number) => {
    setSelectedPreset('custom');
    setWeights(prev => ({
      ...prev,
      [dim]: Math.max(0, Math.min(100, value)),
    }));
  };

  const handleSelectPreset = (presetKey: string) => {
    setSelectedPreset(presetKey);
    if (presetKey in WEIGHT_PRESETS) {
      setWeights(WEIGHT_PRESETS[presetKey].weights);
    }
  };

  const handleNormalize = () => {
    if (totalSum <= 0) return;
    const factor = 100 / totalSum;
    const normalized: Partial<WeightDimensions> = {};
    for (const key of Object.keys(weights) as (keyof WeightDimensions)[]) {
      normalized[key] = Math.round(weights[key] * factor * 10) / 10;
    }
    setWeights(normalized as WeightDimensions);
  };

  const handleReset = () => {
    setWeights(CANONICAL_DEFAULT_WEIGHTS);
    setSelectedPreset('balanced_default');
    onResetWeights?.();
  };

  const handleSave = () => {
    onSaveWeights?.(weights);
  };

  return (
    <section
      aria-label="Match Weight Configuration"
      data-testid="match-weight-configuration"
      className="mjobs-match-weight-config rounded-xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900"
    >
      <div className="mb-6 flex flex-col justify-between gap-3 border-b border-slate-100 pb-4 dark:border-slate-800 sm:flex-row sm:items-center">
        <div>
          <h3 className="text-lg font-bold tracking-tight text-slate-900 dark:text-slate-100">
            Match Weight Configuration
          </h3>
          <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
            Tune scoring dimension weights and select role-oriented weight distribution presets.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span
            data-testid="total-weight-gauge"
            className={`rounded-full px-3 py-1 text-xs font-semibold ${
              isNormalized
                ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300'
                : 'bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300'
            }`}
          >
            Total: {Math.round(totalSum * 10) / 10}% {isNormalized ? '(Balanced)' : '(Unbalanced)'}
          </span>
        </div>
      </div>

      {/* Preset Selector */}
      <div className="mb-6">
        <label htmlFor="preset-select" className="mb-1 block text-xs font-semibold text-slate-700 dark:text-slate-300">
          Weight Distribution Preset
        </label>
        <select
          id="preset-select"
          data-testid="preset-select"
          value={selectedPreset}
          onChange={e => handleSelectPreset(e.target.value)}
          className="w-full rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-800 shadow-sm focus:border-blue-500 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
        >
          {Object.entries(WEIGHT_PRESETS).map(([key, preset]) => (
            <option key={key} value={key}>
              {preset.label}
            </option>
          ))}
          <option value="custom">Custom Weights</option>
        </select>
      </div>

      {/* Dimensions Grid */}
      <div className="grid gap-4 sm:grid-cols-2" data-testid="dimensions-grid">
        {(Object.keys(weights) as (keyof WeightDimensions)[]).map(dim => (
          <div
            key={dim}
            data-testid={`dimension-row-${dim}`}
            className="rounded-lg border border-slate-100 bg-slate-50/50 p-3 dark:border-slate-800/60 dark:bg-slate-850"
          >
            <div className="mb-1 flex items-center justify-between text-xs">
              <span className="font-semibold capitalize text-slate-800 dark:text-slate-200">
                {dim}
              </span>
              <span className="font-mono font-bold text-blue-600 dark:text-blue-400">
                {weights[dim]}%
              </span>
            </div>
            <input
              type="range"
              min="0"
              max="50"
              step="0.5"
              value={weights[dim]}
              onChange={e => handleDimensionChange(dim, parseFloat(e.target.value))}
              data-testid={`slider-${dim}`}
              className="w-full accent-blue-600"
              aria-label={`${dim} weight slider`}
            />
          </div>
        ))}
      </div>

      {/* Action Footer */}
      <div className="mt-6 flex flex-wrap items-center justify-between gap-3 border-t border-slate-100 pt-4 dark:border-slate-800">
        <div className="flex gap-2">
          {!isNormalized && (
            <button
              type="button"
              onClick={handleNormalize}
              data-testid="normalize-btn"
              className="rounded bg-amber-50 px-3 py-1.5 text-xs font-semibold text-amber-800 transition hover:bg-amber-100 dark:bg-amber-950/40 dark:text-amber-200"
            >
              Auto-Normalize to 100%
            </button>
          )}
          <button
            type="button"
            onClick={handleReset}
            data-testid="reset-btn"
            className="rounded bg-slate-100 px-3 py-1.5 text-xs font-medium text-slate-600 transition hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300"
          >
            Reset to Canonical Default
          </button>
        </div>
        <button
          type="button"
          onClick={handleSave}
          data-testid="save-btn"
          className="rounded bg-blue-600 px-4 py-1.5 text-xs font-semibold text-white shadow-sm transition hover:bg-blue-700"
        >
          Save Weights
        </button>
      </div>
    </section>
  );
};
