import {expect,it} from 'vitest';
import {aiAdmissionFailure} from './durable-ai-evaluation';

it('distinguishes disabled providers and reused intents from stale candidate plans',()=>{
  expect(aiAdmissionFailure({status:409,code:'AI_EVALUATION_PREVIEW_CHANGED'}).stale).toBe(true);
  const disabled=aiAdmissionFailure({status:409,code:'AI_PROVIDER_DISABLED'});
  expect(disabled.stale).toBe(false);expect(disabled.message).toContain('Configure and save');
  const conflict=aiAdmissionFailure({status:409,code:'AI_EVALUATION_IDENTITY_CONFLICT'});
  expect(conflict.stale).toBe(false);expect(conflict.message).toContain('Check saved operations');
  expect(aiAdmissionFailure(new Error('offline')).message).toContain('same request identity');
});
