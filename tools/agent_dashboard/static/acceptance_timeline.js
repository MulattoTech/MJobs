/* Evidence-based Mission Control acceptance timeline.
 * History is snapshots of canonical matrices or explicitly attributed screenshots;
 * Git commits, CI checks and time gaps never imply feature acceptance.
 */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? "").replace(/[&<>"']/g, char => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }[char]));
  const fmt = value => {
    const date = new Date(value);
    return value && Number.isFinite(date.getTime())
      ? date.toLocaleString([], {month:"short",day:"numeric",year:"numeric",hour:"numeric",minute:"2-digit"})
      : "Unknown time";
  };
  const statusOrder = {NOT_RUN:0,PARTIAL:1,PASS:2};
  const state = {
    current:null, captures:[], baselineId:null, baselineDetail:null, comparison:null,
    lastLoaded:0, lastCurrentId:null, loading:false, loaded:false, metric:"original",
    pendingBaselineKey:"", remembered:new Map(), openReferenceIds:new Set()
  };
  const valueOf = (snapshot, metric) => {
    if (!snapshot) return null;
    if (metric === "original" || metric === "reference") return snapshot[metric] || null;
    const [kind,name] = metric.split(":",2);
    const list = kind === "area" ? snapshot.areas : kind === "priority" ? snapshot.priorities : null;
    return (list || []).find(x => x.name === name) || null;
  };
  const metricLabel = metric => {
    if (metric === "original") return "Original requirements";
    if (metric === "reference") return "Additional reference gaps";
    return metric.slice(metric.indexOf(":")+1).replaceAll("_"," ");
  };
  const deltaSpan = n => n === null || !Number.isFinite(n)
    ? '<span class="acceptance-delta">Not comparable</span>'
    : '<span class="acceptance-delta '+(n>0?"positive":n<0?"negative":"")+'">'+
      (n>0?"+":"")+n+' accepted</span>';
  const printCount = record => record ? record.pass+"/"+record.total+
    " ("+(record.accepted_percent ?? (record.total ? (100*record.pass/record.total).toFixed(1):0))+"%)":"Not recorded";
  const sourceLabel = item => item?.provenance === "user_screenshot"
    ? "User screenshot (aggregate only)" : item?.capture_kind === "manual"
    ? "Manually saved canonical checkpoint" : "Canonical matrix capture";
  const compareText = (now, old) => {
    if (!now || !old) return "No comparable historical figures.";
    const count = now.pass-old.pass;
    const denom = now.total-old.total;
    const pcts = Number(now.accepted_percent??0)-Number(old.accepted_percent??0);
    const note = denom ? " · total changed by "+(denom>0?"+":"")+denom : "";
    return "Earlier "+printCount(old)+" → now "+printCount(now)+" · "+
      (count>0?"+":"")+count+" accepted ("+(pcts>0?"+":"")+pcts.toFixed(1)+" pp)"+note;
  };
  const hideTooltip = () => { const tip=$("acceptanceHoverTooltip");if(tip)tip.hidden=true; };
  const moveTooltip = event => {
    const tip=$("acceptanceHoverTooltip");
    if (!tip || tip.hidden || !event || !Number.isFinite(event.clientX)) return;
    const width=Math.min(tip.offsetWidth||320,330);
    tip.style.left=Math.min(window.innerWidth-width-12,Math.max(12,event.clientX+15))+"px";
    tip.style.top=Math.min(window.innerHeight-(tip.offsetHeight||100)-10,Math.max(10,event.clientY+15))+"px";
  };
  const showTooltip = (html,event) => {
    const tip=$("acceptanceHoverTooltip");
    if (!tip) return;
    tip.innerHTML=html;tip.hidden=false;moveTooltip(event);
  };
  function baseline() { return state.captures.find(x=>x.id===state.baselineId)||null; }
  function updateMetrics() {
    const select=$("acceptanceMetric");
    if (!select || !state.current) return;
    const options=[
      ["original","Original requirements"],["reference","Additional reference gaps"],
      ...[...(state.current.areas||[])].map(x=>["area:"+x.name,"Area · "+x.name.replaceAll("_"," ")]),
      ...[...(state.current.priorities||[])].map(x=>["priority:"+x.name,"Priority · "+x.name])
    ];
    const prior=select.value;
    const signature=options.map(x=>x[0]).join("|");
    if (select.dataset.signature===signature) return;
    select.replaceChildren();
    for (const [key,label] of options) {
      const item=document.createElement("option");item.value=key;item.textContent=label;
      select.appendChild(item);
    }
    select.dataset.signature=signature;
    select.value=options.some(x=>x[0]===prior)?prior:"original";
    state.metric=select.value;
  }
  function updateBaselinePicker() {
    const sel=$("acceptanceBaseline");
    if (!sel) return;
    const old=state.baselineId;
    sel.replaceChildren();
    if (!state.captures.length) {
      const opt=document.createElement("option");opt.value="";opt.textContent="No captures yet";sel.appendChild(opt);
      return;
    }
    for (const item of state.captures) {
      const opt=document.createElement("option");opt.value=String(item.id);
      opt.textContent=fmt(item.observed_at)+" · "+sourceLabel(item)+" · "+item.original.pass+"/"+item.original.total;
      sel.appendChild(opt);
    }
    state.baselineId=state.captures.some(x=>x.id===old)?old:state.captures[0].id;
    sel.value=String(state.baselineId);
    const slider=$("acceptanceRange");
    slider.min="0";slider.max=String(Math.max(0,state.captures.length-1));
    slider.value=String(Math.max(0,state.captures.findIndex(x=>x.id===state.baselineId)));
    slider.disabled=state.captures.length<2;
  }
  function trend() {
    const svg=$("acceptanceSparkline");
    if (!svg) return;
    const rows=state.captures;
    const metric=$("acceptanceMetric")?.value||"original";
    state.metric=metric;
    const ns="http://www.w3.org/2000/svg";
    svg.replaceChildren();
    const add=(tag,attrs) => {
      const el=document.createElementNS(ns,tag);
      Object.entries(attrs).forEach(([key,val])=>el.setAttribute(key,String(val)));
      svg.appendChild(el);return el;
    };
    const x=i=>rows.length<2?440:25+(830*i)/(rows.length-1);
    const y=rec=>122-101*Math.max(0,Math.min(100,Number(rec?.accepted_percent)||0))/100;
    for (const pct of [0,50,100]) {
      const line=add("line",{x1:25,x2:855,y1:122-101*pct/100,y2:122-101*pct/100,
        stroke:"#29455a","stroke-dasharray":"3 7"});
      line.setAttribute("aria-hidden","true");
    }
    if (!rows.length) { svg.setAttribute("aria-label","No historical snapshots yet");return; }
    const points=rows.map((row,i)=>({row,i,rec:valueOf(row,metric)})).filter(x=>x.rec);
    if (points.length>1) add("polyline",{
      points:points.map(p=>x(p.i)+","+y(p.rec)).join(" "),
      fill:"none",stroke:"#59d5a3","stroke-width":"2.8",
      "stroke-linejoin":"round","stroke-linecap":"round"
    });
    for (const point of points) {
      const selected=point.row.id===state.baselineId;
      const screenshot=point.row.provenance==="user_screenshot";
      const dot=add("circle",{
        cx:x(point.i),cy:y(point.rec),r:selected?7:4.8,
        fill:selected?"#e9fff5":screenshot?"#efbb6a":"#4ed6a2",
        tabindex:0,role:"button",
        "aria-label":fmt(point.row.observed_at)+"; "+sourceLabel(point.row)+"; "+
          metricLabel(metric)+" "+printCount(point.rec)
      });
      const item=point.row;
      const message='<strong>'+escape(metricLabel(metric))+'</strong><div>'+
        escape(fmt(item.observed_at))+'</div><div>'+escape(sourceLabel(item))+
        '</div><div>'+escape(printCount(point.rec))+'</div>'+
        '<div>Click to compare this capture.</div>';
      dot.addEventListener("pointerenter",e=>showTooltip(message,e));
      dot.addEventListener("pointermove",moveTooltip);
      dot.addEventListener("pointerleave",hideTooltip);
      dot.addEventListener("focus",()=>showTooltip(message,null));
      dot.addEventListener("blur",hideTooltip);
      dot.addEventListener("click",()=>chooseBaseline(item.id));
      dot.addEventListener("keydown",e=>{
        if (e.key==="Enter"||e.key===" ") { e.preventDefault();chooseBaseline(item.id); }
      });
    }
    svg.setAttribute("aria-label",metricLabel(metric)+": "+
      rows.length+" historical captures. Hover for values; click a point to compare.");
  }
  function renderReferenceItems(remembered) {
    const list=$("acceptanceReferenceList");
    if (!list || !state.current) return;
    const current=state.current.reference_statuses || {};
    const prior=state.baselineDetail?.reference_statuses || {};
    const labels=state.current.reference_titles || {};
    const items=Object.entries(current).sort((a,b)=>
      (statusOrder[a[1]]??9)-(statusOrder[b[1]]??9)||a[0].localeCompare(b[0]));
    if (!items.length) { list.textContent="Per-reference statuses are not currently available.";return; }
    list.innerHTML=items.map(([id,status])=>{
      const key="reference:"+id;
      const old=Object.prototype.hasOwnProperty.call(prior,id)?prior[id]:"Not available from this capture";
      const title=labels[id]?" · "+escape(labels[id]):"";
      return '<details data-disclosure-key="'+escape(key)+'" '+(remembered?.get(key)?"open":"")+'>'+
        '<summary><span>'+escape(id)+title+'</span><strong class="ref-status '+escape(status)+'">'+
        escape(status)+'</strong></summary><div>Current: '+escape(status)+
        ' · Historical baseline: '+escape(old)+'</div></details>';
    }).join("");
  }
  function renderDeltas() {
    const old=baseline();
    const now=state.current;
    if (!now) return;
    for (const node of document.querySelectorAll("#releaseContent [data-history-kind]")) {
      const kind=node.dataset.historyKind;
      const metric=kind==="area"||kind==="priority" ? kind+":"+node.dataset.historyName : kind;
      const earlier=valueOf(old,metric);
      const current=valueOf(now,metric);
      const info=node.querySelector(".acceptance-row-delta,.history-insight");
      if (info) info.textContent=old?compareText(current,earlier):
        "No earlier checkpoint selected. Capture history accumulates as acceptance changes.";
      const bar=node.querySelector(".bar,.release-meter");
      if (bar) {
        bar.title=(old?fmt(old.observed_at)+" — "+sourceLabel(old)+": "+printCount(earlier)+" | ":"")+
          "Current: "+printCount(current);
      }
    }
  }
  function renderComparison() {
    const base=baseline(), now=state.current;
    if (!now) return;
    const out=$("acceptanceComparison");
    if (!base) {
      out.textContent="No historical checkpoint yet. Current acceptance will be captured automatically when the canonical matrices are available.";
      return;
    }
    const d=now.original.pass-base.original.pass;
    const re=now.reference.pass-base.reference.pass;
    out.innerHTML='<span class="acceptance-capture-badge">Baseline '+escape(fmt(base.observed_at))+
      ' · '+escape(sourceLabel(base))+'</span>'+
      '<span class="acceptance-capture-badge">Current '+escape(fmt(now.observed_at))+'</span>'+
      'Original '+deltaSpan(d)+' · References '+deltaSpan(re)+
      (now.original.total!==base.original.total?
        '<span class="acceptance-delta">Original total changed '+escape(base.original.total)+" → "+
        escape(now.original.total)+'</span>':"")+
      (base.provenance==="user_screenshot"?
        '<span class="acceptance-delta">Screenshot: aggregate data only</span>':"");
    $("acceptanceTimelineLabel").innerHTML='<span>Earlier: '+escape(printCount(valueOf(base,state.metric)))+
      ' · '+escape(fmt(base.observed_at))+'</span><span>Now: '+
      escape(printCount(valueOf(now,state.metric)))+
      ' · '+escape(metricLabel(state.metric))+'</span>';
    renderDeltas();
  }
  function renderTransitions() {
    const list=$("acceptanceTransitionsList"), counter=$("acceptanceTransitionsCount");
    const result=state.comparison;
    if (!list || !counter) return;
    if (!result || !result.transitions_available) {
      counter.textContent="(per-ID comparison unavailable)";
      list.textContent="A screenshot provides aggregate bars only. Once two canonical checkpoints exist, changes to individual MJ-REQ and MJ-REF statuses will appear here.";
      return;
    }
    const changes=result.status_transitions||[];
    counter.textContent="("+changes.length+" changed statuses)";
    list.innerHTML=changes.length?changes.slice(0,160).map(c=>
      '<div class="acceptance-transition-line">'+escape(c.id)+' · '+
      escape(c.before)+' → <strong>'+escape(c.after)+'</strong></div>'
    ).join("")+(changes.length>160?'<p>First 160 changes shown.</p>':""):
    '<div>No requirement-level status changes between these captures.</div>';
  }
  async function loadSelectedDetail() {
    const id=state.baselineId;
    if (!id) return;
    const key=id+":"+(state.current?.acceptance_capture_id||"unknown");
    if (state.pendingBaselineKey===key) return;
    state.pendingBaselineKey=key;
    try {
      const [detail,diff] = await Promise.all([
        fetch("/api/progress/timeline/"+id,{cache:"no-store"}).then(r=>r.ok?r.json():null),
        fetch("/api/progress/compare?baseline_id="+id,{cache:"no-store"}).then(r=>r.ok?r.json():null)
      ]);
      if (state.pendingBaselineKey!==key) return;
      state.baselineDetail=detail;state.comparison=diff;
      const remembered=state.remembered;
      renderReferenceItems(remembered);
      renderTransitions();
    }catch(error){
      if (state.pendingBaselineKey===key) {
        state.baselineDetail=null;state.comparison=null;
        $("acceptanceTransitionsList").textContent="Per-requirement comparison temporarily unavailable.";
      }
    }
  }
  function chooseBaseline(id) {
    if (!state.captures.some(x=>x.id===id)) return;
    state.baselineId=id;
    $("acceptanceBaseline").value=String(id);
    $("acceptanceRange").value=String(Math.max(0,state.captures.findIndex(x=>x.id===id)));
    state.pendingBaselineKey="";
    state.baselineDetail=null;state.comparison=null;
    trend();renderComparison();renderTransitions();renderReferenceItems(state.remembered);
    void loadSelectedDetail();
  }
  function attach() {
    $("acceptanceMetric").addEventListener("change",()=>{state.metric=$("acceptanceMetric").value;trend();renderComparison();});
    $("acceptanceBaseline").addEventListener("change",()=>chooseBaseline(Number($("acceptanceBaseline").value)));
    $("acceptanceRange").addEventListener("input",()=> {
      const item=state.captures[Number($("acceptanceRange").value)];
      if (item) chooseBaseline(item.id);
    });
    $("acceptanceCaptureNow").addEventListener("click",async()=> {
      const button=$("acceptanceCaptureNow");
      button.disabled=true;
      button.textContent="Saving checkpoint…";
      try {
        const optsResponse=await fetch("/api/launcher/options",{cache:"no-store"});
        if(!optsResponse.ok)throw Error("Cannot verify local authorization");
        const opts=await optsResponse.json();
        if(!opts.token)throw Error("Local authorization unavailable");
        const reply=await fetch("/api/progress/checkpoint",{method:"POST",
          headers:{"X-MJobs-Launch-Token":opts.token},cache:"no-store"});
        if(!reply.ok)throw Error("Checkpoint capture failed ("+reply.status+")");
        const data=await reply.json();
        state.lastLoaded=0;
        await refreshCaptures();
        $("acceptanceTimelineLabel").textContent="Saved canonical checkpoint #"+data.id+" at "+fmt(data.observed_at)+". Compare it against another capture to measure the run.";
      }catch(error){
        $("acceptanceTimelineLabel").textContent="Checkpoint not saved: "+error.message;
      }finally{
        button.disabled=false;button.textContent="Save checkpoint now";
      }
    });
    $("acceptanceExpandAll").addEventListener("click",()=> {
      for (const node of document.querySelectorAll("#releaseTracker details")) node.open=true;
    });
    $("acceptanceCollapseAll").addEventListener("click",()=> {
      for (const node of document.querySelectorAll("#releaseTracker details")) node.open=false;
    });
    const parent=$("releaseContent");
    const itemOf=element=>element instanceof Element?element.closest(".release-meter,.area-progress .bar"):null;
    const tooltipFor = (target,event) => {
      const node=target?.closest("[data-history-kind]");
      if (!node || !state.current) return;
      const metric=node.dataset.historyKind==="area"||node.dataset.historyKind==="priority"
        ? node.dataset.historyKind+":"+node.dataset.historyName : node.dataset.historyKind;
      const old=baseline(),current=valueOf(state.current,metric),prior=valueOf(old,metric);
      showTooltip('<strong>'+escape(metricLabel(metric))+'</strong><div>Current: '+
        escape(printCount(current))+'</div><div>'+(old?
        "Earlier ("+escape(fmt(old.observed_at))+"): "+escape(printCount(prior))+
        "</div><div>"+escape(compareText(current,prior))+"</div><div>"+escape(sourceLabel(old)):
        "No prior history yet")+"</div>",event);
    };
    parent.addEventListener("mouseover",event=>{const target=itemOf(event.target);if(target)tooltipFor(target,event);});
    parent.addEventListener("mousemove",event=>{if(!itemOf(event.target))return;moveTooltip(event);});
    parent.addEventListener("mouseout",event=>{if(itemOf(event.target))hideTooltip();});
    parent.addEventListener("focusin",event=>{const target=itemOf(event.target);if(target)tooltipFor(target,null);});
    parent.addEventListener("focusout",hideTooltip);
    document.addEventListener("visibilitychange",()=>{if(document.hidden)hideTooltip();});
  }
  async function refreshCaptures() {
    if (state.loading) return;
    state.loading=true;
    try {
      const response=await fetch("/api/progress/timeline?limit=250",{cache:"no-store"});
      if (!response.ok) throw Error("HTTP "+response.status);
      const result=await response.json();
      if (!Array.isArray(result.captures)) throw Error("Invalid timeline data");
      const previous=state.captures.map(x=>x.id).join("|");
      state.captures=result.captures;
      state.lastLoaded=Date.now();
      if (previous!==state.captures.map(x=>x.id).join("|")) {
        updateBaselinePicker();
        trend();state.pendingBaselineKey="";
      }
      renderComparison();
      void loadSelectedDetail();
    }catch(error) {
      $("acceptanceComparison").textContent="Acceptance history is unavailable: "+error.message+
        ". Current canonical acceptance above is still shown.";
    }finally{state.loading=false;}
  }
  attach();
  window.MJobsAcceptanceTimeline={
    onProgress(data,priorDisclosures) {
      state.current=data;
      if (priorDisclosures) state.remembered=priorDisclosures;
      updateMetrics();
      renderReferenceItems(state.remembered);
      renderDeltas();
      if (state.lastCurrentId!==data.acceptance_capture_id ||
          Date.now()-state.lastLoaded>16000 || !state.loaded){
        state.lastCurrentId=data.acceptance_capture_id;
        state.loaded=true;
        void refreshCaptures();
      } else renderComparison();
    }
  };
})();
