import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { within } from '@testing-library/dom';
import { JSDOM } from 'jsdom';
import { afterEach, expect, it, vi } from 'vitest';

afterEach(() => vi.unstubAllGlobals());

const modules = import.meta.glob<{ FirstRunWizard: React.ComponentType<any> }>('./first-run-wizard.tsx');

function expectButtonsToHaveAccessibleNames(document: Document) {
  const buttons = [...document.querySelectorAll('button')];
  expect(buttons.length).toBeGreaterThan(0);
  const namedButtons = within(document.body).queryAllByRole('button', { name: /\S/ });
  expect(namedButtons).toHaveLength(buttons.length);
}

it('renders all ten setup steps and keeps external submission unavailable', async () => {
  const module = modules['./first-run-wizard.tsx'];
  expect(module, 'A connected first-run wizard component is required').toBeDefined();
  const { FirstRunWizard } = await module!();
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });
  const labels = [
    'Create profile', 'Upload resume', 'Review extracted facts', 'Configure desired jobs',
    'Configure location and remote preferences', 'Configure application preferences',
    'Configure AI', 'Enable sources', 'Choose automation level', 'Run first search',
  ];
  const steps = labels.map((label, index) => ({
    id: `step-${index + 1}`,
    label,
    description: `Description for ${label}`,
    complete: index === 0,
    action: index === 8 ? 'acknowledge' as const : index === 9 ? 'search' as const : 'navigate' as const,
    actionLabel: index === 8 ? 'Keep manual review only' : index === 9 ? 'Run first search' : `Open ${label}`,
  }));
  const html = renderToStaticMarkup(<FirstRunWizard
    profileId="profile-1"
    steps={steps}
    sources={[{ id: 'greenhouse:acme', name: 'Acme careers', provider: 'Greenhouse' }]}
    searches={[{ id: 'systems-admin', name: 'Systems Administrator' }]}
    selectedSourceId="greenhouse:acme"
    selectedSearchId="systems-admin"
    canRunSearch={false}
    busy={false}
    onNavigate={vi.fn()}
    onAcknowledge={vi.fn()}
    onSelectSource={vi.fn()}
    onSelectSearch={vi.fn()}
    onRunSearch={vi.fn()}
  />);

  expect(html).toContain('First-run setup');
  for (const step of steps) expect(html).toContain(step.label);
  expect(html).toContain('Manual review only');
  expect(html).toContain('External submission stays off');
  expect(html).toContain('Open Create profile');
  expect(html).toContain('Run first search');
  expect(html).toContain('Choose an enabled source');
  expect(html).toContain('First-run saved search profile');
  expect(html).not.toContain('Submit application');
  expect(html).not.toContain('Automatic applications');
});

it('keeps first-run controls keyboard-native and gives each control an accessible name', async () => {
  const { FirstRunWizard } = await modules['./first-run-wizard.tsx']!();
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() });
  const html = renderToStaticMarkup(<FirstRunWizard
    profileId="profile-1"
    steps={[
      { id: 'profile', label: 'Create profile', description: 'Add profile details.', complete: false,
        action: 'navigate', actionLabel: 'Open profile', skipLabel: 'Skip profile setup' },
      { id: 'review', label: 'Review facts', description: 'Acknowledge the facts.', complete: false,
        action: 'acknowledge', actionLabel: 'Confirm local review' },
      { id: 'search', label: 'Run first search', description: 'Choose saved search and source.', complete: false,
        action: 'search', actionLabel: 'Run first search' },
    ]}
    sources={[{ id: 'source-1', name: 'Example source', provider: 'Greenhouse' }]}
    searches={[{ id: 'search-1', name: 'Systems Administrator' }]}
    selectedSourceId="source-1" selectedSearchId="search-1" canRunSearch={true} busy={false}
    onNavigate={vi.fn()} onAcknowledge={vi.fn()} onSelectSource={vi.fn()} onSelectSearch={vi.fn()}
    onRunSearch={vi.fn()}
  />);

  const document = new JSDOM(html).window.document;
  expectButtonsToHaveAccessibleNames(document);
  for (const button of document.querySelectorAll('button')) {
    expect(button.getAttribute('type')).toBe('button');
  }
  expect(html).toContain('<label for="first-run-search-profile">First-run saved search profile</label>');
  expect(html).toContain('<label for="first-run-source">Enabled source for this search</label>');
  expect(html).not.toMatch(/<(?:div|span)[^>]*role="button"/);
  expect(html).not.toMatch(/<(?:button|select)[^>]*tabindex="-1"/i);
});

it('rejects buttons whose content does not provide an accessible name', () => {
  const unnamedMarkup = [
    '<button type="button"><span></span></button>',
    '<button type="button"><span aria-hidden="true">Hidden label</span></button>',
    '<button type="button"><span hidden>Hidden label</span></button>',
  ];
  for (const markup of unnamedMarkup) {
    const document = new JSDOM(markup).window.document;
    expect(() => expectButtonsToHaveAccessibleNames(document)).toThrow();
  }
});

it('keeps the collapsed first-run resume action a named, keyboard-native button', async () => {
  const { FirstRunWizard } = await modules['./first-run-wizard.tsx']!();
  vi.stubGlobal('localStorage', { getItem: () => '1', setItem: vi.fn(), removeItem: vi.fn() });
  const html = renderToStaticMarkup(<FirstRunWizard
    profileId="profile-1" steps={[]} sources={[]} searches={[]}
    selectedSourceId="" selectedSearchId="" canRunSearch={false} busy={false}
    onNavigate={vi.fn()} onAcknowledge={vi.fn()} onSelectSource={vi.fn()} onSelectSearch={vi.fn()}
    onRunSearch={vi.fn()}
  />);

  expect(html).toContain('<button type="button" class="quiet">Resume first-run setup (0/0)</button>');
  expect(html).not.toContain('role="button"');
});
