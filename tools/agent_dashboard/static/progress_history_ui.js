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
const pct = metric => metric?.total ? (100 * metric.pass/metric.total).toFixed(1) : "0.0";
const sign = n => n > 0 ? "+"+n : String(n);
function panel() {
  let el = $("progressTimeTravel");
  if (el) return el;
  el = document.createElement("section");
  el.id="progressTimeTravel";
  el.className="progress-time-machine";
  const release = $("releaseContent");
  release.insertBefore(el, release.firstChild);
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
  return label+" | selected "+stamp(selectedPoint()?.at)+": "+prev.pass+"/"+prev.total+
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
  matching.sort((a,b)=> a.status.localeCompare(b.status)||a.id.localeCompare(b.id));
  const prior=historical?.data?.requirements||{};
  const entries=matching.map(item=>{
    const from=prior[item.id]?.status;
    const change=from && from!==item.status ? ` <span class="progress-transition">${escape(from)} → ${escape(item.status)}</span>` : "";
    return `<div class="progress-item"><code>${escape(item.id)}</code><span>${escape(item.status)}${change}</span></div>`;
  }).join("");
  const diff=old ? `<strong>Δ accepted ${sign(now.pass-old.pass)}</strong> since ${escape(stamp(selectedPoint()?.at))}` : "No comparison snapshot selected";
  return `<div class="progress-drilldown"><div>${diff}. ${now.partial} PARTIAL · ${now.not_run} NOT_RUN · ${now.total} total.</div>
    <div class="progress-item-list">${entries||"No items in this group."}</div></div>`;
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
    details.open=openState.has(id) ? openState.get(id) : true;
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
  el.innerHTML=`<div class="progress-time-header"><div><h3>Acceptance history & time travel</h3><p>Compare verified captures—not commit counts. Hover any bar for then/now values; click cards and categories to expand.</p></div>
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
    ["Original requirements",live.original.pass,compare.original.pass],
    ["Additional references",live.reference.pass,compare.reference.pass],
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
function onProgress(data) {
  if(!data?.available)return;
  live=data;
  rawMarkup=$("releaseContent")?.innerHTML || "";
  const id=data.history_capture?.snapshot_id;
  if(!timeline.length || (id && !timeline.some(p=>p.id===id))) {
    void refresh();
  } else {
    decorateSections();
  }
}
window.MJobsProgressHistory={onProgress,refresh};
})();