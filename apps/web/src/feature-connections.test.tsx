import React from 'react';
import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { TransientRetention } from './transient-retention';
import { retentionSnapshotFromPreview, type RetentionPreviewPayload } from './feature-connections';

describe('RetentionConnected', () => {
  it('turns a fixed configured preview into the six-category user view', async () => {
    const payload: RetentionPreviewPayload = {
      policy: [{id:'screens',category:'DEBUG_SCREENSHOT',pattern:'*.png',max_age_days:30}],
      preview: {status:'OK',dry_run:true,by_category:{DEBUG_SCREENSHOT:{candidate_count:2,candidate_bytes:72}}},
      last_sweep: null,
    };
    const snapshot = retentionSnapshotFromPreview(payload);
    const html = renderToStaticMarkup(<TransientRetention status="ready" snapshot={snapshot} />);
    expect(html).toContain('2 items');
    expect(html).toContain('72 B');
    expect(html).toContain('Debug screenshots');
    expect(html).toContain('Last sweep: Not run yet');
    expect(html).not.toContain('Delete listed temporary data');
  });

  it('fails closed for malformed server summaries', () => {
    expect(() => retentionSnapshotFromPreview({policy:[],preview:{status:'PRIVATE',by_category:{}},last_sweep:null} as unknown as RetentionPreviewPayload)).toThrow();
  });
});
