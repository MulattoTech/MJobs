import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  parseSourceRelationshipGraph,
  SourceRelationshipGraphPanel,
  type SourceRelationshipGraphResponse,
} from './source-relationship-graph';

const graph: SourceRelationshipGraphResponse = {
  profile_id: 'profile-a',
  nodes: [
    {id:'company:acme',type:'company',label:'Acme Systems',metadata:{company_id:'acme',domain:'private.example'}},
    {id:'company_job_source:source-1',type:'company_job_source',label:'Greenhouse · acme',metadata:{tenant:'private-tenant'}},
    {id:'job_posting:posting-1',type:'job_posting',label:'req-7',metadata:{source_job_id:'private-provider-id'}},
    {id:'canonical_job:job-1',type:'canonical_job',label:'Systems Administrator',metadata:{job_id:'private-job-id'}},
    {id:'application_route:route-1',type:'application_route',label:'Greenhouse · req-7',metadata:{route_id:'private-route-id'}},
    {id:'application:application-1',type:'application',label:'DRAFT',metadata:{application_id:'private-application-id'}},
  ],
  edges: [
    {id:'edge-private-company',source:'company:acme',target:'company_job_source:source-1',type:'company_source'},
    {id:'edge-private-source-posting',source:'company_job_source:source-1',target:'job_posting:posting-1',type:'source_posting'},
    {id:'edge-private-job-posting',source:'canonical_job:job-1',target:'job_posting:posting-1',type:'job_posting'},
    {id:'edge-private-route',source:'canonical_job:job-1',target:'application_route:route-1',type:'job_route'},
    {id:'edge-private-application',source:'application_route:route-1',target:'application:application-1',type:'route_application'},
  ],
};

type TestElement = React.ReactElement<{children?:React.ReactNode; [key:string]:any}>;
function findElement(node: React.ReactNode, predicate: (element: TestElement) => boolean): TestElement | null {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findElement(child, predicate);
      if (found) return found;
    }
    return null;
  }
  if (!React.isValidElement(node)) return null;
  const element=node as TestElement;
  if (predicate(element)) return element;
  return findElement(element.props.children, predicate);
}

describe('SourceRelationshipGraphPanel', () => {
  it('renders readable nodes and relationship links with a useful initial detail', () => {
    const html = renderToStaticMarkup(<SourceRelationshipGraphPanel profileId="profile-a" graph={graph}
      loading={false} error={false} onRetry={vi.fn()} onSelect={vi.fn()} />);

    for (const label of ['Acme Systems','Greenhouse · acme','req-7','Systems Administrator','Greenhouse · req-7','DRAFT',
      'Company', 'Employer source', 'Feed posting', 'Canonical job', 'Application route', 'Application',
      'Company → employer source','Employer source → feed posting','Canonical job → feed posting',
      'Canonical job → application route','Application route → application', 'Selected graph item',
      'Systems Administrator']) expect(html).toContain(label);
    for (const privateValue of ['private.example','private-tenant','private-provider-id','private-job-id',
      'private-route-id','private-application-id','edge-private-company','company:acme']) {
      expect(html).not.toContain(privateValue);
    }
  });

  it('selection opens the selected node detail without exposing its identifier or metadata', () => {
    const onSelect = vi.fn();
    const initial = SourceRelationshipGraphPanel({profileId:'profile-a',graph,loading:false,error:false,onRetry:vi.fn(),onSelect});
    const companyButton = findElement(initial, element => element.type === 'g' &&
      element.props['aria-label'] === 'Company: Acme Systems');
    expect(companyButton).not.toBeNull();
    companyButton?.props.onClick();
    expect(onSelect).toHaveBeenCalledExactlyOnceWith({kind:'node',id:'company:acme'});

    const selected = renderToStaticMarkup(<SourceRelationshipGraphPanel profileId="profile-a" graph={graph}
      selected={{kind:'node',id:'company:acme'}} loading={false} error={false}
      onRetry={vi.fn()} onSelect={vi.fn()} />);
    expect(selected).toContain('Selected graph item');
    expect(selected).toContain('Acme Systems');
    expect(selected).toContain('Company');
    expect(selected).not.toContain('private.example');
    expect(selected).not.toContain('company:acme');
  });

  it('selecting a relationship displays its safe relationship label and endpoint labels', () => {
    const onSelect = vi.fn();
    const initial = SourceRelationshipGraphPanel({profileId:'profile-a',graph,loading:false,error:false,onRetry:vi.fn(),onSelect});
    const relationshipButton = findElement(initial, element => element.type === 'button' &&
      element.props['aria-label'] === 'Canonical job → application route: Systems Administrator → Greenhouse · req-7');
    expect(relationshipButton).not.toBeNull();
    relationshipButton?.props.onClick();
    expect(onSelect).toHaveBeenCalledExactlyOnceWith({kind:'edge',id:'edge-private-route'});

    const selected = renderToStaticMarkup(<SourceRelationshipGraphPanel profileId="profile-a" graph={graph}
      selected={{kind:'edge',id:'edge-private-route'}} loading={false} error={false}
      onRetry={vi.fn()} onSelect={vi.fn()} />);
    expect(selected).toContain('Relationship');
    expect(selected).toContain('Canonical job → application route');
    expect(selected).toContain('Systems Administrator → Greenhouse · req-7');
    expect(selected).not.toContain('edge-private-route');
  });

  it('shows explicit loading, error, no-profile, and empty-graph states', () => {
    const render = (props: Partial<React.ComponentProps<typeof SourceRelationshipGraphPanel>>) => renderToStaticMarkup(
      <SourceRelationshipGraphPanel profileId="profile-a" graph={null} loading={false} error={false}
        onRetry={vi.fn()} onSelect={vi.fn()} {...props} />);
    expect(render({loading:true})).toContain('Loading source relationships');
    const errorHtml = render({error:true});
    expect(errorHtml).toContain('role="alert"');
    expect(errorHtml).toContain('Could not load source relationships');
    expect(errorHtml).toContain('Retry');
    expect(render({profileId:null})).toContain('Select a profile to view its source relationships');
    expect(render({graph:{profile_id:'profile-a',nodes:[],edges:[]}})).toContain('No source relationships are available for this profile yet');
    const mismatched = render({graph:{...graph,profile_id:'profile-b'}});
    expect(mismatched).toContain('does not match the selected profile');
    expect(mismatched).not.toContain('Acme Systems');
  });

  it('rejects responses for another profile and invalid node or edge references', () => {
    expect(() => parseSourceRelationshipGraph({...graph,profile_id:'profile-b'},'profile-a')).toThrow('GRAPH_SCOPE_MISMATCH');
    expect(() => parseSourceRelationshipGraph({...graph,nodes:[{id:'n',type:'unknown',label:'Unknown'}]},'profile-a')).toThrow('GRAPH_RESPONSE_INVALID');
    expect(() => parseSourceRelationshipGraph({...graph,edges:[{id:'e',source:'missing',target:'also-missing',type:'company_source'}]},'profile-a')).toThrow('GRAPH_RESPONSE_INVALID');
  });

  it('discards raw node metadata before rendering', () => {
    const parsed = parseSourceRelationshipGraph(graph,'profile-a');
    expect(parsed.nodes.every(node => !('metadata' in node))).toBe(true);
  });
});
