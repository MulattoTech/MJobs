import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  ExplainableMatchingBreakdown,
  type MatchFactorItemView,
} from './explainable-matching-breakdown';

describe('ExplainableMatchingBreakdown', () => {
  const canonicalPositiveFactors: MatchFactorItemView[] = [
    { points: 20, description: 'System Administrator title match', formatted_line: '+20 System Administrator title match' },
    { points: 12, description: 'Active Directory', formatted_line: '+12 Active Directory' },
    { points: 10, description: 'Windows Server', formatted_line: '+10 Windows Server' },
    { points: 8, description: 'PowerShell', formatted_line: '+8 PowerShell' },
    { points: 7, description: 'Microsoft 365', formatted_line: '+7 Microsoft 365' },
    { points: 6, description: 'VMware', formatted_line: '+6 VMware' },
    { points: 6, description: 'Remote US', formatted_line: '+6 Remote US' },
    { points: 5, description: 'salary range', formatted_line: '+5 salary range' },
    { points: 5, description: 'relevant infrastructure experience', formatted_line: '+5 relevant infrastructure experience' },
  ];

  const canonicalNegativeFactors: MatchFactorItemView[] = [
    { points: -5, description: "bachelor's preferred", formatted_line: "-5 bachelor's preferred" },
    { points: -3, description: 'Azure certification preferred', formatted_line: '-3 Azure certification preferred' },
  ];

  const canonicalFormattedText = `+20 System Administrator title match
+12 Active Directory
+10 Windows Server
+8 PowerShell
+7 Microsoft 365
+6 VMware
+6 Remote US
+5 salary range
+5 relevant infrastructure experience

-5 bachelor's preferred
-3 Azure certification preferred

Score: 91`;

  it('renders canonical 91/100 spec example with positive bonuses and deductions', () => {
    const html = renderToStaticMarkup(
      <ExplainableMatchingBreakdown
        positiveFactors={canonicalPositiveFactors}
        negativeFactors={canonicalNegativeFactors}
        totalScore={91}
        categoryLabel="Excellent Match"
        formattedText={canonicalFormattedText}
      />
    );

    expect(html).toContain('91/100');
    expect(html).toContain('Excellent Match');
    expect(html).toContain('+20');
    expect(html).toContain('System Administrator title match');
    expect(html).toContain('+12');
    expect(html).toContain('Active Directory');
    expect(html).toContain('-5');
    expect(html).toContain("bachelor&#x27;s preferred");
    expect(html).toContain('-3');
    expect(html).toContain('Azure certification preferred');
    expect(html).toContain('Copy Summary');
    expect(html).toContain('View Canonical Text');
  });

  it('renders positive sum and negative sum summaries accurately', () => {
    const html = renderToStaticMarkup(
      <ExplainableMatchingBreakdown
        positiveFactors={canonicalPositiveFactors}
        negativeFactors={canonicalNegativeFactors}
        totalScore={91}
        categoryLabel="Excellent Match"
        formattedText={canonicalFormattedText}
      />
    );

    // Sum of positives is 20+12+10+8+7+6+6+5+5 = 79
    expect(html).toContain('+79 pts');
    // Sum of deductions is 5+3 = 8
    expect(html).toContain('-8 pts');
  });
});
