import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const mainSource = readFileSync(new URL('./main.tsx', import.meta.url), 'utf8');
const indexSource = readFileSync(new URL('../index.html', import.meta.url), 'utf8');

describe('web entrypoint characterization', () => {
  it('keeps the existing primary navigation order and labels', () => {
    expect(mainSource).toContain(
      "type Tab = 'Jobs'|'Pipeline'|'Profile'|'Career Stories'|'Learning'|'Sources'|'Applications'|'Review Queue'|'AI Settings'|'Activity';",
    );
    expect(mainSource).toContain(
      "(['Jobs','Pipeline','Profile','Career Stories','Learning','Sources','Applications','Review Queue','AI Settings','Activity'] as Tab[])",
    );
    expect(mainSource).toContain(
      "tab==='Jobs'?'Find your next opportunity.':tab==='Pipeline'?'Move opportunities forward.':tab==='Profile'?'Your experience, in your words.':tab==='Career Stories'?'Build reusable career stories.':tab==='Learning'?'Understand your matches.':tab==='Sources'?'Choose where to look.':tab==='Applications'?'Every application, accounted for.':tab==='Review Queue'?'Review application blockers.':tab==='AI Settings'?'Choose models by task.':'See what is happening.'",
    );
  });

  it('keeps first-run actions mapped to their current tabs and anchors', () => {
    expect(mainSource).toContain(
      "'create-profile':{tab:'Profile',anchor:'create-profile'}",
    );
    expect(mainSource).toContain(
      "'upload-resume':{tab:'Profile',anchor:'upload-resume'}",
    );
    expect(mainSource).toContain(
      "'review-extracted-facts':{tab:'Profile',anchor:'review-extracted-facts'}",
    );
    expect(mainSource).toContain(
      "'configure-desired-jobs':{tab:'Sources',anchor:'configure-desired-jobs'}",
    );
    expect(mainSource).toContain(
      "'configure-location':{tab:'Profile',anchor:'configure-location'}",
    );
    expect(mainSource).toContain(
      "'configure-application-preferences':{tab:'Profile',anchor:'configure-application-preferences'}",
    );
    expect(mainSource).toContain(
      "'configure-ai':{tab:'AI Settings',anchor:''}",
    );
    expect(mainSource).toContain(
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
