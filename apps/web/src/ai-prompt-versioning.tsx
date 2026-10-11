import React, { useState } from 'react';

export type PromptTargetId = 'resume' | 'cover_letter' | 'screening_answers' | 'job_parsing';

export interface PromptVersionItem {
  version: string;
  template: string;
  description: string;
  variables: string[];
  sha256: string;
  createdAtUtc?: string;
  isActive: boolean;
}

export interface AIPromptVersioningProps {
  targets?: Record<PromptTargetId, PromptVersionItem[]>;
  onSelectActiveVersion?: (target: PromptTargetId, version: string) => void;
  onRegisterVersion?: (target: PromptTargetId, version: string, template: string, description: string) => void;
}

export const CANONICAL_TARGET_LABELS: Record<PromptTargetId, { title: string; description: string }> = {
  resume: {
    title: 'Resume Tailoring',
    description: 'Internal prompt for tailoring approved profile facts to matching job requirements.',
  },
  cover_letter: {
    title: 'Cover Letter Drafting',
    description: 'Internal prompt for synthesizing tailored, factual candidate introduction letters.',
  },
  screening_answers: {
    title: 'Screening Answers',
    description: 'Internal prompt for answering company application questions from verified experience.',
  },
  job_parsing: {
    title: 'Job Parsing',
    description: 'Internal prompt for extracting structured role fields from raw untrusted postings.',
  },
};

export const DEFAULT_PROMPT_VERSIONS: Record<PromptTargetId, PromptVersionItem[]> = {
  resume: [
    {
      version: '1.0.0',
      template: 'Tailor resume facts for opportunity:\nRole: {job_title}\nCompany: {company_name}\nFacts: {career_facts}',
      description: 'Initial canonical resume tailoring prompt template v1.0.0',
      variables: ['job_title', 'company_name', 'career_facts'],
      sha256: 'a1b2c3d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef',
      isActive: true,
    },
  ],
  cover_letter: [
    {
      version: '1.0.0',
      template: 'Draft cover letter for {job_title} at {company_name}:\n{candidate_profile}\n{qualifications}',
      description: 'Initial canonical cover letter prompt template v1.0.0',
      variables: ['job_title', 'company_name', 'candidate_profile', 'qualifications'],
      sha256: 'b2c3d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef01',
      isActive: true,
    },
  ],
  screening_answers: [
    {
      version: '1.0.0',
      template: 'Answer question {question_text} for {job_title} at {company_name} using: {approved_answers}',
      description: 'Initial canonical screening answer prompt template v1.0.0',
      variables: ['question_text', 'job_title', 'company_name', 'approved_answers'],
      sha256: 'c3d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef0102',
      isActive: true,
    },
  ],
  job_parsing: [
    {
      version: '1.0.0',
      template: 'Extract structured job details from:\n{raw_content}',
      description: 'Initial canonical job parsing prompt template v1.0.0',
      variables: ['raw_content'],
      sha256: 'd4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef010203',
      isActive: true,
    },
  ],
};

export const AIPromptVersioningView: React.FC<AIPromptVersioningProps> = ({
  targets = DEFAULT_PROMPT_VERSIONS,
  onSelectActiveVersion,
}) => {
  const targetKeys = Object.keys(CANONICAL_TARGET_LABELS) as PromptTargetId[];
  const [selectedTarget, setSelectedTarget] = useState<PromptTargetId>('resume');

  const versions = targets[selectedTarget] || [];
  const activeVersion = versions.find(v => v.isActive) || versions[0];

  return (
    <section
      aria-label="AI Prompt Versioning"
      className="mjobs-ai-prompt-versioning rounded-xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900"
    >
      <div className="mb-6 flex flex-col justify-between gap-3 border-b border-slate-100 pb-4 dark:border-slate-800 sm:flex-row sm:items-center">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-slate-900 dark:text-slate-100">
            AI Prompt &amp; Template Versioning
          </h2>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Cryptographically versioned templates for reproducible document generation and untrusted parsing.
          </p>
        </div>
        <div>
          <span className="inline-flex items-center rounded-full bg-indigo-50 px-2.5 py-1 text-xs font-semibold text-indigo-700 dark:bg-indigo-950/50 dark:text-indigo-300">
            Reproducibility Gate Active
          </span>
        </div>
      </div>

      {/* Target Selector Tabs */}
      <div className="mb-6 flex flex-wrap gap-2 border-b border-slate-100 pb-3 dark:border-slate-800" data-testid="target-tabs">
        {targetKeys.map(tKey => (
          <button
            key={tKey}
            type="button"
            data-testid={`target-tab-${tKey}`}
            onClick={() => setSelectedTarget(tKey)}
            className={`rounded-lg px-3.5 py-1.5 text-xs font-semibold transition ${
              selectedTarget === tKey
                ? 'bg-indigo-600 text-white shadow-sm'
                : 'bg-slate-100 text-slate-700 hover:bg-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700'
            }`}
          >
            {CANONICAL_TARGET_LABELS[tKey].title}
          </button>
        ))}
      </div>

      {/* Active Version Overview Card */}
      {activeVersion ? (
        <div className="rounded-lg border border-slate-200 bg-slate-50/50 p-5 dark:border-slate-800 dark:bg-slate-850/50">
          <div className="flex flex-col justify-between gap-2 sm:flex-row sm:items-center">
            <div>
              <div className="flex items-center gap-2">
                <h3 className="text-base font-bold text-slate-900 dark:text-slate-100">
                  Version {activeVersion.version}
                </h3>
                <span className="rounded bg-emerald-100 px-2 py-0.5 text-xs font-semibold text-emerald-800 dark:bg-emerald-900/60 dark:text-emerald-200">
                  Active in Production
                </span>
              </div>
              <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
                {activeVersion.description}
              </p>
            </div>
            <div className="text-right">
              <span className="text-xs text-slate-400">SHA-256 Fingerprint:</span>
              <p className="font-mono text-xs font-semibold text-slate-600 dark:text-slate-300">
                {activeVersion.sha256.substring(0, 16)}…
              </p>
            </div>
          </div>

          <div className="mt-4">
            <span className="text-xs font-semibold text-slate-700 dark:text-slate-300">
              Template Variables:
            </span>
            <div className="mt-1 flex flex-wrap gap-1.5">
              {activeVersion.variables.map(v => (
                <span
                  key={v}
                  className="rounded bg-indigo-50 px-2 py-0.5 font-mono text-xs font-medium text-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-300"
                >
                  &#123;{v}&#125;
                </span>
              ))}
            </div>
          </div>

          <div className="mt-4">
            <span className="text-xs font-semibold text-slate-700 dark:text-slate-300">
              Template Body:
            </span>
            <pre className="mt-1 overflow-x-auto rounded border border-slate-200 bg-white p-3 font-mono text-xs text-slate-800 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200">
              {activeVersion.template}
            </pre>
          </div>
        </div>
      ) : (
        <p className="text-xs text-slate-500">No prompt versions registered for this target.</p>
      )}

      {/* Version History Table */}
      <div className="mt-6">
        <h3 className="text-sm font-bold text-slate-900 dark:text-slate-100">
          Template Version History ({versions.length})
        </h3>
        <div className="mt-2 overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-800">
          <table className="min-w-full divide-y divide-slate-200 text-left text-xs dark:divide-slate-800">
            <thead className="bg-slate-50 font-semibold text-slate-700 dark:bg-slate-850 dark:text-slate-300">
              <tr>
                <th className="px-3 py-2">Version</th>
                <th className="px-3 py-2">Description</th>
                <th className="px-3 py-2">SHA-256 Fingerprint</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
              {versions.map(v => (
                <tr key={v.version} className="hover:bg-slate-50/50 dark:hover:bg-slate-850/30">
                  <td className="px-3 py-2 font-mono font-semibold text-slate-900 dark:text-slate-100">
                    {v.version}
                  </td>
                  <td className="px-3 py-2 text-slate-600 dark:text-slate-400">
                    {v.description}
                  </td>
                  <td className="px-3 py-2 font-mono text-slate-500">
                    {v.sha256.substring(0, 12)}…
                  </td>
                  <td className="px-3 py-2">
                    {v.isActive ? (
                      <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-2xs font-semibold text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300">
                        ACTIVE
                      </span>
                    ) : (
                      <span className="text-slate-400">Archived</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right">
                    {!v.isActive && onSelectActiveVersion && (
                      <button
                        type="button"
                        onClick={() => onSelectActiveVersion(selectedTarget, v.version)}
                        className="font-semibold text-indigo-600 hover:text-indigo-700 dark:text-indigo-400"
                      >
                        Activate
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
};
