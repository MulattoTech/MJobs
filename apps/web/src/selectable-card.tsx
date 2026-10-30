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
  return <button
    type="button"
    className={`job-card${selected ? ' selected' : ''}`}
    aria-label={`${title} at ${company}`}
    onClick={onActivate}
  >
    <span className="card-top">
      <span className="eyebrow">{company}</span>
      <span className={badgeClassName}>{badge}</span>
    </span>
    <span className="job-card-title">{title}</span>
    <span className="job-card-summary">{summary}</span>
    {footerItems.length > 0 && <span className="card-foot">
      {footerItems.map((item, index) => <span className={item.className} key={`${item.text}-${index}`}>{item.text}</span>)}
    </span>}
    {provenance && <JobRouteProvenance {...provenance} />}
  </button>;
}
