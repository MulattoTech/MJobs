import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, expect, it, vi } from 'vitest';

afterEach(() => vi.unstubAllGlobals());

const modules = import.meta.glob<{ FirstRunWizard: React.ComponentType<any> }>('./first-run-wizard.tsx');

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
