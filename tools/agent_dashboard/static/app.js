const $ = (s, root=document) => root.querySelector(s);
const laneRoot = $("#lanes");
const template = $("#laneTemplate");
const cards = {};
const editing = new Set();
const terminals = {};

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

function fmtNum(value) {
  const n=Number(value);
  if(!Number.isFinite(n)) return "—";
  return n.toLocaleString();
}
function fmtTokens(value) {
  const n=Number(value);
  if(!Number.isFinite(n)) return "—";
  if(n>=1_000_000) return (n/1_000_000).toFixed(n>=10_000_000?1:2)+"M";
  if(n>=1_000) return (n/1_000).toFixed(n>=100_000?0:1)+"K";
  return n.toLocaleString();
}
function statTile(label,value,title="") {
  return '<div class="token-stat"'+(title?' title="'+esc(title)+'"':'')+'><span>'+esc(label)+'</span><strong>'+esc(value)+'</strong></div>';
}
function renderTokenGroup(target, stats, extras={}) {
  if(!target) return;
  if(!stats) { target.innerHTML=statTile("Status","No data yet"); return; }
  const tiles=[
    ["Context",fmtTokens(stats.current_context_tokens)],
    ["Input",fmtTokens(stats.input_tokens)],
    ["Output",fmtTokens(stats.output_tokens)],
    ["Reasoning",fmtTokens(stats.reasoning_tokens)],
    ["Cache read",fmtTokens(stats.cache_read_tokens)],
    ["Cache write",fmtTokens(stats.cache_write_tokens)],
    ["Processed",fmtTokens(stats.cumulative_total_tokens)],
    ["Turns",fmtNum(stats.assistant_turns)],
  ];
  if(stats.context_used_percent!=null) tiles.push(["Context used",Number(stats.context_used_percent).toFixed(1)+"%"]);
  if(stats.output_tokens_per_wall_second!=null) tiles.push(["Output tok/s",Number(stats.output_tokens_per_wall_second).toFixed(2)]);
  if(stats.cache_share_percent!=null) tiles.push(["Cache share",Number(stats.cache_share_percent).toFixed(1)+"%"]);
  if(stats.tool_calls!=null) tiles.push(["Tool calls",fmtNum(stats.tool_calls)]);
  if(stats.session_count!=null) tiles.push(["Sessions",fmtNum(stats.session_count)]);
  for(const [k,v] of Object.entries(extras)) tiles.push([k,v]);
  target.innerHTML=tiles.map(([label,value])=>statTile(label,value)).join("");
}

function renderStatsTiles(target, entries) {
  if(!target) return;
  target.innerHTML=entries.map(([label,value,title])=>statTile(label,value,title||"")).join("");
}
function secs(value) {
  const n=Number(value);
  if(!Number.isFinite(n)) return "—";
  if(n<60) return n.toFixed(1)+"s";
  if(n<3600) return (n/60).toFixed(1)+"m";
  return (n/3600).toFixed(2)+"h";
}
function renderCodexStats(node, usage) {
  const telemetry=usage?.telemetry||{};
  const current=telemetry.current_session||{};
  const turn=current.current_turn||{};
  const session=current.session||{};
  const worktree=telemetry.worktree||{};
  const all=telemetry.model_mjobs_all_time||{};
  const day=telemetry.model_mjobs_24h||{};
  renderStatsTiles($(".codex-turn-grid",node),[
    ["Context",fmtTokens(turn.cumulative_total_tokens)],
    ["Context used",turn.context_used_percent!=null?Number(turn.context_used_percent).toFixed(1)+"%":"—"],
    ["Context window",fmtTokens(turn.context_window)],
    ["New input",fmtTokens(turn.new_input_tokens)],
    ["Cached input",fmtTokens(turn.cache_read_tokens)],
    ["Cache write",fmtTokens(turn.cache_write_tokens)],
    ["Output",fmtTokens(turn.output_tokens)],
    ["Reasoning",fmtTokens(turn.reasoning_tokens)],
  ]);
  renderStatsTiles($(".codex-session-grid",node),[
    ["Processed",fmtTokens(session.cumulative_total_tokens)],
    ["Input",fmtTokens(session.input_tokens)],
    ["New input",fmtTokens(session.new_input_tokens)],
    ["Cached input",fmtTokens(session.cache_read_tokens)],
    ["Output",fmtTokens(session.output_tokens)],
    ["Reasoning",fmtTokens(session.reasoning_tokens)],
    ["Model turns",fmtNum(session.model_turns)],
    ["Tool calls",fmtNum(session.tool_calls)],
    ["Cache share",session.cache_share_percent!=null?Number(session.cache_share_percent).toFixed(1)+"%":"—"],
    ["Output tok/s",session.output_tokens_per_wall_second!=null?Number(session.output_tokens_per_wall_second).toFixed(2):"—"],
    ["Elapsed",secs(session.elapsed_seconds)],
    ["Avg output/turn",session.avg_output_tokens_per_turn!=null?fmtTokens(session.avg_output_tokens_per_turn):"—"],
  ]);
  renderStatsTiles($(".codex-worktree-grid",node),[
    ["Sessions",fmtNum(worktree.session_count)],
    ["Processed",fmtTokens(worktree.cumulative_total_tokens)],
    ["Input",fmtTokens(worktree.input_tokens)],
    ["Cached input",fmtTokens(worktree.cache_read_tokens)],
    ["Output",fmtTokens(worktree.output_tokens)],
    ["Reasoning",fmtTokens(worktree.reasoning_tokens)],
    ["Model turns",fmtNum(worktree.model_turns)],
    ["Tool calls",fmtNum(worktree.tool_calls)],
    ["Elapsed",secs(worktree.elapsed_seconds)],
    ["Cache share",worktree.cache_share_percent!=null?Number(worktree.cache_share_percent).toFixed(1)+"%":"—"],
  ]);
  renderStatsTiles($(".codex-model-grid",node),[
    ["MJobs sessions",fmtNum(all.session_count)],
    ["Processed",fmtTokens(all.cumulative_total_tokens)],
    ["Input",fmtTokens(all.input_tokens)],
    ["Cached input",fmtTokens(all.cache_read_tokens)],
    ["Output",fmtTokens(all.output_tokens)],
    ["Reasoning",fmtTokens(all.reasoning_tokens)],
    ["24h processed",fmtTokens(day.cumulative_total_tokens)],
    ["24h output",fmtTokens(day.output_tokens)],
    ["24h sessions",fmtNum(day.session_count)],
  ]);
  $(".codex-raw-stats",node).textContent=JSON.stringify({
    session_id:current.session_id,
    model:current.model,
    reasoning_effort:current.reasoning_effort,
    time_created:current.time_created,
    time_updated:current.time_updated,
    current_turn:turn,
    session_total:session,
    tool_breakdown:session.tool_breakdown,
    worktree_total:worktree,
    selected_model_mjobs_all_time:all,
    selected_model_mjobs_24h:day,
    quota:{
      used_percent:usage?.used_percent,
      remaining_percent:usage?.remaining_percent,
      resets_at:usage?.resets_at,
      credits_balance:usage?.credits_balance,
    }
  },null,2);
}
function renderAntigravityStats(node, usage) {
  const telemetry=usage?.telemetry||{};
  const current=telemetry.current_conversation||{};
  const worktree=telemetry.worktree||{};
  const all=telemetry.configured_model_history||{};
  const day=telemetry.configured_model_24h||{};
  renderStatsTiles($(".antigravity-current-grid",node),[
    ["Prompt context",fmtTokens(current.latest_context_tokens)],
    ["Latest input",fmtTokens(current.latest_input_tokens)],
    ["Latest cache",fmtTokens(current.latest_cache_read_tokens)],
    ["Latest output",fmtTokens(current.latest_output_tokens)],
    ["Input total",fmtTokens(current.input_tokens)],
    ["Cache read",fmtTokens(current.cache_read_tokens)],
    ["Output total",fmtTokens(current.output_tokens)],
    ["Processed",fmtTokens(current.cumulative_total_tokens)],
    ["Planner turns",fmtNum(current.planner_turns)],
    ["Tool calls",fmtNum(current.tool_calls)],
    ["Thinking steps",fmtNum(current.thinking_steps)],
    ["Thinking chars",fmtNum(current.thinking_characters)],
    ["Cache share",current.cache_share_percent!=null?Number(current.cache_share_percent).toFixed(1)+"%":"—"],
    ["Output tok/s",current.output_tokens_per_wall_second!=null?Number(current.output_tokens_per_wall_second).toFixed(2):"—"],
    ["Elapsed",secs(current.elapsed_seconds)],
  ]);
  renderStatsTiles($(".antigravity-worktree-grid",node),[
    ["Conversations",fmtNum(worktree.conversation_count)],
    ["Processed",fmtTokens(worktree.cumulative_total_tokens)],
    ["Input",fmtTokens(worktree.input_tokens)],
    ["Cache read",fmtTokens(worktree.cache_read_tokens)],
    ["Output",fmtTokens(worktree.output_tokens)],
    ["Planner turns",fmtNum(worktree.planner_turns)],
    ["Tool calls",fmtNum(worktree.tool_calls)],
    ["Thinking steps",fmtNum(worktree.thinking_steps)],
    ["Thinking chars",fmtNum(worktree.thinking_characters)],
    ["Elapsed",secs(worktree.elapsed_seconds)],
    ["Cache share",worktree.cache_share_percent!=null?Number(worktree.cache_share_percent).toFixed(1)+"%":"—"],
  ]);
  renderStatsTiles($(".antigravity-model-grid",node),[
    ["Conversations",fmtNum(all.conversation_count)],
    ["Processed",fmtTokens(all.cumulative_total_tokens)],
    ["Input",fmtTokens(all.input_tokens)],
    ["Cache read",fmtTokens(all.cache_read_tokens)],
    ["Output",fmtTokens(all.output_tokens)],
    ["Tool calls",fmtNum(all.tool_calls)],
    ["24h processed",fmtTokens(day.cumulative_total_tokens)],
    ["24h output",fmtTokens(day.output_tokens)],
    ["24h conversations",fmtNum(day.conversation_count)],
  ]);
  $(".antigravity-raw-stats",node).textContent=JSON.stringify({
    conversation_id:current.conversation_id,
    issue:current.issue,
    requirement_id:current.requirement_id,
    model_hint:current.model_hint,
    time_created:current.time_created,
    time_updated:current.time_updated,
    latest_context_tokens:current.latest_context_tokens,
    thinking_words:current.thinking_words,
    thinking_characters:current.thinking_characters,
    tool_breakdown:current.tool_breakdown,
    current_conversation:current,
    worktree_total:worktree,
    configured_model_history:all,
    configured_model_24h:day,
    attribution_note:telemetry.model_history_attribution,
    quota_note:usage?.message,
  },null,2);
}

function renderOllamaStats(node, usage) {
  const panel=$(".ollama-stats",node);
  panel.classList.remove("hidden");
  const current=usage?.current_session;
  const worktree=usage?.worktree;
  const all=usage?.model_all_time;
  const day=usage?.model_24h;
  const runtime=usage?.runtime||{};
  renderTokenGroup($(".current-token-grid",node),current,{
    "Elapsed":current?.elapsed_seconds!=null ? Math.round(current.elapsed_seconds)+"s" : "—",
    "Finish":current?.latest_finish||"—",
  });
  renderTokenGroup($(".worktree-token-grid",node),worktree,{
    "Sessions":fmtNum(usage?.session_count),
  });
  const modelCombined=all ? {...all} : null;
  renderTokenGroup($(".model-token-grid",node),modelCombined,{
    "24h input":fmtTokens(day?.input_tokens),
    "24h output":fmtTokens(day?.output_tokens),
    "24h cache":fmtTokens(day?.cache_read_tokens),
  });
  const raw={
    current_session_id:current?.session_id,
    title:current?.title,
    model_id:current?.model_id,
    provider_id:current?.provider_id,
    current_context_tokens:current?.current_context_tokens,
    context_window:current?.context_window||runtime.context_window,
    context_used_percent:current?.context_used_percent,
    tool_calls:current?.tool_calls,
    tool_breakdown:current?.tool_breakdown,
    reasoning_parts:current?.reasoning_parts,
    assistant_active_seconds:current?.assistant_active_seconds,
    generation_wall_seconds:current?.generation_wall_seconds,
    output_tokens_per_wall_second:current?.output_tokens_per_wall_second,
    runtime,
    worktree_total:worktree,
    selected_model_all_time:all,
    selected_model_24h:day,
  };
  $(".ollama-raw-stats",node).textContent=JSON.stringify(raw,null,2);
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

function terminalState(lane,node) {
  if (terminals[lane]) return terminals[lane];
  const state={
    lane,node,ws:null,paused:false,pending:"",currentRun:null,retryTimer:null,
  };
  terminals[lane]=state;
  $(".terminal-pause",node).onclick=()=>{
    state.paused=!state.paused;
    $(".terminal-pause",node).textContent=state.paused?"Resume":"Pause";
    if(!state.paused && state.pending){
      appendTerminal(state,state.pending);
      state.pending="";
    }
  };
  $(".terminal-clear",node).onclick=()=>{ $(".terminal",node).textContent=""; };
  $(".terminal-grow",node).onclick=()=>{
    node.classList.toggle("terminal-expanded");
    $(".terminal-grow",node).textContent=node.classList.contains("terminal-expanded")?"Collapse":"Expand";
  };
  return state;
}
function appendTerminal(state,text) {
  if(!text) return;
  if(state.paused){ state.pending+=text; return; }
  const terminal=$(".terminal",state.node);
  terminal.textContent+=text;
  if($(".terminal-follow",state.node).checked) terminal.scrollTop=terminal.scrollHeight;
}
function connectTerminal(lane,node) {
  const state=terminalState(lane,node);
  if(state.ws && [WebSocket.OPEN,WebSocket.CONNECTING].includes(state.ws.readyState)) return;
  const proto=location.protocol==="https:"?"wss":"ws";
  const ws=new WebSocket(proto+"://"+location.host+"/ws/lanes/"+lane+"/terminal");
  state.ws=ws;
  const badge=$(".terminal-connection",node);
  ws.onopen=()=>{
    badge.textContent="LIVE";
    badge.className="terminal-connection pill running";
  };
  ws.onmessage=(event)=>{
    let msg;
    try{ msg=JSON.parse(event.data); }catch{ appendTerminal(state,event.data); return; }
    if(msg.type==="run"){
      if(msg.run_id!==state.currentRun){
        state.currentRun=msg.run_id;
        $(".terminal-run",node).textContent=msg.run_id?"run "+msg.run_id:"no active run";
        appendTerminal(state,"\n\n===== "+lane.toUpperCase()+" RUN "+(msg.run_id||"none")+" =====\n");
      }
      return;
    }
    if(msg.type==="data"){
      const prefix=msg.source==="stdout"?"":"\n["+msg.source+"] ";
      appendTerminal(state,prefix+(msg.data||""));
    }
  };
  ws.onclose=()=>{
    badge.textContent="reconnecting";
    badge.className="terminal-connection pill quiet";
    state.ws=null;
    clearTimeout(state.retryTimer);
    state.retryTimer=setTimeout(()=>connectTerminal(lane,node),1200);
  };
  ws.onerror=()=>ws.close();
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
  laneRoot.appendChild(node);
  cards[lane]=node;
  connectTerminal(lane,node);
  return node;
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
  const external=globalThis.MJobsFleetObservation?.provider(lane);
  const st=external?.present?(external.working?"AO WORKING":"AO IDLE"):("Independent: "+(runtime.status||"NOT REPORTED"));
  $(".status",node).textContent=st;
  $(".status",node).className="status pill "+statusClass(st);
  $(".activity",node).textContent=external?.present?external.working+" working / "+external.total+" AO agents":a.state||"NOT REPORTED";
  $(".activity",node).className="activity pill "+statusClass(a.state||"UNKNOWN");
  $(".pid",node).textContent=runtime.pid||"—";
  $(".issue",node).textContent=claim.issue?"#"+claim.issue:"—";
  $(".last-activity",node).textContent=fmtAge(a.last_activity_seconds);
  $(".session",node).textContent=a.session_id ? a.session_id.slice(0,8)+"…" : "—";
  $(".memory",node).textContent=(a.process_tree?.length ? totalMemory(a.process_tree).toFixed(0)+" MB" : "—");
  $(".lane-gpu",node).textContent=(a.observed_gpu_percent!=null ? a.observed_gpu_percent.toFixed(1)+"%" : "—");
  $(".workspace",node).textContent=runtime.workspace ? "Worktree: "+runtime.workspace : "";
  $(".reason",node).textContent=external?.present?external.total+" AO runtimes managed through Fleet Control. Independent launcher: "+(runtime.status||"not reported")+(runtime.last_reason?". "+runtime.last_reason:""):runtime.last_reason||"No AO runtime is represented by this independent launcher record.";
  $(".process-proof",node).textContent=processProof(data);
  renderUsage(node,data.usage,lane);

  const gpuControl=$(".gpu-control",node);
  gpuControl.classList.toggle("hidden",lane!=="ollama");
  const codexStats=$(".codex-stats",node);
  codexStats.classList.toggle("hidden",lane!=="codex");
  if(lane==="codex") renderCodexStats(node,data.usage);
  const antigravityStats=$(".antigravity-stats",node);
  antigravityStats.classList.toggle("hidden",lane!=="antigravity");
  if(lane==="antigravity") renderAntigravityStats(node,data.usage);
  const ollamaStats=$(".ollama-stats",node);
  ollamaStats.classList.toggle("hidden",lane!=="ollama");
  if(lane==="ollama") renderOllamaStats(node,data.usage);
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
const advancedPanel = document.querySelector(".advanced-control");
if (advancedPanel) advancedPanel.addEventListener("toggle", () => {
  if (advancedPanel.open) refresh();
});
refresh();
setInterval(() => {
  if (!document.hidden && (!advancedPanel || advancedPanel.open)) refresh();
}, 6000);
