import React, { useCallback, useEffect, useState } from 'react';
import { api } from './api';
import { MANAGED_RETENTION_CATEGORIES, TransientRetention, isRetentionCleanupControl, type RetentionCleanupControl, type RetentionSnapshot } from './transient-retention';

const CATEGORY_MAP = {
  DEBUG_SCREENSHOT: 'debug_screenshots',
  TEMP_HTML: 'temporary_html',
  RAW_PAYLOAD: 'old_raw_payloads',
  BROWSER_ARTIFACT: 'temporary_browser_artifacts',
  GENERATED_TEMPORARY: 'temporary_generated_files',
  ROTATED_LOG: 'rotated_logs',
} as const;

type RetentionCategoryCode = keyof typeof CATEGORY_MAP;
type CountBytes = {candidate_count?:number;candidate_bytes?:number;removed_count?:number;removed_bytes?:number};
export type RetentionPreviewPayload = {
  policy: Array<{id:string;category:RetentionCategoryCode;pattern:string;max_age_days:number}>;
  preview: {status:'OK'|'BLOCKED'|'PARTIAL';dry_run:boolean;by_category:Partial<Record<RetentionCategoryCode,CountBytes>>};
  last_sweep: null | {status:'OK'|'PARTIAL';dry_run:boolean;removed_count:number;removed_bytes:number};
  cleanup_control?: {
    source:'PERSISTED_POLICY'|'SERVER_CONFIGURATION';
    state:'ALLOWED'|'DISABLED'|'REVIEW_REQUIRED'|'SERVER_DISABLED';
    policy_revision:number|null;
  };
};

function nonNegativeInteger(value: unknown): number {
  return Number.isSafeInteger(value) && typeof value === 'number' && value >= 0 ? value : 0;
}

function cleanupControlFromPayload(value: unknown): RetentionCleanupControl {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('RETENTION_CONTROL_INVALID');
  const raw = value as Record<string,unknown>;
  if (Object.keys(raw).length !== 3 || !Object.hasOwn(raw,'source') || !Object.hasOwn(raw,'state') || !Object.hasOwn(raw,'policy_revision')) {
    throw new Error('RETENTION_CONTROL_INVALID');
  }
  const control = {source:raw.source,state:raw.state,policyRevision:raw.policy_revision};
  if (!isRetentionCleanupControl(control)) throw new Error('RETENTION_CONTROL_INVALID');
  return control;
}

export function retentionSnapshotFromPreview(value: RetentionPreviewPayload): RetentionSnapshot {
  if (!value || !Array.isArray(value.policy) || !value.preview ||
      !['OK','BLOCKED','PARTIAL'].includes(value.preview.status) ||
      value.preview.dry_run !== true || typeof value.preview.by_category !== 'object' || !value.preview.by_category ||
      Array.isArray(value.preview.by_category)) {
    throw new Error('RETENTION_PREVIEW_INVALID');
  }
  const cleanupControl = Object.hasOwn(value,'cleanup_control') ? cleanupControlFromPayload(value.cleanup_control) : undefined;
  const rules = new Map<RetentionCategoryCode, number>();
  for (const rule of value.policy) {
    if (!rule || !Object.hasOwn(CATEGORY_MAP,rule.category) || !Number.isInteger(rule.max_age_days) ||
        rule.max_age_days < 1 || rule.max_age_days > 365) throw new Error('RETENTION_POLICY_INVALID');
    rules.set(rule.category, Math.min(rules.get(rule.category) ?? 365, rule.max_age_days));
  }
  const policies = MANAGED_RETENTION_CATEGORIES.map(category => {
    const code = (Object.keys(CATEGORY_MAP) as RetentionCategoryCode[]).find(key=>CATEGORY_MAP[key]===category.id)!;
    const age = rules.get(code);
    return {category:category.id,configured:age!==undefined,enabled:age!==undefined&&cleanupControl?.state==='ALLOWED',maxAgeDays:age??30};
  });
  const preview = MANAGED_RETENTION_CATEGORIES.map(category => {
    const code = (Object.keys(CATEGORY_MAP) as RetentionCategoryCode[]).find(key=>CATEGORY_MAP[key]===category.id)!;
    const row = value.preview.by_category[code];
    if (row !== undefined && (!row || typeof row !== 'object' || Array.isArray(row) ||
        [row.candidate_count,row.candidate_bytes].some(count => count !== undefined &&
          (typeof count !== 'number' || !Number.isSafeInteger(count) || count < 0)))) {
      throw new Error('RETENTION_PREVIEW_INVALID');
    }
    return {category:category.id,eligibleCount:nonNegativeInteger(row?.candidate_count),eligibleBytes:nonNegativeInteger(row?.candidate_bytes),
      available:value.preview.status!=='BLOCKED'&&row?.candidate_count!==undefined&&row.candidate_bytes!==undefined};
  });
  const last = value.last_sweep;
  if (last && (!['OK','PARTIAL'].includes(last.status) || typeof last.dry_run !== 'boolean')) {
    throw new Error('RETENTION_SWEEP_INVALID');
  }
  return {
    policies, preview, cleanupControl, previewStatus:value.preview.status,
    lastSweep: last ? {
      status:last.status==='OK'&&!last.dry_run?'SUCCEEDED':last.status==='PARTIAL'?'FAILED':'NOT_RUN',
      removedCount:nonNegativeInteger(last.removed_count),
      removedBytes:nonNegativeInteger(last.removed_bytes),
    } : {status:'NOT_RUN',removedCount:0,removedBytes:0},
  };
}

type RetentionRequest = (path:string) => Promise<unknown>;
const defaultRetentionRequest:RetentionRequest=path=>api<unknown>(path);
export function RetentionConnected({request=defaultRetentionRequest}:{request?:RetentionRequest}) {
  const [generation,setGeneration]=useState(0);
  const [state,setState]=useState<{status:'loading'|'ready'|'error';snapshot?:RetentionSnapshot}>({status:'loading'});
  const refresh=useCallback(()=>setGeneration(value=>value+1),[]);
  useEffect(()=>{
    let active=true;
    setState({status:'loading'});
    void request('/retention/preview').then(value=>{
      const snapshot=retentionSnapshotFromPreview(value as RetentionPreviewPayload);
      if(active)setState({status:'ready',snapshot});
    }).catch(()=>{if(active)setState({status:'error'});});
    return ()=>{active=false;};
  },[generation,request]);
  return <TransientRetention status={state.status} snapshot={state.snapshot} onRefresh={refresh}/>;
}
