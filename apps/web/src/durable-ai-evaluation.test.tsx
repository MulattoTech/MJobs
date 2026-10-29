import {expect,it} from 'vitest';
import {aiAdmissionFailure,aiEvaluationFailure} from './durable-ai-evaluation';

it('requires a fresh plan after credential changes without claiming an in-flight call was recalled',()=>{
  expect(aiAdmissionFailure({code:'AI_PROVIDER_CREDENTIAL_CHANGED'}).stale).toBe(true);
  expect(aiEvaluationFailure('AI_PROVIDER_CREDENTIAL_CHANGED')).toContain('Review provider history');
  expect(aiEvaluationFailure('AI_PROVIDER_CREDENTIAL_UNAVAILABLE')).toContain('existing credential store');
  expect(aiEvaluationFailure('AI_PROVIDER_BINDING_INVALID')).toContain('could not be verified');
});

it('distinguishes disabled providers and reused intents from stale candidate plans',()=>{
  expect(aiAdmissionFailure({status:409,code:'AI_EVALUATION_PREVIEW_CHANGED'}).stale).toBe(true);
  const disabled=aiAdmissionFailure({status:409,code:'AI_PROVIDER_DISABLED'});
  expect(disabled.stale).toBe(false);expect(disabled.message).toContain('Configure and save');
  const conflict=aiAdmissionFailure({status:409,code:'AI_EVALUATION_IDENTITY_CONFLICT'});
  expect(conflict.stale).toBe(false);expect(conflict.message).toContain('Check saved operations');
  expect(aiAdmissionFailure(new Error('offline')).message).toContain('same request identity');
});

it.each(['AI_PROVIDER_UNAVAILABLE','AI_PROVIDER_TIMEOUT','AI_ERROR'])(
  'keeps non-AI work available and requires an explicit safe retry for %s',code=>{
    const message=aiEvaluationFailure(code);
    expect(message).toContain('Discovery and deterministic scoring remain available');
    expect(message).toContain('No partial advisory batch was published');
    expect(message).toContain('explicit retry is safe after recovery');
    expect(message).toContain('Automatic replay remains paused');
    expect(message).toContain('billable');
    expect(message).not.toContain(code);
  },
);

it('preserves BLOCKED as a terminal operation state without retry guidance',()=>{
  expect(aiEvaluationFailure('BLOCKED')).toBe(
    'Evaluation stopped: BLOCKED. No partial advisory batch was published.',
  );
});
