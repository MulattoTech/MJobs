import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  AIProviderSettings,
  type AIProviderConfig,
} from './ai-provider-settings';

describe('AIProviderSettings', () => {
  const sampleProviders: AIProviderConfig[] = [
    {
      id: 'prov-openai',
      name: 'OpenAI API',
      providerType: 'openai',
      baseUrl: 'https://api.openai.com/v1',
      hasApiKey: true,
      maskedApiKey: 'sk-proj-...89ab',
      status: 'connected',
      isDefault: true,
      capabilities: {
        textGeneration: true,
        structuredOutput: true,
        streaming: false,
      },
      activeModel: 'gpt-4o',
      concurrencyLimit: 4,
    },
    {
      id: 'prov-local',
      name: 'Local Endpoint (LM Studio)',
      providerType: 'local',
      baseUrl: 'http://127.0.0.1:8080/v1',
      hasApiKey: false,
      status: 'connected',
      isDefault: false,
      capabilities: {
        textGeneration: true,
        structuredOutput: true,
        streaming: false,
      },
      activeModel: 'mistral-7b-instruct',
      concurrencyLimit: 2,
    },
    {
      id: 'prov-disabled',
      name: 'Disabled / No-AI',
      providerType: 'disabled',
      baseUrl: 'disabled://local',
      hasApiKey: false,
      status: 'disconnected',
      isDefault: false,
      capabilities: {
        textGeneration: false,
        structuredOutput: false,
        streaming: false,
      },
      activeModel: 'none',
      concurrencyLimit: 0,
    },
  ];

  it('renders provider list with status, capabilities, and masked keys', () => {
    const html = renderToStaticMarkup(<AIProviderSettings providers={sampleProviders} />);

    expect(html).toContain('AI Provider Infrastructure');
    expect(html).toContain('3 Configured');
    expect(html).toContain('OpenAI API');
    expect(html).toContain('Default Active');
    expect(html).toContain('sk-proj-...89ab');
    expect(html).toContain('Local Endpoint (LM Studio)');
    expect(html).toContain('http://127.0.0.1:8080/v1');
    expect(html).toContain('No Key Required (Local/Self-hosted)');
    expect(html).toContain('Text Gen');
    expect(html).toContain('Structured JSON');
    expect(html).toContain('4 slots');
    expect(html).toContain('2 slots');
  });

  it('renders action controls and default provider indicators', () => {
    const html = renderToStaticMarkup(
      <AIProviderSettings
        providers={sampleProviders}
        onSelectDefault={() => {}}
        onTestConnection={async () => true}
      />
    );

    expect(html).toContain('Primary Provider');
    expect(html).toContain('Set as Default');
    expect(html).toContain('Test Connection');
  });
});
