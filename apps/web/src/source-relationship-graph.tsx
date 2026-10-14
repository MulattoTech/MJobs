import React, { useEffect, useMemo, useState } from 'react';
import { api } from './api';

const NODE_TYPES = {
  company: {label:'Company', column:0},
  company_job_source: {label:'Employer source', column:1},
  job_posting: {label:'Feed posting', column:2},
  canonical_job: {label:'Canonical job', column:3},
  application_route: {label:'Application route', column:4},
  application: {label:'Application', column:5},
} as const;
const EDGE_TYPES = {
  company_source: 'Company → employer source',
  source_posting: 'Employer source → feed posting',
  job_posting: 'Canonical job → feed posting',
  job_route: 'Canonical job → application route',
  route_application: 'Application route → application',
} as const;
const MAX_NODES = 300;
const MAX_EDGES = 600;
const NODE_WIDTH = 184;
const NODE_HEIGHT = 58;
const COLUMN_GAP = 210;
const ROW_GAP = 92;

export type SourceRelationshipNodeType = keyof typeof NODE_TYPES;
export type SourceRelationshipEdgeType = keyof typeof EDGE_TYPES;
export type SourceRelationshipGraphResponse = {
  profile_id: string;
  nodes: Array<{id:string; type:SourceRelationshipNodeType; label:string; metadata?:Record<string,unknown>}>;
  edges: Array<{id:string; source:string; target:string; type:SourceRelationshipEdgeType}>;
};
type SafeNode = {id:string; type:SourceRelationshipNodeType; label:string};
type SafeEdge = {id:string; source:string; target:string; type:SourceRelationshipEdgeType};
type SafeGraph = {profile_id:string; nodes:SafeNode[]; edges:SafeEdge[]; truncated:boolean};
export type SourceRelationshipSelection = {kind:'node'|'edge'; id:string};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function parseSourceRelationshipGraph(value: unknown, profileId: string): SafeGraph {
  if (!isRecord(value) || value.profile_id !== profileId) throw new Error('GRAPH_SCOPE_MISMATCH');
  if (!Array.isArray(value.nodes) || !Array.isArray(value.edges)) throw new Error('GRAPH_RESPONSE_INVALID');

  const nodeRows = value.nodes;
  const edgeRows = value.edges;
  if (nodeRows.length > 10000 || edgeRows.length > 20000) throw new Error('GRAPH_RESPONSE_INVALID');
  const nodes: SafeNode[] = [];
  const nodeIds = new Set<string>();
  for (const [index,row] of nodeRows.entries()) {
    if (!isRecord(row) || typeof row.id !== 'string' || !row.id || row.id.length > 512 ||
        typeof row.label !== 'string' || row.label.length > 512 ||
        typeof row.type !== 'string' || !Object.hasOwn(NODE_TYPES,row.type) || nodeIds.has(row.id)) {
      throw new Error('GRAPH_RESPONSE_INVALID');
    }
    nodeIds.add(row.id);
    if(index<MAX_NODES)nodes.push({id:row.id, type:row.type as SourceRelationshipNodeType, label:row.label.trim() || NODE_TYPES[row.type as SourceRelationshipNodeType].label});
  }
  const visibleNodeIds = new Set(nodes.map(node=>node.id));

  const edges: SafeEdge[] = [];
  const edgeIds = new Set<string>();
  for (const [index,row] of edgeRows.entries()) {
    if (!isRecord(row) || typeof row.id !== 'string' || !row.id || row.id.length > 512 || edgeIds.has(row.id) ||
        typeof row.source !== 'string' || !nodeIds.has(row.source) ||
        typeof row.target !== 'string' || !nodeIds.has(row.target) ||
        typeof row.type !== 'string' || !Object.hasOwn(EDGE_TYPES,row.type)) {
      throw new Error('GRAPH_RESPONSE_INVALID');
    }
    edgeIds.add(row.id);
    if(index<MAX_EDGES && visibleNodeIds.has(row.source) && visibleNodeIds.has(row.target)) {
      edges.push({id:row.id, source:row.source, target:row.target, type:row.type as SourceRelationshipEdgeType});
    }
  }
  return {profile_id:profileId, nodes, edges, truncated:nodeRows.length > nodes.length || edgeRows.length > edges.length};
}

function layoutNodes(nodes: SafeNode[]) {
  const positions = new Map<string,{x:number;y:number}>();
  const columnCounts = new Map<number,number>();
  for (const node of nodes) {
    const column = NODE_TYPES[node.type].column;
    const row = columnCounts.get(column) || 0;
    columnCounts.set(column,row + 1);
    positions.set(node.id,{x:20 + column * COLUMN_GAP,y:20 + row * ROW_GAP});
  }
  return {positions,height:Math.max(150,...Array.from(columnCounts.values(),count=>count * ROW_GAP + 20))};
}

function displayLabel(value: string): string {
  const clean = value.replace(/[\u0000-\u001f\u007f]/g,' ').replace(/\s+/g,' ').trim();
  return clean.length > 27 ? `${clean.slice(0,26)}…` : clean;
}

type PanelProps = {
  profileId: string | null;
  graph: SourceRelationshipGraphResponse | null;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
  selected?: SourceRelationshipSelection | null;
  onSelect: (selection: SourceRelationshipSelection) => void;
};

export function SourceRelationshipGraphPanel({profileId,graph,loading,error,onRetry,selected,onSelect}:PanelProps) {
  if (!profileId) return <section className="panel source-relationship-graph"><h2>Source relationship graph</h2>
    <p>Select a profile to view its source relationships.</p></section>;
  if (loading) return <section className="panel source-relationship-graph" aria-busy="true">
    <h2>Source relationship graph</h2><p role="status">Loading source relationships…</p></section>;
  if (error) return <section className="panel source-relationship-graph"><h2>Source relationship graph</h2>
    <p role="alert">Could not load source relationships for this profile.</p>
    <button type="button" onClick={onRetry}>Retry</button></section>;
  if (!graph) return <section className="panel source-relationship-graph"><h2>Source relationship graph</h2>
    <p role="alert">Source relationships are unavailable.</p><button type="button" onClick={onRetry}>Retry</button></section>;

  let safeGraph: SafeGraph;
  try {
    safeGraph = parseSourceRelationshipGraph(graph,profileId);
  } catch {
    return <section className="panel source-relationship-graph"><h2>Source relationship graph</h2>
      <p role="alert">This graph does not match the selected profile or has invalid relationship data.</p>
      <button type="button" onClick={onRetry}>Reload graph</button></section>;
  }
  if (!safeGraph.nodes.length) return <section className="panel source-relationship-graph">
    <h2>Source relationship graph</h2><p className="empty">No source relationships are available for this profile yet.</p></section>;

  const nodesById = new Map(safeGraph.nodes.map(node=>[node.id,node]));
  const {positions,height} = layoutNodes(safeGraph.nodes);
  const currentSelection = selected && (selected.kind === 'node'
    ? nodesById.has(selected.id)
    : safeGraph.edges.some(edge=>edge.id === selected.id)) ? selected : {kind:'node' as const,id:safeGraph.nodes[0].id};
  const selectedNode = currentSelection.kind === 'node' ? nodesById.get(currentSelection.id) : null;
  const selectedEdge = currentSelection.kind === 'edge' ? safeGraph.edges.find(edge=>edge.id === currentSelection.id) : null;

  return <section className="panel source-relationship-graph" aria-label="Source relationship graph">
    <div className="toolbar"><div><h2>Source relationship graph</h2>
      <p>Explore how this profile’s companies, feeds, jobs, routes, and applications connect.</p></div>
      <button type="button" onClick={onRetry}>Refresh graph</button></div>
    <div className="source-relationship-graph-grid" style={{display:'grid',gridTemplateColumns:'1fr',gap:18,alignItems:'start'}}>
      <div className="source-relationship-map" role="region" aria-label="Relationship map"
        style={{overflow:'auto',border:'1px solid var(--border)',borderRadius:8,padding:10,background:'var(--surface-subtle)'}}>
        <svg style={{display:'block',minWidth:1280,width:'100%',height:'auto'}} viewBox={`0 0 ${20 + 5 * COLUMN_GAP + NODE_WIDTH + 20} ${height}`} role="group" aria-label="Company and application relationships">
          {safeGraph.edges.map(edge=>{
            const source=positions.get(edge.source); const target=positions.get(edge.target);
            if(!source||!target)return null;
            const forward=source.x<=target.x;
            const x1=source.x+(forward?NODE_WIDTH:0); const x2=target.x+(forward?0:NODE_WIDTH);
            const y1=source.y+NODE_HEIGHT/2; const y2=target.y+NODE_HEIGHT/2;
            const bend=Math.max(28,Math.abs(x2-x1)*0.42);
            return <path key={edge.id} fill="none" stroke={currentSelection.kind==='edge'&&currentSelection.id===edge.id?'var(--good)':'var(--edge)'} strokeWidth={currentSelection.kind==='edge'&&currentSelection.id===edge.id?3:2}
              d={`M ${x1} ${y1} C ${x1+(forward?bend:-bend)} ${y1}, ${x2-(forward?bend:-bend)} ${y2}, ${x2} ${y2}`} />;
          })}
          {safeGraph.nodes.map(node=>{
            const point=positions.get(node.id)!;
            const active=currentSelection.kind==='node'&&currentSelection.id===node.id;
            return <g key={node.id} role="button" tabIndex={0} aria-label={`${NODE_TYPES[node.type].label}: ${node.label}`}
              aria-pressed={active} className={`source-relationship-node ${node.type}${active?' selected':''}`}
              onClick={()=>onSelect({kind:'node',id:node.id})}
              onKeyDown={event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();onSelect({kind:'node',id:node.id});}}}>
              <title>{`${NODE_TYPES[node.type].label}: ${node.label}`}</title>
              <rect x={point.x} y={point.y} width={NODE_WIDTH} height={NODE_HEIGHT} rx="8"
                fill={active?'var(--good-bg)':'var(--surface)'} stroke={active?'var(--good)':'var(--border)'} strokeWidth={active?3:1.5} />
              <text x={point.x+12} y={point.y+21} fill="var(--muted)" fontSize="11" fontWeight="700">{NODE_TYPES[node.type].label}</text>
              <text x={point.x+12} y={point.y+42} fill="var(--text)" fontSize="13" fontWeight="600">{displayLabel(node.label)}</text>
            </g>;
          })}
        </svg>
      </div>
      <aside className="source-relationship-details" aria-label="Selected graph item">
        <h3>Selected graph item</h3>
        {selectedNode&&<><span className="tag">{NODE_TYPES[selectedNode.type].label}</span>
          <p><strong>{selectedNode.label}</strong></p></>}
        {selectedEdge&&<><span className="tag">Relationship</span>
          <p><strong>{EDGE_TYPES[selectedEdge.type]}</strong></p>
          <p>{nodesById.get(selectedEdge.source)?.label} → {nodesById.get(selectedEdge.target)?.label}</p></>}
      </aside>
    </div>
    <section className="source-relationship-links" aria-label="Relationships" style={{marginTop:18,borderTop:'1px solid var(--border)',paddingTop:12}}>
      <h3>Relationships</h3>
      {safeGraph.edges.length ? <ul>{safeGraph.edges.map(edge=>{
        const source=nodesById.get(edge.source); const target=nodesById.get(edge.target);
        if(!source||!target)return null;
        const active=currentSelection.kind==='edge'&&currentSelection.id===edge.id;
        return <li key={edge.id}><button type="button" className={active?'selected':''} aria-pressed={active}
          aria-label={`${EDGE_TYPES[edge.type]}: ${source.label} → ${target.label}`}
          style={{display:'grid',gap:3,textAlign:'left',width:'100%',margin:'5px 0',borderColor:active?'var(--good)':undefined,background:active?'var(--good-bg)':undefined}}
          onClick={()=>onSelect({kind:'edge',id:edge.id})}>
          <span>{EDGE_TYPES[edge.type]}</span><small>{source.label} → {target.label}</small>
        </button></li>;
      })}</ul> : <p className="muted">No linked relationships are recorded yet.</p>}
    </section>
    {safeGraph.truncated&&<p className="muted" role="status">This large graph is limited to the first {safeGraph.nodes.length} nodes and {safeGraph.edges.length} relationships.</p>}
  </section>;
}

export function SourceRelationshipGraph({profileId}:{profileId:string|null}) {
  const [attempt,setAttempt]=useState(0);
  const [loaded,setLoaded]=useState<{profileId:string;graph:SourceRelationshipGraphResponse}|null>(null);
  const [loading,setLoading]=useState(!!profileId);
  const [error,setError]=useState(false);
  const [selected,setSelected]=useState<SourceRelationshipSelection|null>(null);
  const requestKey=useMemo(()=>profileId ? `/profiles/${encodeURIComponent(profileId)}/source-relationship-graph` : '',[profileId]);

  useEffect(()=>{
    let current=true;
    setSelected(null);
    setError(false);
    if(!profileId){setLoaded(null);setLoading(false);return ()=>{current=false;};}
    setLoading(true);
    setLoaded(null);
    void api<unknown>(requestKey).then(value=>{
      const graph=parseSourceRelationshipGraph(value,profileId) as SourceRelationshipGraphResponse;
      if(current)setLoaded({profileId,graph});
    }).catch(()=>{if(current)setError(true);})
      .finally(()=>{if(current)setLoading(false);});
    return ()=>{current=false;};
  },[profileId,requestKey,attempt]);

  const graph=loaded?.profileId===profileId?loaded.graph:null;
  return <SourceRelationshipGraphPanel profileId={profileId} graph={graph} loading={loading||!!profileId&&!graph&&!error}
    error={error} onRetry={()=>setAttempt(value=>value+1)} selected={selected} onSelect={setSelected}/>;
}
