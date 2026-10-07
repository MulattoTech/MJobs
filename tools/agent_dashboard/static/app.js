const $ = (s, root=document) => root.querySelector(s);
const laneRoot = $("#lanes");
const template = $("#laneTemplate");
const cards = {};
const editing = new Set();

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }[c]));
}
async function api(path, options={}) {
  const response = await fetch(path, {
    headers: {"Content-Type":"application/json", ...(options.headers||{})},
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || response.statusText);
  return body;
}
function statusClass(status) {
  return String(status || "stopped").toLowerCase().replaceAll(" ", "_");
}
function fmtAge(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return Math.round(seconds)+"s ago";
  if (seconds < 3600) return Math.round(seconds/60)+"m ago";
  return (seconds/3600).toFixed(1)+"h ago";
}
function fmtReset(epoch) {
  if (!epoch) return "";
  const d = new Date(epoch*1000);
  const delta = Math.max(0, d-Date.now());
  const h = Math.floor(delta/3600000);
  const m = Math.floor((delta%3600000)/60000);
  return d.toLocaleString()+" ("+h+"h "+m+"m)";
}
function totalMemory(tree) {
  return (tree||[]).reduce((sum,p)=>sum+(Number(p.working_set_mb)||0),0);
}
function processProof(data) {
  const a=data.activity||{};
  const r=data.runtime||{};
  return JSON.stringify({
    supervisor_pid:r.pid||null,
    activity:a.state||null,
    last_output_seconds:a.last_activity_seconds,
    session_id:a.session_id||null,
    workspace:r.workspace||null,
    effective_model:r.effective_model||data.config?.model||null,
    process_tree:a.process_tree||[],
    visibility_note:a.visibility_note||null,
  },null,2);
}
function renderUsage(node, usage, lane) {
  const fill=$(".usage-fill",node);
  const title=$(".usage-title",node);
  const percent=$(".usage-percent",node);
  const meta=$(".usage-meta",node);
  if (lane==="codex" && usage?.available) {
    const remaining=Number(usage.remaining_percent);
    const used=Number(usage.used_percent);
    title.textContent="Codex quota";
    percent.textContent=remaining+"% left";
    fill.style.width=Math.max(0,Math.min(100,remaining))+"%";
    meta.textContent=[
      used+"% used",
      usage.window_minutes ? (usage.window_minutes/1440).toFixed(0)+"-day window" : "",
      usage.resets_at ? "resets "+fmtReset(usage.resets_at) : "",
      usage.credits_balance ? "credits "+Number(usage.credits_balance).toFixed(2) : "",
      usage.reset_credits_available!=null ? "full resets "+usage.reset_credits_available : "",
    ].filter(Boolean).join(" · ");
    return;
  }
  title.textContent = lane==="antigravity" ? "Antigravity quota" : "Usage";
  percent.textContent = "";
  fill.style.width="0%";
  meta.textContent = usage?.message || usage?.error || "Usage unavailable";
}
function createCard(lane) {
  const node=template.content.firstElementChild.cloneNode(true);
  node.dataset.lane=lane;
  $(".lane-name",node).textContent=lane;
  for (const sel of [".model",".reasoning",".prompt",".gpu-offload"]) {
    $(sel,node)?.addEventListener("focus",()=>editing.add(lane));
    $(sel,node)?.addEventListener("blur",()=>editing.delete(lane));
  }
  $(".auto-continue",node).addEventListener("change",()=>editing.add(lane));
  $(".gpu-offload",node).addEventListener("input",e=>{
    $(".gpu-offload-value",node).textContent=e.target.value+"%";
    editing.add(lane);
  });
  $(".save",node).onclick=async()=>{
    try {
      const body={
        model:$(".model",node).value.trim(),
        reasoning:$(".reasoning",node).value.trim(),
        auto_continue:$(".auto-continue",node).checked,
        prompt_override:$(".prompt",node).value,
      };
      if(lane==="ollama") body.gpu_offload_percent=Number($(".gpu-offload",node).value);
      await api("/api/lanes/"+lane+"/config",{method:"PATCH",body:JSON.stringify(body)});
      editing.delete(lane);
      $(".reason",node).textContent="Settings saved. Restart the lane to apply model/offload changes.";
      await refresh();
    } catch(e) { $(".reason",node).textContent=e.message; }
  };
  $(".start",node).onclick=()=>action(lane,"start",node);
  $(".restart",node).onclick=()=>action(lane,"restart",node);
  $(".stop",node).onclick=()=>action(lane,"stop",node);
  laneRoot.appendChild(node); cards[lane]=node; return node;
}
async function action(lane,verb,node) {
  try {
    $(".reason",node).textContent=verb+" requested…";
    await api("/api/lanes/"+lane+"/"+verb,{method:"POST",body:"{}"});
    await refresh();
  } catch(e) { $(".reason",node).textContent=e.message; }
}
function updateCard(lane,data,coord) {
  const node=cards[lane]||createCard(lane);
  const runtime=data.runtime||{}, cfg=data.config||{}, a=data.activity||{};
  const claim=coord?.claims?.[lane]||{};
  $(".lane-runner",node).textContent=(cfg.runner||lane)+" · "+(runtime.effective_model||cfg.model||"");
  const st=runtime.status||"STOPPED";
  $(".status",node).textContent=st;
  $(".status",node).className="status pill "+statusClass(st);
  $(".activity",node).textContent=a.state||"UNKNOWN";
  $(".activity",node).className="activity pill "+statusClass(a.state||"UNKNOWN");
  $(".pid",node).textContent=runtime.pid||"—";
  $(".issue",node).textContent=claim.issue?"#"+claim.issue:"—";
  $(".last-activity",node).textContent=fmtAge(a.last_activity_seconds);
  $(".session",node).textContent=a.session_id ? a.session_id.slice(0,8)+"…" : "—";
  $(".memory",node).textContent=(a.process_tree?.length ? totalMemory(a.process_tree).toFixed(0)+" MB" : "—");
  $(".lane-gpu",node).textContent=(a.observed_gpu_percent!=null ? a.observed_gpu_percent.toFixed(1)+"%" : "—");
  $(".workspace",node).textContent=runtime.workspace ? "Worktree: "+runtime.workspace : "";
  $(".reason",node).textContent=runtime.last_reason||"";
  $(".process-proof",node).textContent=processProof(data);
  renderUsage(node,data.usage,lane);

  const gpuControl=$(".gpu-control",node);
  gpuControl.classList.toggle("hidden",lane!=="ollama");
  if(!editing.has(lane)) {
    $(".model",node).value=cfg.model||"";
    $(".reasoning",node).value=cfg.reasoning||"";
    $(".auto-continue",node).checked=Boolean(cfg.auto_continue);
    $(".prompt",node).value=cfg.prompt_override||"";
    if(lane==="ollama") {
      const pct=cfg.gpu_offload_percent ?? 100;
      $(".gpu-offload",node).value=pct;
      $(".gpu-offload-value",node).textContent=pct+"%";
    }
  }
  $(".log",node).textContent=data.log_tail||"(no log yet)";
}
function renderRepo(state) {
  const git=state.git||{}, coord=state.coordination||{}, dirty=git.dirty||[];
  const lock=coord.lock?.lane||coord.lock?.owner?.lane||"free";
  $("#mainState").textContent=dirty.length?"main DIRTY":"main clean";
  $("#mainState").className="pill "+(dirty.length?"error":"running");
  const gpu=state.system?.gpu;
  $("#gpuState").textContent=gpu?.available ? "GPU "+Number(gpu.utilization_percent||0).toFixed(1)+"%" : "GPU n/a";
  $("#gpuState").className="pill "+(Number(gpu?.utilization_percent||0)>1?"running":"");
  $("#updatedAt").textContent="Updated "+new Date(state.time).toLocaleTimeString();
  $("#repoSummary").innerHTML=[
    ["Branch",git.branch],["HEAD",(git.head||"").slice(0,10)],
    ["origin/main",(git.origin_main||"").slice(0,10)],["Integration lock",lock],
    ["Dirty paths",dirty.length],["Worktrees",(git.worktrees||[]).length],
  ].map(([k,v])=>"<span><b>"+esc(k)+":</b> "+esc(v)+"</span>").join("");
  $("#coordination").textContent=JSON.stringify({
    lock:coord.lock||null, claims:coord.claims||{}, worktrees:git.worktrees||[],
    dirty, gpu:state.system?.gpu||null, ollama:state.system?.ollama||null
  },null,2);
  const alerts=[];
  for(const [lane,data] of Object.entries(state.lanes||{})) {
    const st=data.runtime?.status||"";
    if(["WAITING_APPROVAL","BLOCKED_QUOTA","BLOCKED_ENVIRONMENT","ERROR","CRASH_LOOP_GUARD"].includes(st))
      alerts.push(lane+": "+st+" — "+(data.runtime?.last_reason||"review log"));
    if(data.runtime?.pid && data.activity?.state==="QUIET" && (data.activity?.last_activity_seconds||0)>120)
      alerts.push(lane+": process exists but no log output for "+fmtAge(data.activity.last_activity_seconds));
  }
  if(dirty.length) alerts.push("Root main has uncommitted changes: "+dirty.join(", "));
  $("#alerts").innerHTML=alerts.map(x=>"<div>"+esc(x)+"</div>").join("");
}
async function refresh() {
  try {
    const state=await api("/api/state");
    renderRepo(state);
    for(const [lane,data] of Object.entries(state.lanes||{})) updateCard(lane,data,state.coordination);
  } catch(e) { $("#alerts").innerHTML="<div>Dashboard refresh failed: "+esc(e.message)+"</div>"; }
}
$("#refreshBtn").onclick=refresh;
$("#stopAllBtn").onclick=async()=>{
  if(!confirm("Stop every supervised agent lane?")) return;
  await api("/api/stop-all",{method:"POST",body:"{}"}); await refresh();
};
refresh();
setInterval(refresh,2500);
