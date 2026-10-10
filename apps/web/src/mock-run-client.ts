export type MockRunScope = {profileId:string; applicationId:string};
export type MockRunIntent = {version:1; requestId:string; submit:boolean};
export type MockRunReceipt = {
  operation_id:string; state:string; error_code:string|null; replayed:boolean;
  input_fingerprint:string; external_submission_enabled:false;
};
export type IntentStorage = Pick<Storage,'getItem'|'setItem'|'removeItem'>;
export const mockRunTerminal = new Set(['COMPLETED','CANCELLED','BLOCKED','DEAD_LETTERED','DISCARDED','FAILED']);
const requestPattern = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const states = new Set(['QUEUED','RUNNING',...mockRunTerminal]);

export function mockRunStorageKey(scope:MockRunScope):string {
  return `mjobs-mock-run-v1:${encodeURIComponent(scope.profileId)}:${encodeURIComponent(scope.applicationId)}`;
}
export function mockRunEndpoint(scope:MockRunScope):string {
  return `/profiles/${encodeURIComponent(scope.profileId)}/applications/${encodeURIComponent(scope.applicationId)}/mock-run`;
}
export function loadMockRunIntent(storage:IntentStorage, scope:MockRunScope):MockRunIntent|null {
  const text=storage.getItem(mockRunStorageKey(scope));
  if(text===null)return null;
  let value:unknown;
  try {value=JSON.parse(text);} catch {throw new Error('Saved run identity is unreadable. Review Activity before starting another run.');}
  if(!value||typeof value!=='object'||Array.isArray(value))throw new Error('Saved run identity is invalid. Review Activity.');
  const row=value as Record<string,unknown>;
  if(row.version!==1||typeof row.requestId!=='string'||!requestPattern.test(row.requestId)||typeof row.submit!=='boolean'
      ||Object.keys(row).some(key=>!['version','requestId','submit'].includes(key)))
    throw new Error('Saved run identity is invalid. Review Activity.');
  return row as MockRunIntent;
}
export function saveMockRunIntent(storage:IntentStorage,scope:MockRunScope,intent:MockRunIntent):void {
  // Persistence must succeed before POST: a lost acknowledgement retains the same intent.
  storage.setItem(mockRunStorageKey(scope),JSON.stringify(intent));
}
export function forgetTerminalMockRun(storage:IntentStorage,scope:MockRunScope,receipt:MockRunReceipt|null):void {
  if(!receipt||!mockRunTerminal.has(receipt.state))throw new Error('Resolve the current run before starting a new one.');
  storage.removeItem(mockRunStorageKey(scope));
}
function safeError(response:Response,value:unknown):Error {
  const detail=(value as {detail?:{code?:unknown}}|null)?.detail;
  const code=detail&&typeof detail==='object'&&typeof detail.code==='string'&&/^[A-Z][A-Z0-9_]{0,99}$/.test(detail.code)?detail.code:'';
  return new Error(code||`Mock run request failed (${response.status}).`);
}
async function receipt(response:Response):Promise<MockRunReceipt> {
  const value:unknown=await response.json();
  if(!response.ok)throw safeError(response,value);
  const row=value as Partial<MockRunReceipt>|null;
  if(!row||typeof row.operation_id!=='string'||!requestPattern.test(row.operation_id)
      ||typeof row.state!=='string'||!states.has(row.state)||typeof row.replayed!=='boolean'
      ||typeof row.input_fingerprint!=='string'||!/^[0-9a-f]{64}$/.test(row.input_fingerprint)
      ||row.external_submission_enabled!==false
      ||(row.error_code!==null&&typeof row.error_code!=='string'))
    throw new Error('Run acknowledgement could not be verified. Keep the saved request and check status.');
  return row as MockRunReceipt;
}
export async function postMockRun(scope:MockRunScope,intent:MockRunIntent,request:typeof fetch=fetch):Promise<MockRunReceipt> {
  return receipt(await request(mockRunEndpoint(scope),{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({request_id:intent.requestId,submit:intent.submit})}));
}
export async function lookupMockRun(scope:MockRunScope,intent:MockRunIntent,request:typeof fetch=fetch):Promise<MockRunReceipt|null> {
  const response=await request(`${mockRunEndpoint(scope)}s/${encodeURIComponent(intent.requestId)}`,{method:'GET'});
  if(response.status===404)return null;
  return receipt(response);
}
export async function cancelMockRun(operationId:string,request:typeof fetch=fetch):Promise<void> {
  if(!requestPattern.test(operationId))throw new Error('Invalid operation identity.');
  const response=await request(`/operations/${encodeURIComponent(operationId)}/cancel`,{method:'POST'});
  if(!response.ok)throw safeError(response,await response.json());
}
