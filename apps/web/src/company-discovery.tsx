import React, { useState } from 'react';

type Props = {
  busy: boolean;
  onDiscover: (companyName: string, companyWebsite: string) => Promise<void>;
};

export function CompanyDiscovery({busy, onDiscover}: Props) {
  const [companyName, setCompanyName] = useState('');
  const [companyWebsite, setCompanyWebsite] = useState('');
  return <section className="panel" aria-labelledby="company-discovery-heading">
    <h2 id="company-discovery-heading">Add a company to look for jobs</h2>
    <p>We inspect public pages only and honor the site’s access rules. The website you provide stays unverified until supported evidence confirms it.</p>
    <p className="muted">Supported job feeds can be imported for this company. Other recruiting systems may need review before they can be used.</p>
    <form onSubmit={event => {
      event.preventDefault();
      void onDiscover(companyName.trim(), companyWebsite.trim());
    }}>
      <div className="two-col">
        <label>Company name<input required disabled={busy} maxLength={200} value={companyName} onChange={event => setCompanyName(event.target.value)} /></label>
        <label>Company website (HTTPS)<input required disabled={busy} type="url" pattern="https://.*" maxLength={2000} placeholder="https://example.com" value={companyWebsite} onChange={event => setCompanyWebsite(event.target.value)} /></label>
      </div>
      <button className="primary" type="submit" disabled={busy}>Discover company jobs</button>
    </form>
  </section>;
}
