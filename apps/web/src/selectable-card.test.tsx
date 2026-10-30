import { readFileSync } from 'node:fs';
import { JSDOM } from 'jsdom';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
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
    const button = new JSDOM(html).window.document.querySelector('button');

    expect(button?.getAttribute('type')).toBe('button');
    expect(button?.getAttribute('aria-label')).toContain('Acme Corp');
    expect(button?.getAttribute('aria-label')).toContain('Platform Engineer');
    expect(button?.querySelector('div,p,h1,h2,h3,h4,h5,h6')).toBeNull();
    expect(button?.textContent).toContain('Acme Corp');
    expect(button?.textContent).toContain('Platform Engineer');
  });

  it('uses native click activation without custom keyboard emulation', () => {
    let activations = 0;
    const card = SelectableCard({
      company: 'Acme Corp',
      title: 'Platform Engineer',
      badge: 'Ready',
      summary: 'Remote',
      onActivate: () => { activations += 1; },
    });

    expect(card.props.type).toBe('button');
    expect(card.props.onKeyDown).toBeUndefined();
    card.props.onClick();
    expect(activations).toBe(1);
  });
});
