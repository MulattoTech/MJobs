import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { MAIN_TABS, tabTitle } from './navigation';

const mainSource = readFileSync(new URL('./main.tsx', import.meta.url), 'utf8');
const navigationSource = readFileSync(new URL('./navigation.tsx', import.meta.url), 'utf8');
const indexSource = readFileSync(new URL('../index.html', import.meta.url), 'utf8');

describe('web entrypoint characterization', () => {
  it('keeps the existing primary navigation order, labels, and titles', () => {
    expect(MAIN_TABS.map(tab => [tab, tabTitle(tab)])).toEqual([
      ['Jobs', 'Find your next opportunity.'],
      ['Pipeline', 'Move opportunities forward.'],
      ['Profile', 'Your experience, in your words.'],
      ['Career Stories', 'Build reusable career stories.'],
      ['Learning', 'Understand your matches.'],
      ['Sources', 'Choose where to look.'],
      ['Applications', 'Every application, accounted for.'],
      ['Review Queue', 'Review application blockers.'],
      ['AI Settings', 'Choose models by task.'],
      ['Activity', 'See what is happening.'],
    ]);
  });

  it('keeps first-run actions mapped to their current tabs and anchors', () => {
    expect(navigationSource).toContain(
      "'create-profile':{tab:'Profile',anchor:'create-profile'}",
    );
    expect(navigationSource).toContain(
      "'upload-resume':{tab:'Profile',anchor:'upload-resume'}",
    );
    expect(navigationSource).toContain(
      "'review-extracted-facts':{tab:'Profile',anchor:'review-extracted-facts'}",
    );
    expect(navigationSource).toContain(
      "'configure-desired-jobs':{tab:'Sources',anchor:'configure-desired-jobs'}",
    );
    expect(navigationSource).toContain(
      "'configure-location':{tab:'Profile',anchor:'configure-location'}",
    );
    expect(navigationSource).toContain(
      "'configure-application-preferences':{tab:'Profile',anchor:'configure-application-preferences'}",
    );
    expect(navigationSource).toContain(
      "'configure-ai':{tab:'AI Settings',anchor:''}",
    );
    expect(navigationSource).toContain(
      "'enable-sources':{tab:'Sources',anchor:'enable-sources'}",
    );
  });

  it('keeps tab changes as the navigation cancellation seam', () => {
    expect(mainSource).toMatch(
      /function setTab\(next:Tab\) \{\s*\+\+navigationSequence\.current;\s*\+\+appSequence\.current;\s*setApplicationLoading\(null\);\s*applicationRestoreProfile\.current=profileRef\.current;\s*setTabState\(next\);\s*\}/,
    );
  });

  it('keeps the document root and startup initialization order', () => {
    expect(indexSource).toContain('<div id="root"></div>');
    expect(mainSource).toMatch(
      /initializeTheme\(\);\s*createRoot\(document\.getElementById\('root'\)!\)\.render\(<React\.StrictMode><App\/><\/React\.StrictMode>\);/,
    );
  });
});
