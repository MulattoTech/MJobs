/* Time travel for canonical acceptance evidence, not inferred commit progress. */
(() => {
"use strict";
const $ = id => document.getElementById(id);
const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
}[c]));
const stamp = at => at ? new Date(at).toLocaleString() : "Unknown";
let timeline = [], chosenId = null, live = null, historical = null;
let manualRefresh = 0, openState = new Map(), busy = false, rawMarkup = "";
let knownLatest = null, generation = 0;
let campaignData = null, campaignBusy = false, campaignGeneration = 0, requirementCatalog = [];
const pct = metric => metric?.total ? (100 * metric.pass/metric.total).toFixed(1) : "0.0";
const sign = n => n > 0 ? "+"+n : String(n);
function panel() {
  let el = $("progressTimeTravel");
  if (el) return el;
  el = document.createElement("section");
  el.id="progressTimeTravel";
  el.className="progress-time-machine";
  const release = $("releaseContent");
  const summary = release.querySelector(".release-summary");
  if (summary) summary.after(el);
  else release.appendChild(el);
  return el;
}
async function request(url,opts) {
  const response=await fetch(url,{cache:"no-store",...opts});
  if(!response.ok)throw Error("HTTP "+response.status);
  return response.json();
}
const metric=(data,key) => data?.[key] || {pass:0,partial:0,not_run:0,total:0};
function selectedPoint() {
  return timeline.find(p=>p.id===Number(chosenId)) || null;
}
function barDetail(now,prev,label) {
  if(!prev)return label+": "+now.pass+"/"+now.total+" PASS; no older capture selected";
  return label+" | selected checkpoint "+stamp(selectedPoint()?.at)+": "+prev.pass+"/"+prev.total+
    " PASS, "+prev.partial+" PARTIAL, "+prev.not_run+" NOT_RUN → now "+
    now.pass+"/"+now.total+" PASS, "+now.partial+" PARTIAL, "+now.not_run+
    " NOT_RUN | Δ accepted "+sign(now.pass-prev.pass);
}
function addTrend(el,now,previous,label) {
  if(!el) return;
  const note=barDetail(now,previous,label);
  el.title=note;
  el.setAttribute("aria-label",note);
  const fill=el.querySelector(".fill, .release-meter>span");
  if(fill && previous) {
    const before=document.createElement("span");
    before.className="progress-historical-fill";
    before.style.width=pct(previous)+"%";
    before.title="Historical accepted "+previous.pass+"/"+previous.total;
    const container=fill.parentElement;
    container.classList.add("progress-compare-bar");
    container.insertBefore(before,fill);
    fill.classList.add("progress-current-fill");
  }
}
function groupDetails(kind,name,now,old,detail) {
  const items=detail?.data?.requirements || {};
  const matching=Object.values(items).filter(x=>x[kind==="areas"?"area":"priority"]===name);
  const containsCampaign=matching.some(item=>item.id==="MJ-REQ-292");
  matching.sort((a,b)=>{
    if(containsCampaign && ((kind==="priorities"&&name==="P0") || kind==="areas")) {
      const campaignOrder=Number(b.id==="MJ-REQ-292")-Number(a.id==="MJ-REQ-292");
      if(campaignOrder)return campaignOrder;
    }
    return a.status.localeCompare(b.status)||a.id.localeCompare(b.id);
  });
  const prior=historical?.data?.requirements||{};
  const entries=matching.map(item=>{
    const from=prior[item.id]?.status;
    const change=from && from!==item.status ? ` <span class="progress-transition">${escape(from)} → ${escape(item.status)}</span>` : "";
    const campaign=campaignFor(item.id);
    const catalogTitle=requirementTitle(item.id);
    const title=catalogTitle || item.title || campaign?.title || item.id;
    const disclosure=campaign ? campaignDisclosure(campaign) : item.id==="MJ-REQ-292" ? campaignUnavailable() : "";
    const score=campaign ? `<span class="campaign-row-score">${Number(campaign.scoreboard?.verified_pass||0)} verified · ${Number(campaign.scoreboard?.PARTIAL||0)} partial · ${Number(campaign.scoreboard?.NOT_RUN||0)} not run</span>` : "";
    return `<div class="progress-item"><span class="progress-item-identity"><strong>${escape(title)}</strong><code>${escape(item.id)}</code>${score}</span><span>${escape(item.status)}${change}</span>${disclosure}</div>`;
  }).join("");
  const diff=old ? `<strong>Δ accepted ${sign(now.pass-old.pass)}</strong> since ${escape(stamp(selectedPoint()?.at))}` : "No comparison snapshot selected";
  return `<div class="progress-drilldown"><div>${diff}. ${now.partial} PARTIAL · ${now.not_run} NOT_RUN · ${now.total} total.</div>
    <div class="progress-item-list">${entries||"No items in this group."}</div></div>`;
}
function campaignFor(requirementId) {
  if (!campaignData?.available || !Array.isArray(campaignData.campaigns)) return null;
  return campaignData.campaigns.find(item=>item?.requirement_id===requirementId) || null;
}
function requirementTitle(requirementId) {
  const catalog=requirementCatalog.length?requirementCatalog:campaignData?.requirements;
  if(Array.isArray(catalog)) return catalog.find(item=>item?.id===requirementId)?.title || "";
  if(catalog && typeof catalog==="object") {
    const item=catalog[requirementId];
    return typeof item==="string"?item:item?.title||"";
  }
  return "";
}
function campaignUnavailable() {
  const message=campaignData?.error||(campaignData?.available?"No MJ-REQ-292 campaign is present in this snapshot.":"Campaign details are loading.");
  const open=openState.get("campaign:MJ-REQ-292")===true?" open":"";
  return `<details class="campaign-details" data-key="campaign:MJ-REQ-292"${open}><summary>Campaign details · expand phases and steps</summary><p class="campaign-unavailable">Campaign details unavailable: ${escape(message)}</p></details>`;
}
function safeIssueUrl(value) {
  try {
    const url=new URL(value);
    return url.protocol==="https:" && ["github.com","www.github.com"].includes(url.hostname) ? url.href : "";
  } catch { return ""; }
}
function campaignStatus(status) {
  const value=String(status||"NOT_RUN").toUpperCase();
  if (["PASS","VERIFIED_PASS","VERIFIED"].includes(value)) return "verified";
  if (value==="PARTIAL") return "partial";
  if (value==="FAIL") return "fail";
  if (["BLOCKED","BLOCKED_EXTERNAL","BLOCKED_ENVIRONMENT"].includes(value)) return "blocked";
  return "not-run";
}
function statusBar(counts,total,label) {
  const segments=[
    ["verified",Number(counts.verified_pass||0),"verified pass"],
    ["partial",Number(counts.PARTIAL||0),"partial"],
    ["not-run",Number(counts.NOT_RUN||0),"not run"],
    ["fail",Number(counts.FAIL||0),"fail"],
    ["blocked",Number(counts.BLOCKED||0),"blocked"],
  ].filter(([,count])=>count>0);
  const denominator=Math.max(0,Number(total)||0);
  const pieces=segments.map(([kind,count,name])=>{
    const width=denominator?Math.min(100,count/denominator*100):0;
    return `<span class="campaign-segment ${kind}" style="width:${width}%" title="${count} ${name}"></span>`;
  }).join("");
  const summary=segments.map(([,count,name])=>`${count} ${name}`).join(", ") || "No step status recorded";
  return `<span class="campaign-status-bar" role="img" aria-label="${escape(label)}: ${escape(summary)}">${pieces}</span>`;
}
function phaseCounts(phase) {
  const counts={verified_pass:0,PARTIAL:0,NOT_RUN:0,FAIL:0,BLOCKED:0};
  for(const step of (Array.isArray(phase?.steps)?phase.steps:[])) {
    const status=campaignStatus(step?.status);
    if(status==="verified") counts.verified_pass++;
    else if(status==="partial") counts.PARTIAL++;
    else if(status==="fail") counts.FAIL++;
    else if(status==="blocked") counts.BLOCKED++;
    else counts.NOT_RUN++;
  }
  return counts;
}
function stepDetails(step,campaignId) {
  const number=Number(step?.number);
  const key=`campaign:${campaignId}:step:${Number.isFinite(number)?number:escape(step?.title||"unknown")}`;
  const details=document.createElement("details");
  details.className="campaign-step";
  details.dataset.key=key;
  details.open=openState.get(key)===true;
  const summary=document.createElement("summary");
  const title=document.createElement("span");
  title.textContent=`${Number.isFinite(number)?`Step ${number}`:"Step"}: ${step?.title||"Untitled step"}`;
  const status=document.createElement("span");
  status.className=`campaign-status ${campaignStatus(step?.status)}`;
  status.textContent=String(step?.status||"NOT_RUN");
  summary.append(title,status);
  details.append(summary);
  const evidence=document.createElement("div");
  evidence.className="campaign-step-evidence";
  const fields=[
    ["Observed result",step?.observed_result],
    ["Remaining evidence gap",step?.remaining_gap||step?.missing_behavior],
    ["Tested commit",step?.tested_commit],
    ["Executed",step?.executed_at_utc ? stamp(step.executed_at_utc) : ""],
    ["Review state",step?.review_status],
    ["Merged acceptance",step?.merge_status],
  ];
  for(const [label,value] of fields) {
    if(value==null || value==="") continue;
    const row=document.createElement("div");
    const name=document.createElement("strong"); name.textContent=label+": ";
    const text=document.createElement("span"); text.textContent=String(value);
    row.append(name,text); evidence.append(row);
  }
  details.append(evidence);
  details.addEventListener("toggle",()=>rememberOpen(key,details.open));
  return details;
}
function campaignDisclosure(campaign) {
  const id=campaign.requirement_id;
  const key=`campaign:${id}`;
  const details=document.createElement("details");
  details.className="campaign-details";
  details.dataset.key=key;
  details.open=openState.get(key)===true;
  const summary=document.createElement("summary");
  summary.textContent="Campaign evidence · expand phases and steps";
  details.append(summary);
  const body=document.createElement("div");
  body.className="campaign-body";
  const score=campaign.scoreboard||{};
  const countline=document.createElement("p");
  countline.className="campaign-scoreboard";
  countline.textContent=`${Number(score.verified_pass||0)} verified · ${Number(score.PARTIAL||0)} partial · ${Number(score.NOT_RUN||0)} not run · ${Number(score.FAIL||0)} fail · ${Number(score.BLOCKED||0)} blocked · ${Number(score.total||0)} total`;
  body.append(countline);
  const meta=document.createElement("p");
  meta.className="campaign-checkpoint";
  meta.textContent=`Working checkpoint (recorded snapshot, not live activity): ${campaign.source_revision||"unknown revision"} (${campaign.source_kind||"recorded snapshot"}) · handoff revision ${campaign.handoff_revision||"not recorded"} · ledger updated ${stamp(campaign.updated_at_utc)} · dashboard observed ${stamp(campaignData?.observed_at)}. Merged acceptance: each step's merge state is shown separately.`;
  body.append(meta);
  if(campaign.routing_state) {
    const state=document.createElement("p"); state.className="campaign-routing-state";
    state.textContent=`Last recorded checkpoint state: ${String(campaign.routing_state)}`+
      (campaign.routing_state_scope?` (${String(campaign.routing_state_scope)})`:"");
    body.append(state);
  }
  if(campaign.acceptance_claim_scope) {
    const scope=document.createElement("p"); scope.className="campaign-scope";
    scope.textContent=String(campaign.acceptance_claim_scope); body.append(scope);
  }
  const href=safeIssueUrl(campaign.issue_url);
  if(href) {
    const link=document.createElement("a"); link.href=href; link.target="_blank"; link.rel="noopener noreferrer";
    link.textContent="Open linked issue"; body.append(link);
  }
  const phases=Array.isArray(campaign.phases)?campaign.phases:[];
  const list=document.createElement("div"); list.className="campaign-phases";
  for(const phase of phases) {
    const phaseKey=`campaign:${id}:phase:${phase?.id||phase?.label||"unknown"}`;
    const phaseDetails=document.createElement("details");
    phaseDetails.className="campaign-phase"; phaseDetails.dataset.key=phaseKey;
    phaseDetails.open=openState.get(phaseKey)===true;
    const phaseSummary=document.createElement("summary");
    const label=document.createElement("span"); label.className="campaign-phase-label";
    label.textContent=String(phase?.label||phase?.id||"Phase");
    const counts=phaseCounts(phase), total=(Array.isArray(phase?.steps)?phase.steps:[]).length;
    phaseSummary.append(label);
    const bar=document.createElement("span"); bar.innerHTML=statusBar(counts,total,label.textContent);
    phaseSummary.append(bar.firstElementChild);
    const countText=document.createElement("span"); countText.className="campaign-phase-counts";
    countText.textContent=`${counts.verified_pass} verified · ${counts.PARTIAL} partial · ${counts.NOT_RUN} not run · ${counts.FAIL} fail · ${counts.BLOCKED} blocked`;
    phaseSummary.append(countText); phaseDetails.append(phaseSummary);
    const steps=document.createElement("div"); steps.className="campaign-steps";
    for(const step of (Array.isArray(phase?.steps)?phase.steps:[])) steps.append(stepDetails(step,id));
    phaseDetails.append(steps);
    phaseDetails.addEventListener("toggle",()=>rememberOpen(phaseKey,phaseDetails.open));
    list.append(phaseDetails);
  }
  body.append(list);
  const notes=[];
  for(const [label,value] of [["Blockers",campaign.blockers], ["Next action",campaign.next_action]]) {
    if((Array.isArray(value)&&value.length) || (typeof value==="string"&&value)) {
      const item=document.createElement("p"); item.className="campaign-next-action";
      item.textContent=`${label}: ${Array.isArray(value)?value.join(" · "):value}`; notes.push(item);
    }
  }
  for(const note of notes) body.append(note);
  details.append(body);
  details.addEventListener("toggle",()=>rememberOpen(key,details.open));
  return details.outerHTML;
}
function decorateSections() {
  const container=$("releaseContent");
  if(!container || !live)return;
  if(rawMarkup) container.innerHTML=rawMarkup;
  controls();
  const compare=selectedPoint();
  const metrics=[
    [".release-main-metric","original","Original requirements"],
    [".release-side-metric:nth-child(2)","reference","Additional reference gaps"],
    [".release-side-metric:nth-child(3)","release","Release readiness"]
  ];
  for(const [selector,kind,label] of metrics) {
    const card=container.querySelector(selector);
    if(!card)continue;
    const id="metric:"+kind;
    const details=document.createElement("details");
    details.className="progress-expandable progress-card-details";
    details.dataset.key=id;
    details.open=openState.has(id) ? openState.get(id) : false;
    const summary=document.createElement("summary");
    summary.className="progress-card-summary";
    while(card.firstChild)summary.appendChild(card.firstChild);
    details.appendChild(summary);
    const content=document.createElement("div");
    content.className="progress-card-extra";
    if(kind==="release"){
      content.innerHTML=`<div>Current milestone: ${escape(live.milestone)}. Historical readiness: ${escape(compare?.release_readiness||"No prior capture")}.</div><div>GitHub and checkout states are listed below; readiness is not determined from commit count.</div>`;
    } else {
      const value=metric(live,kind);
      const prev=metric(compare,kind);
      addTrend(summary.querySelector(".release-meter"),value,compare?prev:null,label);
      const delta=compare ? `Accepted change: <strong>${sign(value.pass-prev.pass)}</strong> compared with ${escape(stamp(compare.at))}` : "Capture or select an earlier checkpoint to compare.";
      content.innerHTML=`<div>${delta}</div><div>PASS ${value.pass} · PARTIAL ${value.partial} · NOT_RUN ${value.not_run} · TOTAL ${value.total}</div>`;
      if(kind==="reference"){
        const refs=Object.values(liveDetail?.data?.references||{}).sort((a,b)=>a.id.localeCompare(b.id));
        const old=historical?.data?.references||{};
        content.innerHTML+=`<div class="progress-item-list">${refs.map(x=>
          `<div class="progress-item"><code>${escape(x.id)}</code><span>${escape(x.status)}
          ${old[x.id]&&old[x.id].status!==x.status?`<b class="progress-transition">${escape(old[x.id].status)} → ${escape(x.status)}</b>`:""}</span></div>`
        ).join("")}</div>`;
      } else {
        content.innerHTML+=`<div>Expand individual feature areas or priorities below for the requirement IDs and state changes.</div>`;
      }
    }
    details.appendChild(content);
    card.appendChild(details);
    details.addEventListener("toggle",()=>rememberOpen(id,details.open));
  }
  for(const [kind,index] of [["areas",0],["priorities",1]]) {
    const column=container.querySelectorAll(".release-details > div")[index];
    if(!column)continue;
    const colKey="column:"+kind;
    const outer=document.createElement("details");
    outer.className="progress-expandable progress-group";
    outer.dataset.key=colKey;
    outer.open=openState.has(colKey) ? openState.get(colKey) : true;
    const title=column.querySelector("h3");
    const header=document.createElement("summary");
    header.textContent=title?.textContent||kind;
    title?.remove();
    outer.appendChild(header);
    const content=document.createElement("div");
    const rows=Array.from(column.querySelectorAll(".area-progress"));
    for(const row of rows) {
      const label=row.querySelector(".name")?.textContent||"";
      const normalized=kind==="areas"?label.replaceAll(" ","_"):label;
      const now=(kind==="areas"?live.areas:live.priorities).find(x=>x.name===normalized);
      if(!now)continue;
      const previous=(compare?.[kind]||{})[normalized] || null;
      const rid=kind+":"+normalized;
      const expandable=document.createElement("details");
      expandable.className="progress-expandable progress-group-item";
      expandable.dataset.key=rid;
      expandable.open=openState.has(rid) ? openState.get(rid) : false;
      const summary=document.createElement("summary");
      summary.appendChild(row);
      const current=row.querySelector(".count");
      if(current && compare) {
        const change=document.createElement("span");
        change.className="progress-delta";
        const delta=now.pass-(previous?.pass||0);
        change.textContent=" "+sign(delta);
        change.classList.add(delta<0?"down":delta>0?"up":"flat");
        current.appendChild(change);
      }
      addTrend(row.querySelector(".bar"),now,previous,label);
      expandable.appendChild(summary);
      const body=document.createElement("div");
      body.innerHTML=groupDetails(kind,normalized,now,previous,liveDetail);
      expandable.appendChild(body);
      expandable.addEventListener("toggle",()=>rememberOpen(rid,expandable.open));
      content.appendChild(expandable);
    }
    outer.appendChild(content);
    outer.addEventListener("toggle",()=>rememberOpen(colKey,outer.open));
    column.replaceChildren(outer);
  }
}
let liveDetail=null;
function rememberOpen(key,isOpen) {
  openState.set(key,isOpen);
}
function controls() {
  const el=panel();
  const current=selectedPoint();
  const compare=current;
  const first=timeline[0];
  const pos=Math.max(0,timeline.findIndex(p=>p.id===Number(chosenId)));
  const max=Math.max(0,timeline.length-1);
  el.innerHTML=`<div class="progress-time-header"><div><h3>Acceptance history & time travel</h3><p>Historical gains are included in the current totals above. Select a checkpoint to compare verified acceptance.</p><p>Compare verified captures—not commit counts. Hover any bar for then/now values; click cards and categories to expand.</p></div>
     <div class="progress-time-actions"><button id="progressCaptureNow" type="button">Capture now</button><button id="progressExpandAll" type="button">Expand all</button><button id="progressCollapseAll" type="button">Collapse all</button></div></div>
     <div class="progress-time-controls"><label>Historical checkpoint<select id="progressHistorySelect">
     ${timeline.map((p,i)=>`<option value="${p.id}" ${p.id===Number(chosenId)?"selected":""}>${escape(stamp(p.at))} · ${p.original.pass}/${p.original.total} · ${escape(p.reason)}</option>`).join("")}
     </select></label>
     <label>Move through captured time<input id="progressHistoryRange" type="range" min="0" max="${max}" value="${pos}" ${timeline.length<2?"disabled":""}/></label></div>
     <div class="progress-time-status" id="progressHistoryStatus">${timeline.length} capture(s) since ${escape(stamp(first?.at))}. ${current?"Comparing with "+escape(stamp(current.at)):"No history yet"}.</div>
     <div class="progress-history-delta" id="progressHistoryDelta"></div>
     <div class="progress-history-tooltip" id="progressHoverLegend" aria-live="polite">Hover a current bar to see its historical value. Expand any card or row for item changes.</div>`;
  $("progressHistorySelect")?.addEventListener("change",evt=>choose(Number(evt.target.value)));
  $("progressHistoryRange")?.addEventListener("input",evt=>choose(timeline[Number(evt.target.value)].id));
  $("progressCaptureNow")?.addEventListener("click",async()=>{
    try {
      const opts=await request("/api/launcher/options");
      await request("/api/progress/history/capture",{method:"POST",headers:{"X-MJobs-Launch-Token":opts.token}});
      await refresh(true);
    }catch(e){$("progressHistoryStatus").textContent="Capture failed: "+e.message}
  });
  $("progressExpandAll")?.addEventListener("click",()=>{
    document.querySelectorAll("#releaseContent details.progress-expandable").forEach(x=>x.open=true);
  });
  $("progressCollapseAll")?.addEventListener("click",()=>{
    document.querySelectorAll("#releaseContent details.progress-expandable").forEach(x=>x.open=false);
  });
  const diffs=compare && live ? [
    ["Original requirements since selected checkpoint",live.original.pass,compare.original.pass],
    ["Additional references since selected checkpoint",live.reference.pass,compare.reference.pass],
  ]:[];
  const delta=$("progressHistoryDelta");
  delta.innerHTML=diffs.map(([name,now,old])=>`<span>${escape(name)}: <strong class="${now-old<0?"down":"up"}">${sign(now-old)} accepted</strong> (${old} → ${now})</span>`).join("");
}
async function choose(id) {
  chosenId=id;
  const seq=++generation;
  try{
    historical=(await request("/api/progress/history/"+id));
    if(seq!==generation)return;
    decorateSections();
  }catch(e){
    const status=$("progressHistoryStatus");
    if(status)status.textContent="Historical checkpoint unavailable: "+e.message;
  }
}
async function refreshCampaigns() {
  if(campaignBusy)return;
  campaignBusy=true;
  const seq=++campaignGeneration;
  try {
    const result=await request("/api/progress/campaigns",{signal:AbortSignal.timeout(6000)});
    if(seq!==campaignGeneration)return;
    if(Array.isArray(result?.requirements)) requirementCatalog=result.requirements;
    const valid=result?.schema_version===1 && result.available===true && Array.isArray(result.campaigns) &&
      result.campaigns.every(item=>item && typeof item.requirement_id==="string" && item.scoreboard && Array.isArray(item.phases));
    campaignData=valid ? result : {available:false,error:result?.error||"Campaign response is missing or malformed."};
  } catch(error) {
    if(seq!==campaignGeneration)return;
    campaignData={available:false,error:error.message||"Campaign endpoint unavailable."};
  } finally {
    campaignBusy=false;
    if(live)decorateSections();
  }
}
async function refresh(force=false) {
  if(busy)return;
  busy=true;
  try {
    const result=await request("/api/progress/history?limit=250");
    timeline=result.points||[];
    const id=chosenId && timeline.some(p=>p.id===chosenId)?chosenId:timeline[0]?.id;
    if(!id){controls();return;}
    chosenId=id;
    historical=await request("/api/progress/history/"+id);
    const latest=timeline[timeline.length-1];
    if(live && (!liveDetail || knownLatest!==latest?.id)) {
      liveDetail=await request("/api/progress/history/"+latest.id);
    }
    knownLatest=latest?.id;
    decorateSections();
  }catch(e){
    panel().textContent="Historical acceptance is unavailable: "+e.message;
  } finally{busy=false;}
}
document.addEventListener("toggle",event=>{
  const target=event.target;
  if(target instanceof HTMLDetailsElement && target.dataset.key?.startsWith("campaign:")) rememberOpen(target.dataset.key,target.open);
},true);
function onProgress(data) {
  if(!data?.available)return;
  live=data;
  rawMarkup=$("releaseContent")?.innerHTML || "";
  void refreshCampaigns();
  const id=data.history_capture?.snapshot_id;
  if(!timeline.length || (id && !timeline.some(p=>p.id===id))) {
    void refresh();
  } else {
    decorateSections();
  }
}
window.MJobsProgressHistory={onProgress,refresh};
})();
