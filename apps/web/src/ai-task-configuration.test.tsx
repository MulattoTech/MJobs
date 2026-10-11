import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  AITaskConfigurationView,
  ORDERED_AI_TASKS,
  validateModelId,
  type AITaskId,
  type TaskModelAssignment,
} from './ai-task-configuration';

const mockDefaultTasks: Record<AITaskId, TaskModelAssignment> = {
  job_parsing: { provider: 'disabled', model: null },
  job_scoring: { provider: 'openai', model: 'gpt-4o-mini' },
  requirement_extraction: { provider: 'disabled', model: null },
  resume_tailoring: { provider: 'ollama', model: 'llama3.2' },
  cover_letters: { provider: 'disabled', model: null },
  free_form_answers: { provider: 'disabled', model: null },
  company_summarization: { provider: 'disabled', model: null },
};

describe('validateModelId', () => {
  it('allows null model when provider is disabled', () => {
    expect(validateModelId('disabled', null)).toBeNull();
    expect(validateModelId('disabled', '')).toBeNull();
  });

  it('rejects blank model when provider is enabled', () => {
    expect(validateModelId('openai', '')).toBe('Model identifier is required when provider is enabled.');
    expect(validateModelId('openai', '   ')).toBe('Model identifier is required when provider is enabled.');
    expect(validateModelId('openai', null)).toBe('Model identifier is required when provider is enabled.');
  });

  it('rejects secret token prefixes to avoid credential leak', () => {
    expect(validateModelId('openai', 'sk-live-secret-key')).toBe(
      'Model identifier cannot begin with a credential or secret token prefix.',
    );
    expect(validateModelId('openai', 'ghp_token')).toBe(
      'Model identifier cannot begin with a credential or secret token prefix.',
    );
  });

  it('accepts valid model identifiers', () => {
    expect(validateModelId('openai', 'gpt-4o-mini')).toBeNull();
    expect(validateModelId('ollama', 'llama3.2:latest')).toBeNull();
    expect(validateModelId('azure-openai', 'gpt-4o-deployment')).toBeNull();
  });
});

describe('AITaskConfigurationView Component', () => {
  it('renders all 7 authoritative tasks', () => {
    const html = renderToStaticMarkup(<AITaskConfigurationView tasks={mockDefaultTasks} />);

    expect(html).toContain('Job Parsing');
    expect(html).toContain('Job Scoring');
    expect(html).toContain('Requirement Extraction');
    expect(html).toContain('Resume Tailoring');
    expect(html).toContain('Cover Letters');
    expect(html).toContain('Free-form Answers');
    expect(html).toContain('Company Summarization');

    expect(ORDERED_AI_TASKS).toHaveLength(7);
  });

  it('displays fallback modes correctly', () => {
    const html = renderToStaticMarkup(<AITaskConfigurationView tasks={mockDefaultTasks} />);

    expect(html).toContain('DETERMINISTIC');
    expect(html).toContain('UNAVAILABLE');
    expect(html).toContain('Rule-based, explainable match scores and filters remain available.');
    expect(html).toContain('No new answer is generated; review and reuse only explicitly approved saved answers.');
  });

  it('displays assigned providers and models for active tasks', () => {
    const html = renderToStaticMarkup(<AITaskConfigurationView tasks={mockDefaultTasks} />);

    expect(html).toContain('gpt-4o-mini');
    expect(html).toContain('llama3.2');
    expect(html).toContain('OpenAI Official API');
    expect(html).toContain('Ollama (Local Endpoint)');
  });

  it('renders reset button when onResetAllToDisabled is supplied', () => {
    const html = renderToStaticMarkup(
      <AITaskConfigurationView tasks={mockDefaultTasks} onResetAllToDisabled={() => {}} />,
    );

    expect(html).toContain('Disable All (Safe Fallback)');
    expect(html).toContain('reset-all-disabled-btn');
  });
});
