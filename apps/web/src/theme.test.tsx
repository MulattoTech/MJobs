import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {describe, expect, it} from 'vitest';
import {readThemePreference, resolveTheme, saveThemePreference, ThemeControl} from './theme';

describe('workspace appearance', () => {
  it('follows the system until a valid explicit preference is saved', () => {
    for (const value of [null, '', 'invalid', '<script>', 'DARK']) {
      expect(readThemePreference({getItem: () => value})).toBe('system');
    }
    for (const value of ['light', 'dark', 'system'] as const) {
      expect(readThemePreference({getItem: () => value})).toBe(value);
    }
    expect(resolveTheme('system', true)).toBe('dark');
    expect(resolveTheme('system', false)).toBe('light');
    expect(resolveTheme('light', true)).toBe('light');
    expect(resolveTheme('dark', false)).toBe('dark');
  });

  it('handles denied reads and writes without breaking appearance controls', () => {
    expect(readThemePreference({getItem: () => {throw new Error('denied');}})).toBe('system');
    expect(saveThemePreference({setItem: () => {throw new Error('quota');}}, 'dark')).toBe(false);
    const saved: string[] = [];
    expect(saveThemePreference({setItem: (key, value) => saved.push(key, value)}, 'dark')).toBe(true);
    expect(saved).toEqual(['mjobs-theme', 'dark']);
  });

  it('renders a labeled native keyboard control without requiring browser globals', () => {
    const html = renderToStaticMarkup(<ThemeControl/>);
    expect(html).toContain('Appearance');
    expect(html).toContain('System');
    expect(html).toContain('Light');
    expect(html).toContain('Dark');
    expect(html).toContain('for="workspace-theme"');
    expect(html).toContain('<select id="workspace-theme"');
  });
});
