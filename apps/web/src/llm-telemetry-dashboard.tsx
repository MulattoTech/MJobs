import React, { useState } from 'react';

export type ProviderTelemetry = {
  provider: string;
  active_concurrency: number;
  concurrency_limit: number;
  total_calls: number;
  total_tokens: number;
  total_cost_usd: number;
  error_rate: number;
};

export type TaskUsage = {
  total_calls: number;
  successful_calls: number;
  failed_calls: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_tokens: number;
  total_cost_usd: number;
  avg_latency_ms: number;
};

export type AttemptRecord = {
  attempt_id: string;
  task: string;
  provider: string;
  model: string;
  latency_ms: number;
  status: 'SUCCESS' | 'FAILED' | 'RATE_LIMITED' | 'FALLBACK' | 'BUDGET_STOP';
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  estimated_cost: number;
  fallback_reason?: string | null;
  error_code?: string | null;
  timestamp_utc: string;
};

export type LLMTelemetryDashboardProps = {
  totalCalls?: number;
  successfulCalls?: number;
  failedCalls?: number;
  activeConcurrency?: number;
  globalConcurrencyLimit?: number;
  totalPromptTokens?: number;
  totalCompletionTokens?: number;
  totalTokens?: number;
  totalCostUsd?: number;
  budgetCeilingUsd?: number | null;
  budgetRemainingUsd?: number | null;
  budgetWarningTriggered?: boolean;
  byProvider?: Record<string, ProviderTelemetry>;
  byTask?: Record<string, TaskUsage>;
  recentAttempts?: AttemptRecord[];
};

export function LLMTelemetryDashboard({
  totalCalls = 0,
  successfulCalls = 0,
  failedCalls = 0,
  activeConcurrency = 0,
  globalConcurrencyLimit = 8,
  totalPromptTokens = 0,
  totalCompletionTokens = 0,
  totalTokens = 0,
  totalCostUsd = 0.0,
  budgetCeilingUsd = 10.0,
  budgetRemainingUsd = 10.0,
  budgetWarningTriggered = false,
  byProvider = {},
  byTask = {},
  recentAttempts = [],
}: LLMTelemetryDashboardProps) {
  const [selectedTab, setSelectedTab] = useState<'OVERVIEW' | 'PROVIDERS' | 'TASKS' | 'ATTEMPTS'>('OVERVIEW');

  const successRate = totalCalls > 0 ? (successfulCalls / totalCalls) * 100 : 100;
  const budgetUsagePercent =
    budgetCeilingUsd != null && budgetCeilingUsd > 0
      ? Math.min(100, (totalCostUsd / budgetCeilingUsd) * 100)
      : 0;

  return (
    <section
      className="panel llm-telemetry-dashboard-panel"
      role="region"
      aria-label="LLM Telemetry and Cost Dashboard"
      data-testid="llm-telemetry-panel"
    >
      <div className="toolbar">
        <div>
          <span className="eyebrow">AI TELEMETRY & COST MONITOR (MJ-REF-015)</span>
          <h3>LLM Token, Cost & Concurrency Telemetry</h3>
        </div>
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
          {budgetWarningTriggered && (
            <span
              className="tag warn"
              data-testid="budget-warning-tag"
              title="Budget warning threshold reached (>80%)"
            >
              ⚠️ Budget Warning
            </span>
          )}
          <span
            className={`status ${budgetWarningTriggered ? 'warn' : 'good'}`}
            data-testid="total-cost-status"
          >
            Cost: ${totalCostUsd.toFixed(4)}
          </span>
        </div>
      </div>

      <p className="description">
        MJobs monitors all AI operations in real time: tracking provider attempt chains, fallbacks,
        concurrency limits, and token cost provenance. Zero prompt text or candidate PII is stored.
      </p>

      {/* KPI Cards */}
      <div
        className="kpi-grid"
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
          gap: '0.75rem',
          marginBottom: '1rem',
        }}
      >
        {/* Cost & Budget */}
        <div
          className="kpi-card"
          style={{
            padding: '0.75rem',
            backgroundColor: budgetWarningTriggered ? '#fffaf0' : '#f7fafc',
            border: budgetWarningTriggered ? '1px solid #feebc8' : '1px solid #e2e8f0',
            borderRadius: '6px',
          }}
        >
          <span style={{ fontSize: '0.75rem', fontWeight: 600, color: '#718096' }}>
            ESTIMATED SPEND
          </span>
          <div style={{ fontSize: '1.25rem', fontWeight: 700, margin: '0.25rem 0' }} data-testid="kpi-total-cost">
            ${totalCostUsd.toFixed(4)}
          </div>
          <div style={{ fontSize: '0.75rem', color: '#718096' }}>
            {budgetCeilingUsd != null
              ? `Budget: $${budgetCeilingUsd.toFixed(2)} ($${(budgetRemainingUsd ?? 0).toFixed(2)} left)`
              : 'Unlimited'}
          </div>
          {budgetCeilingUsd != null && (
            <div
              style={{
                width: '100%',
                height: '6px',
                backgroundColor: '#edf2f7',
                borderRadius: '3px',
                overflow: 'hidden',
                marginTop: '0.4rem',
              }}
            >
              <div
                style={{
                  width: `${budgetUsagePercent}%`,
                  height: '100%',
                  backgroundColor: budgetUsagePercent >= 80 ? '#dd6b20' : '#3182ce',
                }}
              />
            </div>
          )}
        </div>

        {/* Tokens */}
        <div
          className="kpi-card"
          style={{
            padding: '0.75rem',
            backgroundColor: '#f7fafc',
            border: '1px solid #e2e8f0',
            borderRadius: '6px',
          }}
        >
          <span style={{ fontSize: '0.75rem', fontWeight: 600, color: '#718096' }}>
            TOKEN CONSUMPTION
          </span>
          <div style={{ fontSize: '1.25rem', fontWeight: 700, margin: '0.25rem 0' }} data-testid="kpi-total-tokens">
            {totalTokens.toLocaleString()}
          </div>
          <span style={{ fontSize: '0.75rem', color: '#718096' }}>
            Prompt: {totalPromptTokens.toLocaleString()} • Completion: {totalCompletionTokens.toLocaleString()}
          </span>
        </div>

        {/* Concurrency */}
        <div
          className="kpi-card"
          style={{
            padding: '0.75rem',
            backgroundColor: '#f7fafc',
            border: '1px solid #e2e8f0',
            borderRadius: '6px',
          }}
        >
          <span style={{ fontSize: '0.75rem', fontWeight: 600, color: '#718096' }}>
            ACTIVE CONCURRENCY
          </span>
          <div style={{ fontSize: '1.25rem', fontWeight: 700, margin: '0.25rem 0' }} data-testid="kpi-concurrency">
            {activeConcurrency} / {globalConcurrencyLimit}
          </div>
          <span style={{ fontSize: '0.75rem', color: '#718096' }}>
            Global slot saturation: {((activeConcurrency / Math.max(1, globalConcurrencyLimit)) * 100).toFixed(0)}%
          </span>
        </div>

        {/* Reliability */}
        <div
          className="kpi-card"
          style={{
            padding: '0.75rem',
            backgroundColor: '#f7fafc',
            border: '1px solid #e2e8f0',
            borderRadius: '6px',
          }}
        >
          <span style={{ fontSize: '0.75rem', fontWeight: 600, color: '#718096' }}>
            REQUEST SUCCESS RATE
          </span>
          <div style={{ fontSize: '1.25rem', fontWeight: 700, margin: '0.25rem 0' }} data-testid="kpi-success-rate">
            {successRate.toFixed(1)}%
          </div>
          <span style={{ fontSize: '0.75rem', color: '#718096' }}>
            {successfulCalls} succeeded • {failedCalls} failed
          </span>
        </div>
      </div>

      {/* Tabs */}
      <div
        className="tab-bar"
        role="tablist"
        style={{ display: 'flex', gap: '0.5rem', marginBottom: '1rem' }}
      >
        {(['OVERVIEW', 'PROVIDERS', 'TASKS', 'ATTEMPTS'] as const).map((tab) => (
          <button
            key={tab}
            type="button"
            role="tab"
            aria-selected={selectedTab === tab}
            onClick={() => setSelectedTab(tab)}
            style={{
              padding: '0.4rem 0.8rem',
              borderRadius: '4px',
              border: selectedTab === tab ? '1px solid #3182ce' : '1px solid #e2e8f0',
              backgroundColor: selectedTab === tab ? '#3182ce' : '#fff',
              color: selectedTab === tab ? '#fff' : '#4a5568',
              fontWeight: 600,
              fontSize: '0.8125rem',
              cursor: 'pointer',
            }}
          >
            {tab.charAt(0) + tab.slice(1).toLowerCase()}
          </button>
        ))}
      </div>

      {/* Tab 1: Providers Breakdown */}
      {(selectedTab === 'OVERVIEW' || selectedTab === 'PROVIDERS') && (
        <div style={{ marginBottom: '1.5rem' }}>
          <h4>Provider Concurrency & Cost</h4>
          <div className="table-responsive">
            <table className="table" style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.875rem' }}>
              <thead>
                <tr style={{ borderBottom: '2px solid #edf2f7', textAlign: 'left' }}>
                  <th style={{ padding: '0.5rem' }}>Provider</th>
                  <th style={{ padding: '0.5rem' }}>Active / Limit</th>
                  <th style={{ padding: '0.5rem' }}>Total Calls</th>
                  <th style={{ padding: '0.5rem' }}>Total Tokens</th>
                  <th style={{ padding: '0.5rem' }}>Total Cost</th>
                  <th style={{ padding: '0.5rem' }}>Error Rate</th>
                </tr>
              </thead>
              <tbody>
                {Object.keys(byProvider).length === 0 ? (
                  <tr>
                    <td colSpan={6} style={{ padding: '1rem', textAlign: 'center', color: '#a0aec0' }}>
                      No provider activity recorded yet.
                    </td>
                  </tr>
                ) : (
                  Object.values(byProvider).map((p) => (
                    <tr key={p.provider} style={{ borderBottom: '1px solid #edf2f7' }}>
                      <td style={{ padding: '0.5rem', fontWeight: 600 }}>{p.provider}</td>
                      <td style={{ padding: '0.5rem' }}>
                        {p.active_concurrency} / {p.concurrency_limit}
                      </td>
                      <td style={{ padding: '0.5rem' }}>{p.total_calls}</td>
                      <td style={{ padding: '0.5rem' }}>{p.total_tokens.toLocaleString()}</td>
                      <td style={{ padding: '0.5rem' }}>${p.total_cost_usd.toFixed(4)}</td>
                      <td style={{ padding: '0.5rem' }}>
                        <span className={`status ${p.error_rate === 0 ? 'good' : 'bad'}`}>
                          {(p.error_rate * 100).toFixed(1)}%
                        </span>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Tab 2: Task Usage */}
      {(selectedTab === 'OVERVIEW' || selectedTab === 'TASKS') && (
        <div style={{ marginBottom: '1.5rem' }}>
          <h4>Task-Level Resource Consumption</h4>
          <div className="table-responsive">
            <table className="table" style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.875rem' }}>
              <thead>
                <tr style={{ borderBottom: '2px solid #edf2f7', textAlign: 'left' }}>
                  <th style={{ padding: '0.5rem' }}>Task</th>
                  <th style={{ padding: '0.5rem' }}>Calls</th>
                  <th style={{ padding: '0.5rem' }}>Total Tokens</th>
                  <th style={{ padding: '0.5rem' }}>Cost</th>
                  <th style={{ padding: '0.5rem' }}>Avg Latency</th>
                </tr>
              </thead>
              <tbody>
                {Object.keys(byTask).length === 0 ? (
                  <tr>
                    <td colSpan={5} style={{ padding: '1rem', textAlign: 'center', color: '#a0aec0' }}>
                      No task telemetry recorded.
                    </td>
                  </tr>
                ) : (
                  Object.entries(byTask).map(([taskName, t]) => (
                    <tr key={taskName} style={{ borderBottom: '1px solid #edf2f7' }}>
                      <td style={{ padding: '0.5rem', fontWeight: 600 }}>{taskName}</td>
                      <td style={{ padding: '0.5rem' }}>
                        {t.successful_calls}/{t.total_calls} ok
                      </td>
                      <td style={{ padding: '0.5rem' }}>{t.total_tokens.toLocaleString()}</td>
                      <td style={{ padding: '0.5rem' }}>${t.total_cost_usd.toFixed(4)}</td>
                      <td style={{ padding: '0.5rem' }}>{t.avg_latency_ms.toFixed(0)} ms</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Tab 3: Recent Attempts & Fallback Chain */}
      {(selectedTab === 'OVERVIEW' || selectedTab === 'ATTEMPTS') && (
        <div>
          <h4>Recent Provider Attempt Chains & Fallbacks</h4>
          <div className="table-responsive">
            <table className="table" style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.8125rem' }}>
              <thead>
                <tr style={{ borderBottom: '2px solid #edf2f7', textAlign: 'left' }}>
                  <th style={{ padding: '0.5rem' }}>Task</th>
                  <th style={{ padding: '0.5rem' }}>Provider / Model</th>
                  <th style={{ padding: '0.5rem' }}>Latency</th>
                  <th style={{ padding: '0.5rem' }}>Status</th>
                  <th style={{ padding: '0.5rem' }}>Tokens</th>
                  <th style={{ padding: '0.5rem' }}>Cost</th>
                  <th style={{ padding: '0.5rem' }}>Fallback Details</th>
                </tr>
              </thead>
              <tbody>
                {recentAttempts.length === 0 ? (
                  <tr>
                    <td colSpan={7} style={{ padding: '1rem', textAlign: 'center', color: '#a0aec0' }}>
                      No recent attempts.
                    </td>
                  </tr>
                ) : (
                  recentAttempts.slice(-10).map((a) => (
                    <tr key={a.attempt_id} style={{ borderBottom: '1px solid #edf2f7' }}>
                      <td style={{ padding: '0.5rem', fontWeight: 500 }}>{a.task}</td>
                      <td style={{ padding: '0.5rem' }}>
                        {a.provider} ({a.model})
                      </td>
                      <td style={{ padding: '0.5rem' }}>{a.latency_ms.toFixed(0)} ms</td>
                      <td style={{ padding: '0.5rem' }}>
                        <span className={`status ${a.status === 'SUCCESS' ? 'good' : a.status === 'FALLBACK' ? 'warn' : 'bad'}`}>
                          {a.status}
                        </span>
                      </td>
                      <td style={{ padding: '0.5rem' }}>{a.total_tokens}</td>
                      <td style={{ padding: '0.5rem' }}>${a.estimated_cost.toFixed(4)}</td>
                      <td style={{ padding: '0.5rem', color: '#718096' }}>
                        {a.fallback_reason || a.error_code || '—'}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div
        className="footer-privacy-note"
        style={{
          marginTop: '1rem',
          padding: '0.5rem 0.75rem',
          backgroundColor: '#edf2f7',
          borderRadius: '4px',
          fontSize: '0.75rem',
          color: '#4a5568',
        }}
      >
        🔒 <strong>Privacy Assured:</strong> Telemetry metrics record only token counts, latencies, error codes, and cost estimations. Prompt content, personal information, and user facts are never stored in telemetry records.
      </div>
    </section>
  );
}
