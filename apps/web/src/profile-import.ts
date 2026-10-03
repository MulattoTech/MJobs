export type ImportedFact = { id?: string; fact_id?: string; kind: string; text: string; source?: string; confidence: number; verified?: boolean; user_approved?: boolean };
export type ImportedContact = { name?: string; email?: string; phones?: string[]; [key: string]: unknown };
export type ImportProfile = { name: string; email: string; contact?: Record<string, unknown>; facts: ImportedFact[] };

export function factKey(fact: ImportedFact): string { return fact.fact_id || fact.id || `${fact.kind}:${fact.text.trim().toLowerCase()}`; }
export function cleanLines(values: string[]): string[] { return values.map(value => value.trim()).filter(Boolean); }

export function addSelectedExtraction(profile: ImportProfile, extraction: { contact: ImportedContact; facts: ImportedFact[] }, selectedFacts: Set<string>, selectedContact: Set<string>, limit = 200): { profile: ImportProfile; added: number; skipped: number; truncated: boolean } {
  const facts = [...profile.facts];
  const existing = new Set(facts.map(factKey));
  let skipped = 0;
  let truncated = false;
  for (const fact of extraction.facts.filter(item => selectedFacts.has(factKey(item)))) {
    const key = factKey(fact);
    if (existing.has(key)) { skipped++; continue; }
    if (facts.length >= limit) { skipped++; truncated=true; continue; }
    existing.add(key);
    facts.push({ ...fact, id: fact.id || fact.fact_id, verified: false, user_approved: false, source: fact.source || 'Resume extraction' });
  }
  const contact = { ...(profile.contact || {}) };
  let name = profile.name;
  let email = profile.email;
  for (const key of selectedContact) {
    const value = extraction.contact[key];
    if (key === 'name' && typeof value === 'string') name = value;
    else if (key === 'email' && typeof value === 'string') email = value;
    else if (key === 'phones' && Array.isArray(value)) contact.phones = value;
    else if (typeof value === 'string' && value.trim()) contact[key] = value;
  }
  return { profile: { ...profile, name, email, contact, facts }, added: facts.length - profile.facts.length, skipped, truncated };
}
