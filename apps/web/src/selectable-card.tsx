import { useId } from 'react';
import { JobRouteProvenance, type JobRouteProvenanceProps } from './job-route-provenance';

export type SelectableCardFooterItem = {
  text: string;
  className?: string;
};

export type SelectableCardProps = {
  company: string;
  title: string;
  badge: string;
  badgeClassName?: string;
  summary: string;
  footerItems?: readonly SelectableCardFooterItem[];
  provenance?: JobRouteProvenanceProps;
  selected?: boolean;
  onActivate: () => void;
};

export function SelectableCard({
  company,
  title,
  badge,
  badgeClassName,
  summary,
  footerItems = [],
  provenance,
  selected = false,
  onActivate,
}: SelectableCardProps) {
  const cardId = useId();
  const companyId = `${cardId}-company`;
  const titleId = `${cardId}-title`;
  const badgeId = `${cardId}-badge`;
  const summaryId = `${cardId}-summary`;
  const footerId = `${cardId}-footer`;
  const provenanceId = `${cardId}-provenance`;
  const descriptionIds = [badgeId, summaryId];
  if (footerItems.length > 0) descriptionIds.push(footerId);
  if (provenance) descriptionIds.push(provenanceId);

  return <button
    type="button"
    className={`job-card${selected ? ' selected' : ''}`}
    aria-labelledby={`${companyId} ${titleId}`}
    aria-describedby={descriptionIds.join(' ')}
    onClick={onActivate}
  >
    <span className="card-top">
      <span className="eyebrow" id={companyId}>{company}</span>
      <span className={badgeClassName} id={badgeId}>{badge}</span>
    </span>
    <span className="job-card-title" id={titleId}>{title}</span>
    <span className="job-card-summary" id={summaryId}>{summary}</span>
    {footerItems.length > 0 && <span className="card-foot" id={footerId}>
      {footerItems.map((item, index) => <span className={item.className} key={`${item.text}-${index}`}>{item.text}</span>)}
    </span>}
    {provenance && <span id={provenanceId}><JobRouteProvenance {...provenance} /></span>}
  </button>;
}
