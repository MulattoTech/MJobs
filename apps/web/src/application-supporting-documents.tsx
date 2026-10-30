import React, {useEffect, useRef, useState} from 'react';
import {api} from './api';

type AvailableDocument = {
  revision_id: string;
  document_kind: 'SUPPORTING_DOCUMENT'|'CERTIFICATION';
  label: string;
  version: number;
  filename: string;
  format: string;
  size_bytes: number;
};

type Selection = {
  profile_id: string;
  application_id: string;
  job_id: string;
  available: AvailableDocument[];
  selected_revision_ids: string[];
  unavailable_revision_ids: string[];
};

type Props = {
  profileId: string;
  applicationId: string;
  disabled?: boolean;
};

function endpoint(profileId: string, applicationId: string) {
  return `/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(applicationId)}/supporting-documents`;
}

export function ApplicationSupportingDocuments({profileId, applicationId, disabled = false}: Props) {
  const [selection, setSelection] = useState<Selection|null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [retrying, setRetrying] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const request = useRef<{key:string;id:string}|null>(null);
  const activeLoad = useRef<{scope:string;token:symbol}|null>(null);
  const scope = `${profileId}:${applicationId}`;

  async function load(isRetry = false) {
    if (activeLoad.current?.scope === scope) return;
    const token = Symbol(scope);
    activeLoad.current = {scope, token};
    const isActive = () => activeLoad.current?.token === token;
    setLoading(true);
    setRetrying(isRetry);
    setError('');
    try {
      const result = await api<Selection>(endpoint(profileId, applicationId));
      if (!isActive()) return;
      if (result.profile_id !== profileId || result.application_id !== applicationId) {
        throw new Error('Application document selection scope did not match.');
      }
      setSelection(result);
      setSelectedIds(result.selected_revision_ids);
      setNotice('');
    } catch {
      if (!isActive()) return;
      setSelection(null);
      setError('Supporting document choices could not be loaded. Retry to review this application.');
    } finally {
      if (isActive()) {
        activeLoad.current = null;
        setLoading(false);
        setRetrying(false);
      }
    }
  }

  useEffect(() => {
    request.current = null;
    setSelection(null);
    setSelectedIds([]);
    void load();
    // Scope keys intentionally restart the read when this panel is reused.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope]);

  function toggle(revisionId: string, checked: boolean) {
    setSelectedIds(current => checked
      ? Array.from(new Set([...current, revisionId])).sort()
      : current.filter(value => value !== revisionId));
    request.current = null;
    setNotice('');
  }

  async function save() {
    if (!selection) return;
    if (selectedIds.length > 5) {
      setError('Select no more than five supporting documents for one application.');
      return;
    }
    setSaving(true);
    setError('');
    setNotice('');
    const key = `${scope}:${selectedIds.join(',')}`;
    if (!request.current || request.current.key !== key) {
      request.current = {key, id: crypto.randomUUID()};
    }
    try {
      const result = await api<Selection & {replayed:boolean}>(endpoint(profileId, applicationId), {
        method: 'PUT',
        body: JSON.stringify({revision_ids: selectedIds, request_id: request.current.id}),
      });
      if (result.profile_id !== profileId || result.application_id !== applicationId
          || result.selected_revision_ids.length !== selectedIds.length
          || result.selected_revision_ids.some((id, index) => id !== selectedIds[index])) {
        throw new Error('Saved supporting document selection did not match this application.');
      }
      setSelection(result);
      setSelectedIds(result.selected_revision_ids);
      setNotice('Supporting document selection saved and confirmed for this application.');
      request.current = null;
    } catch {
      setError('Supporting document selection could not be confirmed. Retry the same selection.');
    } finally {
      setSaving(false);
    }
  }

  const available = selection?.available ?? [];
  const unavailable = selection?.unavailable_revision_ids ?? [];
  const staleSelected = selectedIds.filter(id => !available.some(row => row.revision_id === id));

  return <section className="panel application-supporting-documents" aria-label="Supporting documents for this mock application" aria-busy={loading}>
    <h3>Supporting documents for this mock application</h3>
    <p>Choose optional profile documents for this job. Files are stored as unreviewed; selection does not verify their contents. They are attached only in the controlled local mock workflow. External application uploads remain disabled.</p>
    {loading && <p role="status">Loading profile documents…</p>}
    {error && <p role="alert">{error}</p>}
    {(error || retrying) && <button type="button" disabled={disabled||loading}
      onClick={() => void load(true)}>{retrying?'Retrying supporting documents…':'Retry loading supporting documents'}</button>}
    {unavailable.length > 0 && <div role="alert">
      <p>One or more selected revisions are archived or unavailable. Remove them before saving this application’s selection.</p>
      <button type="button" disabled={disabled||saving} onClick={() => {
        setSelectedIds(ids => ids.filter(id => !unavailable.includes(id)));
        request.current = null;
      }}>Remove unavailable selections</button>
    </div>}
    {!loading && selection && <>
      {available.length === 0
        ? <p>No active supporting documents or certifications are stored for this profile.</p>
        : <fieldset disabled={disabled||saving}>
          <legend>Choose files for this job (up to five)</legend>
          {available.map(document => <label key={document.revision_id}>
            <input type="checkbox" checked={selectedIds.includes(document.revision_id)}
              onChange={event => toggle(document.revision_id, event.currentTarget.checked)} />
            {document.label} · {document.document_kind === 'CERTIFICATION' ? 'Certification' : 'Supporting document'}
            {' · v'}{document.version}{' · '}{document.filename}{' · '}{document.format.toUpperCase()}
          </label>)}
        </fieldset>}
      <button type="button" disabled={disabled||saving||loading||staleSelected.length>0}
        onClick={() => void save()}>{saving?'Saving selection…':'Save supporting document selection'}</button>{' '}
      <button type="button" disabled={disabled||saving||loading} onClick={() => void load()}>Reload selection</button>
    </>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}
