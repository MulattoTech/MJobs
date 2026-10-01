export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request rejected (${response.status}). Check the fields and try again.`);
  return data as T;
}
export function resumeMediaType(filename: string): string {
  const extension = filename.split('.').pop()?.toLowerCase();
  const media: Record<string,string> = {
    pdf: 'application/pdf',
    docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    txt: 'text/plain',
  };
  if (!extension || !media[extension]) throw new Error('Choose a PDF, DOCX, or UTF-8 TXT resume.');
  return media[extension];
}
export async function uploadResume<T>(profileId: string, file: File): Promise<T> {
  if (!file.size || file.size > 10 * 1024 * 1024) throw new Error('Resume must be nonempty and no larger than 10 MiB.');
  const response = await fetch('/profiles/'+encodeURIComponent(profileId)+'/resumes', {
    method: 'POST', body: file,
    headers: {'Content-Type': resumeMediaType(file.name), 'X-Filename': encodeURIComponent(file.name)},
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Resume upload rejected (${response.status}).`);
  return data as T;
}
export function eligible(fact: {verified: boolean; user_approved: boolean; confidence: number; source: string}) {
  return fact.verified && fact.user_approved && fact.confidence >= 80 && fact.source.trim().length > 0;
}
export const terminal = new Set(['COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED']);
