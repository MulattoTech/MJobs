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
