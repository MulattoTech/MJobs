import React, { useState } from 'react';

export type AITaskId =
  | 'job_parsing'
  | 'job_scoring'
  | 'requirement_extraction'
  | 'resume_tailoring'
  | 'cover_letters'
  | 'free_form_answers'
  | 'company_summarization';

export interface TaskFallbackInfo {
  mode: 'DETERMINISTIC' | 'UNAVAILABLE';
  description: string;
}

export const AI_TASK_DEFINITIONS: Record<AITaskId, { title: string; description: string; fallback: TaskFallbackInfo }> = {
  job_parsing: {
    title: 'Job Parsing',
    description: 'Extract structured role, location, salary, and requirements from postings.',
    fallback: {
      mode: 'DETERMINISTIC',
      description: 'Use normalized fields supplied by job-source parsers; do not invent missing fields.',
    },
  },
  job_scoring: {
    title: 'Job Scoring',
    description: 'Evaluate candidate compatibility against profile attributes and preferences.',
    fallback: {
      mode: 'DETERMINISTIC',
      description: 'Rule-based, explainable match scores and filters remain available.',
    },
  },
  requirement_extraction: {
    title: 'Requirement Extraction',
    description: 'Distinguish required, preferred, and nice-to-have qualifications.',
    fallback: {
      mode: 'DETERMINISTIC',
      description: 'Use explicit posting requirements; keep unrecognized qualifications unknown.',
    },
  },
  resume_tailoring: {
    title: 'Resume Tailoring',
    description: 'Align approved career history facts to the specific opportunity.',
    fallback: {
      mode: 'DETERMINISTIC',
      description: 'Select and reorder approved career facts with safe templates; do not add or rephrase claims.',
    },
  },
  cover_letters: {
    title: 'Cover Letters',
    description: 'Draft personalized, factual introduction letters from profile credentials.',
    fallback: {
      mode: 'DETERMINISTIC',
      description: 'Build a factual cover letter from the approved profile and job fields.',
    },
  },
  free_form_answers: {
    title: 'Free-form Answers',
    description: 'Synthesize custom screening question answers using verified experience.',
    fallback: {
      mode: 'UNAVAILABLE',
      description: 'No new answer is generated; review and reuse only explicitly approved saved answers.',
    },
  },
  company_summarization: {
    title: 'Company Summarization',
    description: 'Synthesize mission, culture, and business intelligence summaries.',
    fallback: {
      mode: 'UNAVAILABLE',
      description: 'No company summary is generated; source-grounded company facts remain available.',
    },
  },
};

export const ORDERED_AI_TASKS: AITaskId[] = [
  'job_parsing',
  'job_scoring',
  'requirement_extraction',
  'resume_tailoring',
  'cover_letters',
  'free_form_answers',
  'company_summarization',
];

export interface TaskModelAssignment {
  provider: string;
  model: string | null;
}

export interface AITaskConfigurationProps {
  tasks: Record<AITaskId, TaskModelAssignment>;
  availableProviders?: Array<{ id: string; name: string }>;
  onSaveTask?: (task: AITaskId, assignment: TaskModelAssignment) => void;
  onResetAllToDisabled?: () => void;
}

const DEFAULT_PROVIDERS = [
  { id: 'disabled', name: 'Disabled (Deterministic Fallback)' },
  { id: 'ollama', name: 'Ollama (Local Endpoint)' },
  { id: 'openai-compatible', name: 'OpenAI-Compatible (Self-hosted / vLLM)' },
  { id: 'openai', name: 'OpenAI Official API' },
  { id: 'azure-openai', name: 'Azure OpenAI Enterprise' },
  { id: 'local', name: 'Local Loopback Endpoint' },
];

const SECRET_PREFIXES = ['sk-', 'sk_', 'ghp_', 'gho_', 'hf_'];

export function validateModelId(provider: string, model: string | null): string | null {
  if (provider === 'disabled') return null;
  if (!model || !model.trim()) return 'Model identifier is required when provider is enabled.';
  const trimmed = model.trim();
  const lower = trimmed.toLowerCase();
  for (const prefix of SECRET_PREFIXES) {
    if (lower.startsWith(prefix)) {
      return 'Model identifier cannot begin with a credential or secret token prefix.';
    }
  }
  const safeRegex = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/;
  if (!safeRegex.test(trimmed)) {
    return 'Model identifier contains invalid characters or exceeds 128 characters.';
  }
  return null;
}

export const AITaskConfigurationView: React.FC<AITaskConfigurationProps> = ({
  tasks,
  availableProviders = DEFAULT_PROVIDERS,
  onSaveTask,
  onResetAllToDisabled,
}) => {
  const [editingTask, setEditingTask] = useState<AITaskId | null>(null);
  const [editProvider, setEditProvider] = useState<string>('disabled');
  const [editModel, setEditModel] = useState<string>('');
  const [validationError, setValidationError] = useState<string | null>(null);

  const startEdit = (taskId: AITaskId) => {
    const current = tasks[taskId] || { provider: 'disabled', model: null };
    setEditingTask(taskId);
    setEditProvider(current.provider);
    setEditModel(current.model || '');
    setValidationError(null);
  };

  const cancelEdit = () => {
    setEditingTask(null);
    setValidationError(null);
  };

  const handleSave = (taskId: AITaskId) => {
    const error = validateModelId(editProvider, editProvider === 'disabled' ? null : editModel);
    if (error) {
      setValidationError(error);
      return;
    }
    if (onSaveTask) {
      onSaveTask(taskId, {
        provider: editProvider,
        model: editProvider === 'disabled' ? null : editModel.trim(),
      });
    }
    setEditingTask(null);
    setValidationError(null);
  };

  return (
    <section
      aria-label="AI Task Configuration"
      className="mjobs-ai-task-configuration rounded-xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900"
    >
      <div className="mb-6 flex flex-col justify-between gap-3 border-b border-slate-100 pb-4 dark:border-slate-800 sm:flex-row sm:items-center">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-slate-900 dark:text-slate-100">
            AI Task Model Routing
          </h2>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Configure independent models and inference providers for each task with explicit deterministic fallback behavior.
          </p>
        </div>
        {onResetAllToDisabled && (
          <button
            type="button"
            onClick={onResetAllToDisabled}
            data-testid="reset-all-disabled-btn"
            className="rounded bg-slate-100 px-3 py-1.5 text-xs font-semibold text-slate-700 transition hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
          >
            Disable All (Safe Fallback)
          </button>
        )}
      </div>

      <div className="space-y-4" data-testid="task-routing-list">
        {ORDERED_AI_TASKS.map(taskId => {
          const def = AI_TASK_DEFINITIONS[taskId];
          const assignment = tasks[taskId] || { provider: 'disabled', model: null };
          const isEditing = editingTask === taskId;
          const isEnabled = assignment.provider !== 'disabled';

          return (
            <article
              key={taskId}
              data-testid={`task-card-${taskId}`}
              className={`rounded-lg border p-4 transition-colors ${
                isEnabled
                  ? 'border-indigo-200 bg-indigo-50/20 dark:border-indigo-900/50 dark:bg-indigo-950/10'
                  : 'border-slate-200 bg-slate-50/30 dark:border-slate-800 dark:bg-slate-900/40'
              }`}
            >
              <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-start">
                <div className="flex-1">
                  <div className="flex items-center gap-2">
                    <h3 className="text-base font-semibold text-slate-900 dark:text-slate-100">
                      {def.title}
                    </h3>
                    <span
                      data-testid={`status-badge-${taskId}`}
                      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
                        isEnabled
                          ? 'bg-indigo-100 text-indigo-800 dark:bg-indigo-900/60 dark:text-indigo-200'
                          : 'bg-slate-200 text-slate-700 dark:bg-slate-800 dark:text-slate-300'
                      }`}
                    >
                      {isEnabled ? 'Active AI Model' : 'Fallback Active'}
                    </span>
                  </div>
                  <p className="mt-1 text-xs text-slate-600 dark:text-slate-400">
                    {def.description}
                  </p>

                  {!isEditing ? (
                    <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-600 dark:text-slate-300">
                      <div>
                        <span className="text-slate-400 dark:text-slate-500">Provider:</span>{' '}
                        <span className="font-semibold" data-testid={`assigned-provider-${taskId}`}>
                          {availableProviders.find(p => p.id === assignment.provider)?.name || assignment.provider}
                        </span>
                      </div>
                      {isEnabled && (
                        <div>
                          <span className="text-slate-400 dark:text-slate-500">Model:</span>{' '}
                          <span className="font-mono font-medium" data-testid={`assigned-model-${taskId}`}>
                            {assignment.model || 'None'}
                          </span>
                        </div>
                      )}
                      <div>
                        <span className="text-slate-400 dark:text-slate-500">Fallback Strategy:</span>{' '}
                        <span
                          className={`font-semibold ${
                            def.fallback.mode === 'DETERMINISTIC'
                              ? 'text-emerald-700 dark:text-emerald-400'
                              : 'text-amber-700 dark:text-amber-400'
                          }`}
                        >
                          {def.fallback.mode}
                        </span>
                      </div>
                    </div>
                  ) : (
                    <div className="mt-4 space-y-3 rounded-lg border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-850">
                      <div className="grid gap-3 sm:grid-cols-2">
                        <div>
                          <label
                            htmlFor={`provider-select-${taskId}`}
                            className="block text-xs font-semibold text-slate-700 dark:text-slate-300"
                          >
                            AI Provider
                          </label>
                          <select
                            id={`provider-select-${taskId}`}
                            data-testid={`provider-select-${taskId}`}
                            value={editProvider}
                            onChange={e => {
                              setEditProvider(e.target.value);
                              setValidationError(null);
                            }}
                            className="mt-1 block w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 text-xs text-slate-900 shadow-sm focus:border-indigo-500 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
                          >
                            {availableProviders.map(p => (
                              <option key={p.id} value={p.id}>
                                {p.name}
                              </option>
                            ))}
                          </select>
                        </div>

                        {editProvider !== 'disabled' && (
                          <div>
                            <label
                              htmlFor={`model-input-${taskId}`}
                              className="block text-xs font-semibold text-slate-700 dark:text-slate-300"
                            >
                              Model Identifier
                            </label>
                            <input
                              type="text"
                              id={`model-input-${taskId}`}
                              data-testid={`model-input-${taskId}`}
                              value={editModel}
                              placeholder="e.g. gpt-4o-mini or llama3.2"
                              onChange={e => {
                                setEditModel(e.target.value);
                                setValidationError(null);
                              }}
                              className="mt-1 block w-full rounded border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-mono text-slate-900 shadow-sm focus:border-indigo-500 focus:outline-none dark:border-slate-700 dark:bg-slate-800 dark:text-slate-100"
                            />
                          </div>
                        )}
                      </div>

                      {validationError && (
                        <p
                          role="alert"
                          data-testid={`validation-error-${taskId}`}
                          className="text-xs font-medium text-rose-600 dark:text-rose-400"
                        >
                          {validationError}
                        </p>
                      )}

                      <div className="flex items-center gap-2 pt-1">
                        <button
                          type="button"
                          onClick={() => handleSave(taskId)}
                          data-testid={`save-task-btn-${taskId}`}
                          className="rounded bg-indigo-600 px-3 py-1 text-xs font-semibold text-white shadow-sm hover:bg-indigo-500"
                        >
                          Save Assignment
                        </button>
                        <button
                          type="button"
                          onClick={cancelEdit}
                          data-testid={`cancel-task-btn-${taskId}`}
                          className="rounded bg-slate-100 px-3 py-1 text-xs font-medium text-slate-700 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  )}

                  <div className="mt-2 text-xs text-slate-500 dark:text-slate-400">
                    <span className="italic">Fallback policy:</span> {def.fallback.description}
                  </div>
                </div>

                {!isEditing && onSaveTask && (
                  <button
                    type="button"
                    onClick={() => startEdit(taskId)}
                    data-testid={`edit-task-btn-${taskId}`}
                    className="self-start rounded border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 transition hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
                  >
                    Configure
                  </button>
                )}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
};
