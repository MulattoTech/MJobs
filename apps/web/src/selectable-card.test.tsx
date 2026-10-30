import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { getByRole } from '@testing-library/dom';
import { describe, expect, it } from 'vitest';
import { SelectableCard } from './selectable-card';

const mainSource = readFileSync(new URL('./main.tsx', import.meta.url), 'utf8');

function jobCardButtons(): string[] {
  return [...mainSource.matchAll(/<button\b(?=[^>]*job-card)[^>]*>[\s\S]*?<\/button>/g)].map(match => match[0]);
}

describe('selectable cards in the Jobs and Applications views', () => {
  it('do not use flow content inside buttons and provide an explicit accessible name', () => {
    const forbidden = new Set(['div', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6']);
    const invalidDescendants: string[] = [];
    const unnamedCards: string[] = [];

    for (const button of jobCardButtons()) {
      if (!/\baria-label=/.test(button)) unnamedCards.push(button.slice(0, 80));
      for (const match of button.matchAll(/<([a-z][a-z0-9]*)\b/g)) {
        if (forbidden.has(match[1])) invalidDescendants.push(match[1]);
      }
    }

    expect({ invalidDescendants, unnamedCards }).toEqual({ invalidDescendants: [], unnamedCards: [] });
    expect([...mainSource.matchAll(/<SelectableCard\b/g)]).toHaveLength(3);
  });

  it('renders a named native button with phrasing content only', () => {
    const html = renderToStaticMarkup(<SelectableCard
      company="Acme Corp"
      title="Platform Engineer"
      badge="92% · Strong"
      summary="Remote · Full time"
      footerItems={[{ text: 'Manual capture', className: 'tag' }, { text: '1 posting' }]}
      provenance={{ postingSources: ['HiringCafe'], routeProvider: 'Greenhouse' }}
      onActivate={() => undefined}
    />);
    const document = new JSDOM(html).window.document;
    const button = document.querySelector('button');

    expect(button?.getAttribute('type')).toBe('button');
    expect(button?.querySelector('div,p,h1,h2,h3,h4,h5,h6')).toBeNull();
    expect(getByRole(document.body, 'button', { name: 'Acme Corp Platform Engineer' })).toBe(button);
    for (const description of ['92% · Strong', 'Remote · Full time', 'Manual capture', '1 posting', 'HiringCafe', 'Greenhouse']) {
      expect(getByRole(document.body, 'button', { name: 'Acme Corp Platform Engineer', description: new RegExp(description) })).toBe(button);
    }
  });

  it('uses native click activation without custom keyboard emulation', async () => {
    let activations = 0;
    const dom = new JSDOM('<div id="root"></div>');
    const previousWindow = globalThis.window;
    const previousDocument = globalThis.document;
    globalThis.window = dom.window as unknown as Window & typeof globalThis;
    globalThis.document = dom.window.document;
    const root = createRoot(dom.window.document.querySelector('#root')!);

    try {
      await act(async () => root.render(<SelectableCard
        company="Acme Corp"
        title="Platform Engineer"
        badge="Ready"
        summary="Remote"
        onActivate={() => { activations += 1; }}
      />));
      const button = getByRole(dom.window.document.body, 'button', { name: 'Acme Corp Platform Engineer' }) as HTMLButtonElement;

      expect(button.type).toBe('button');
      expect(button.onkeydown).toBeNull();
      button.click();
      expect(activations).toBe(1);
    } finally {
      await act(async () => root.unmount());
      globalThis.window = previousWindow;
      globalThis.document = previousDocument;
    }
  });
});
