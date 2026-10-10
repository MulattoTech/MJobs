import React, { useState } from 'react';

export interface AIProviderConfig {
  id: string;
  name: string;
  providerType: 'openai' | 'openai-compatible' | 'local' | 'ollama' | 'disabled';
  baseUrl: string;
  hasApiKey: boolean;
  maskedApiKey?: string;
  status: 'connected' | 'disconnected' | 'testing' | 'error';
  isDefault: boolean;
  capabilities: {
    textGeneration: boolean;
    structuredOutput: boolean;
    streaming: boolean;
  };
  activeModel: string;
  concurrencyLimit: number;
  lastTestedUtc?: string;
  errorMessage?: string;
}

export interface AIProviderSettingsProps {
  providers: AIProviderConfig[];
  onSelectDefault?: (providerId: string) => void;
  onTestConnection?: (providerId: string) => Promise<boolean>;
  onUpdateProvider?: (provider: AIProviderConfig) => void;
}

export const AIProviderSettings: React.FC<AIProviderSettingsProps> = ({
  providers,
  onSelectDefault,
  onTestConnection,
}) => {
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, { success: boolean; message: string }>>({});

  const handleTest = async (provider: AIProviderConfig) => {
    if (!onTestConnection) return;
    setTestingId(provider.id);
    try {
      const ok = await onTestConnection(provider.id);
      setTestResults(prev => ({
        ...prev,
        [provider.id]: {
          success: ok,
          message: ok ? 'Connection verified successfully.' : 'Connection check failed. Verify endpoint URL.',
        },
      }));
    } catch {
      setTestResults(prev => ({
        ...prev,
        [provider.id]: {
          success: false,
          message: 'Network error during connection check.',
        },
      }));
    } finally {
      setTestingId(null);
    }
  };

  return (
    <section
      aria-label="AI Provider Settings"
      className="mjobs-ai-provider-settings rounded-xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900"
    >
      <div className="mb-6 flex flex-col justify-between gap-2 border-b border-slate-100 pb-4 dark:border-slate-800 sm:flex-row sm:items-center">
        <div>
          <h2 className="text-xl font-bold tracking-tight text-slate-900 dark:text-slate-100">
            AI Provider Infrastructure
          </h2>
          <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
            Configure local, OpenAI-compatible, and cloud AI inference providers with strict loopback and budget enforcement.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="inline-flex items-center rounded-full bg-blue-50 px-2.5 py-1 text-xs font-semibold text-blue-700 dark:bg-blue-950/50 dark:text-blue-300">
            {providers.length} Configured
          </span>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2" data-testid="ai-provider-list">
        {providers.map(provider => {
          const isTesting = testingId === provider.id;
          const result = testResults[provider.id];

          return (
            <article
              key={provider.id}
              data-testid={`provider-card-${provider.id}`}
              className={`relative flex flex-col justify-between rounded-lg border p-5 transition-shadow ${
                provider.isDefault
                  ? 'border-blue-500 ring-2 ring-blue-500/20 dark:border-blue-600'
                  : 'border-slate-200 hover:border-slate-300 dark:border-slate-800 dark:hover:border-slate-700'
              }`}
            >
              <div>
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <div className="flex items-center gap-2">
                      <h3 className="text-base font-semibold text-slate-900 dark:text-slate-100">
                        {provider.name}
                      </h3>
                      {provider.isDefault && (
                        <span
                          data-testid="default-badge"
                          className="rounded bg-blue-100 px-2 py-0.5 text-xs font-semibold text-blue-800 dark:bg-blue-900/60 dark:text-blue-200"
                        >
                          Default Active
                        </span>
                      )}
                    </div>
                    <p className="mt-1 font-mono text-xs text-slate-500 dark:text-slate-400">
                      {provider.baseUrl}
                    </p>
                  </div>
                  <span
                    data-testid={`status-badge-${provider.id}`}
                    className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
                      provider.status === 'connected'
                        ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300'
                        : provider.status === 'error'
                        ? 'bg-rose-50 text-rose-700 dark:bg-rose-950/40 dark:text-rose-300'
                        : 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300'
                    }`}
                  >
                    ● {provider.status.toUpperCase()}
                  </span>
                </div>

                {/* Capabilities Badges */}
                <div className="mt-3 flex flex-wrap gap-1.5" data-testid="capabilities-container">
                  {provider.capabilities.textGeneration && (
                    <span className="rounded bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700 dark:bg-slate-800 dark:text-slate-300">
                      Text Gen
                    </span>
                  )}
                  {provider.capabilities.structuredOutput && (
                    <span className="rounded bg-indigo-50 px-2 py-0.5 text-xs font-medium text-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-300">
                      Structured JSON
                    </span>
                  )}
                  {provider.capabilities.streaming && (
                    <span className="rounded bg-purple-50 px-2 py-0.5 text-xs font-medium text-purple-700 dark:bg-purple-950/40 dark:text-purple-300">
                      Streaming
                    </span>
                  )}
                </div>

                {/* Model & Key Details */}
                <div className="mt-4 grid grid-cols-2 gap-2 text-xs text-slate-600 dark:text-slate-300">
                  <div>
                    <span className="text-slate-400 dark:text-slate-500">Model:</span>{' '}
                    <span className="font-semibold">{provider.activeModel}</span>
                  </div>
                  <div>
                    <span className="text-slate-400 dark:text-slate-500">Concurrency:</span>{' '}
                    <span className="font-semibold">{provider.concurrencyLimit} slots</span>
                  </div>
                  <div className="col-span-2">
                    <span className="text-slate-400 dark:text-slate-500">API Key:</span>{' '}
                    <span className="font-mono">
                      {provider.hasApiKey
                        ? provider.maskedApiKey || 'Configured (masked)'
                        : 'No Key Required (Local/Self-hosted)'}
                    </span>
                  </div>
                </div>

                {result && (
                  <div
                    role="alert"
                    data-testid={`test-result-${provider.id}`}
                    className={`mt-3 rounded p-2 text-xs ${
                      result.success
                        ? 'bg-emerald-50 text-emerald-800 dark:bg-emerald-950/30 dark:text-emerald-200'
                        : 'bg-rose-50 text-rose-800 dark:bg-rose-950/30 dark:text-rose-200'
                    }`}
                  >
                    {result.message}
                  </div>
                )}
              </div>

              {/* Actions */}
              <div className="mt-5 flex items-center justify-between border-t border-slate-100 pt-3 dark:border-slate-800">
                {!provider.isDefault && onSelectDefault ? (
                  <button
                    type="button"
                    onClick={() => onSelectDefault(provider.id)}
                    className="text-xs font-semibold text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
                    data-testid={`set-default-btn-${provider.id}`}
                  >
                    Set as Default
                  </button>
                ) : (
                  <span className="text-xs text-slate-400">Primary Provider</span>
                )}

                {onTestConnection && provider.providerType !== 'disabled' && (
                  <button
                    type="button"
                    onClick={() => handleTest(provider)}
                    disabled={isTesting}
                    data-testid={`test-btn-${provider.id}`}
                    className="rounded bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-700 transition hover:bg-slate-200 disabled:opacity-50 dark:bg-slate-800 dark:text-slate-200 dark:hover:bg-slate-700"
                  >
                    {isTesting ? 'Testing...' : 'Test Connection'}
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
