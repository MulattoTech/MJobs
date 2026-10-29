import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { JSDOM } from 'jsdom';
import { describe, expect, it } from 'vitest';

function mobileRuleFor(selector: string): CSSStyleRule | undefined {
  const css = readFileSync(fileURLToPath(new URL('./style.css', import.meta.url)), 'utf8');
  const dom = new JSDOM(`<style>${css}</style>`);
  const sheet = dom.window.document.styleSheets.item(0);

  for (const rule of Array.from(sheet?.cssRules ?? [])) {
    if (rule.type !== dom.window.CSSRule.MEDIA_RULE) continue;

    const mediaRule = rule as CSSMediaRule;
    if (mediaRule.conditionText.replaceAll(' ', '') !== '(max-width:780px)') continue;

    const styleRule = Array.from(mediaRule.cssRules).find(
      (nestedRule): nestedRule is CSSStyleRule =>
        nestedRule.type === dom.window.CSSRule.STYLE_RULE &&
        (nestedRule as CSSStyleRule).selectorText === selector,
    );
    if (styleRule) return styleRule;
  }

  return undefined;
}

describe('responsive source enrollment', () => {
  it('stacks the multi-control source form at the mobile breakpoint', () => {
    const sourceForm = mobileRuleFor('.source-add');

    expect(sourceForm, 'expected a mobile .source-add rule').toBeDefined();
    expect(sourceForm?.style.display).toBe('grid');
    expect(sourceForm?.style.getPropertyValue('grid-template-columns')).toBe('minmax(0,1fr)');
  });
});
