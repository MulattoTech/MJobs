import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  LLMTelemetryDashboard,
  type AttemptRecord,
} from './llm-telemetry-dashboard';

describe('LLMTelemetryDashboard', () => {
  const sampleAttempts: AttemptRecord[] = [
    {
      attempt_id: 'att-1',
      task: 'job_scoring',
      provider: 'openai',
      model: 'gpt-4o',
      latency_ms: 150,
      status: 'RATE_LIMITED',
      prompt_tokens: 1200,
      completion_tokens: 0,
      total_tokens: 1200,
      estimated_cost: 0.003,
      fallback_reason: 'HTTP 429 rate limit; fallback to Claude',
      error_code: 'RATE_LIMIT_429',
      timestamp_utc: '2026-10-10T22:30:00Z',
    },
    {
      attempt_id: 'att-2',
      task: 'job_scoring',
      provider: 'anthropic',
      model: 'claude-3-5-sonnet',
      latency_ms: 450,
      status: 'SUCCESS',
      prompt_tokens: 1200,
      completion_tokens: 250,
      total_tokens: 1450,
      estimated_cost: 0.00735,
      timestamp_utc: '2026-10-10T22:30:01Z',
    },
  ];

  it('renders overall metrics, tokens, concurrency, and privacy disclosure', () => {
    const html = renderToStaticMarkup(
      <LLMTelemetryDashboard
        totalCalls={24}
        successfulCalls={23}
        failedCalls={1}
        activeConcurrency={2}
        globalConcurrencyLimit={8}
        totalPromptTokens={35000}
        totalCompletionTokens={5000}
        totalTokens={40000}
        totalCostUsd={0.145}
        budgetCeilingUsd={5.0}
        budgetRemainingUsd={4.855}
        budgetWarningTriggered={false}
        byProvider={{
          openai: {
            provider: 'openai',
            active_concurrency: 1,
            concurrency_limit: 4,
            total_calls: 14,
            total_tokens: 22000,
            total_cost_usd: 0.095,
            error_rate: 0.071,
          },
          ollama: {
            provider: 'ollama',
            active_concurrency: 1,
            concurrency_limit: 2,
            total_calls: 10,
            total_tokens: 18000,
            total_cost_usd: 0.0,
            error_rate: 0.0,
          },
        }}
        byTask={{
          job_scoring: {
            total_calls: 14,
            successful_calls: 13,
            failed_calls: 1,
            total_prompt_tokens: 20000,
            total_completion_tokens: 2000,
            total_tokens: 22000,
            total_cost_usd: 0.095,
            avg_latency_ms: 320,
          },
        }}
        recentAttempts={sampleAttempts}
      />
    );

    expect(html).toContain('AI TELEMETRY &amp; COST MONITOR (MJ-REF-015)');
    expect(html).toContain('$0.1450');
    expect(html).toContain('40,000');
    expect(html).toContain('2 / 8'); // Concurrency active / limit
    expect(html).toContain('95.8%'); // Success rate
    expect(html).toContain('openai');
    expect(html).toContain('ollama');
    expect(html).toContain('job_scoring');
    expect(html).toContain('HTTP 429 rate limit; fallback to Claude');
    expect(html).toContain('Privacy Assured:');
  });

  it('renders budget warning alert when threshold exceeded', () => {
    const html = renderToStaticMarkup(
      <LLMTelemetryDashboard
        totalCostUsd={4.25}
        budgetCeilingUsd={5.0}
        budgetRemainingUsd={0.75}
        budgetWarningTriggered={true}
      />
    );

    expect(html).toContain('Budget Warning');
    expect(html).toContain('$4.2500');
    expect(html).toContain('$0.75 left');
  });

  it('marks unknown usage and cost instead of presenting zero', () => {
    const html = renderToStaticMarkup(
      <LLMTelemetryDashboard
        totalCalls={1}
        totalTokens={0}
        totalCostUsd={0}
        unknownUsageCalls={1}
        unknownCostCalls={1}
        recentAttempts={[{
          attempt_id: 'unknown-1',
          task: 'scoring',
          provider: 'azure-openai',
          model: 'unpriced-deployment',
          latency_ms: 50,
          status: 'SUCCESS',
          prompt_tokens: null,
          completion_tokens: null,
          total_tokens: null,
          estimated_cost: null,
          usage_provenance: 'UNAVAILABLE',
          cost_provenance: 'UNAVAILABLE',
          timestamp_utc: '2026-11-07T00:00:00Z',
        }]}
      />
    );
    expect(html).toContain('1 unknown calls');
    expect(html).toContain('1 unknown');
    expect(html).toContain('Unknown');
    expect(html).not.toContain('$0.0000</td>');
  });

  it('labels rejected calls as not sent', () => {
    const html = renderToStaticMarkup(
      <LLMTelemetryDashboard recentAttempts={[{
        ...sampleAttempts[0],
        request_sent: false,
        prompt_tokens: 0,
        completion_tokens: 0,
        total_tokens: 0,
        estimated_cost: 0,
        usage_provenance: 'NOT_SENT',
        cost_provenance: 'NOT_SENT',
      }]} />
    );
    expect(html).toContain('Not sent');
  });

  it('handles empty state without crashes', () => {
    const html = renderToStaticMarkup(<LLMTelemetryDashboard />);

    expect(html).toContain('LLM Token, Cost &amp; Concurrency Telemetry');
    expect(html).toContain('$0.0000');
    expect(html).toContain('0 / 8');
  });
});
