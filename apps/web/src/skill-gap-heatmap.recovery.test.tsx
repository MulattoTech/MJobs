import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { SkillGapHeatmap } from './skill-gap-heatmap';

function findElement(node: React.ReactNode, type: string): React.ReactElement<any> | undefined {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findElement(child, type);
      if (found) return found;
    }
    return undefined;
  }

  if (!React.isValidElement<any>(node)) return undefined;
  const element = node as React.ReactElement<{ children?: React.ReactNode }>;
  if (element.type === type) return element;
  return findElement(element.props.children, type);
}

describe('skill gap heatmap recovery states', () => {
  it('exposes loading as a busy, named region with a live status announcement', () => {
    const html = renderToStaticMarkup(<SkillGapHeatmap status="loading" />);

    expect(html).toContain('<section class="panel" aria-labelledby="skill-gap-title" aria-busy="true">');
    expect(html).toContain('<h2 id="skill-gap-title">Skill gap heatmap</h2>');
    expect(html).toContain('<p role="status">Loading skill gap heatmap');
  });

  it('keeps the no-jobs empty state inside the named heatmap region', () => {
    const html = renderToStaticMarkup(
      <SkillGapHeatmap status="ready" data={{ profile_version: 'a'.repeat(64), jobs_examined: 0, skills: [] }} />,
    );

    expect(html).toContain('<section class="panel" aria-labelledby="skill-gap-title">');
    expect(html).toContain('<h2 id="skill-gap-title">Skill gap heatmap</h2>');
    expect(html).toContain('No scored jobs to summarize.');
  });

  it('exposes errors as alerts and retry as a named button', () => {
    const html = renderToStaticMarkup(
      <SkillGapHeatmap status="error" error="Heatmap unavailable" onRetry={() => {}} />,
    );

    expect(html).toContain('<div class="alert" role="alert">');
    expect(html).toContain('Heatmap unavailable');
    expect(html).toContain('<button type="button">Retry</button>');
  });

  it('runs the supplied recovery callback when the retry control is activated', () => {
    const onRetry = vi.fn();
    const section = SkillGapHeatmap({ status: 'error', onRetry });
    const button = findElement(section.props.children, 'button');
    const activate = button?.props.onClick;

    expect(button?.props.type).toBe('button');
    expect(typeof activate).toBe('function');
    expect(onRetry).not.toHaveBeenCalled();

    (activate as (() => void) | undefined)?.();

    expect(onRetry).toHaveBeenCalledOnce();
  });
});
