const $ = (s, root=document) => root.querySelector(s);
const laneRoot = $("#lanes");
const template = $("#laneTemplate");
let cards = {};
let editing = new Set();

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }[c]));
}

async function api(path, options={}) {
  const response = await fetch(path, {
    headers: {"Content-Type":"application/json", ...(options.headers || {})},
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || response.statusText);
  return body;
}

function statusClass(status) {
  return String(status || "stopped").toLowerCase().replaceAll(" ", "_");
}

function createCard(lane) {
  const node = template.content.firstElementChild.cloneNode(true);
  node.dataset.lane = lane;
  $(".lane-name", node).textContent = lane;
  for (const selector of [".model",".reasoning",".prompt"]) {
    $(selector,node).addEventListener("focus", () => editing.add(lane));
    $(selector,node).addEventListener("blur", () => editing.delete(lane));
  }
  $(".auto-continue",node).addEventListener("change", () => editing.add(lane));

  $(".save",node).onclick = async () => {
    try {
      await api("/api/lanes/"+lane+"/config", {
        method:"PATCH",
        body:JSON.stringify({
          model:$(".model",node).value.trim(),
          reasoning:$(".reasoning",node).value.trim(),
          auto_continue:$(".auto-continue",node).checked,
          prompt_override:$(".prompt",node).value,
        })
      });
      editing.delete(lane);
      $(".reason",node).textContent = "Settings saved.";
      await refresh();
    } catch (e) { $(".reason",node).textContent = e.message; }
  };
  $(".start",node).onclick = () => action(lane,"start",node);
  $(".restart",node).onclick = () => action(lane,"restart",node);
  $(".stop",node).onclick = () => action(lane,"stop",node);
  laneRoot.appendChild(node);
  cards[lane]=node;
  return node;
}

async function action(lane, verb, node) {
  try {
    $(".reason",node).textContent = verb+" requested…";
    await api("/api/lanes/"+lane+"/"+verb,{method:"POST",body:"{}"});
    await refresh();
  } catch(e) {
    $(".reason",node).textContent = e.message;
  }
}

function updateCard(lane, data, coordination) {
  const node = cards[lane] || createCard(lane);
  const runtime = data.runtime || {};
  const cfg = data.config || {};
  const claim = coordination?.claims?.[lane] || {};
  $(".lane-runner",node).textContent = (cfg.runner || lane) + " · " + (cfg.prompt_file || "");
  const status = runtime.status || "STOPPED";
  const pill = $(".status",node);
  pill.textContent = status;
  pill.className = "status pill "+statusClass(status);
  $(".pid",node).textContent = runtime.pid || "—";
  $(".restarts",node).textContent = runtime.restart_count ?? 0;
  $(".issue",node).textContent = claim.issue ? "#"+claim.issue : "—";
  $(".reason",node).textContent = runtime.last_reason || "";

  if (!editing.has(lane)) {
    $(".model",node).value = cfg.model || "";
    $(".reasoning",node).value = cfg.reasoning || "";
    $(".auto-continue",node).checked = Boolean(cfg.auto_continue);
    $(".prompt",node).value = cfg.prompt_override || "";
  }
  $(".log",node).textContent = data.log_tail || "(no log yet)";
}

function renderRepo(state) {
  const git = state.git || {};
  const coord = state.coordination || {};
  const dirty = git.dirty || [];
  const lock = coord.lock?.lane || coord.lock?.owner?.lane || "free";
  $("#mainState").textContent = dirty.length ? "main DIRTY" : "main clean";
  $("#mainState").className = "pill "+(dirty.length ? "error" : "running");
  $("#updatedAt").textContent = "Updated "+new Date(state.time).toLocaleTimeString();
  $("#repoSummary").innerHTML = [
    ["Branch",git.branch],["HEAD",(git.head||"").slice(0,10)],
    ["origin/main",(git.origin_main||"").slice(0,10)],["Integration lock",lock],
    ["Dirty paths",dirty.length],["Worktrees",(git.worktrees||[]).length],
  ].map(([k,v]) => "<span><b>"+esc(k)+":</b> "+esc(v)+"</span>").join("");
  $("#coordination").textContent = JSON.stringify({
    lock:coord.lock || null, claims:coord.claims || {}, worktrees:git.worktrees || [],
    dirty:dirty
  },null,2);

  const alerts=[];
  for (const [lane,data] of Object.entries(state.lanes||{})) {
    const status=data.runtime?.status || "";
    if (["WAITING_APPROVAL","BLOCKED_QUOTA","BLOCKED_ENVIRONMENT","ERROR","CRASH_LOOP_GUARD"].includes(status)) {
      alerts.push(lane+": "+status+" — "+(data.runtime?.last_reason || "review log"));
    }
  }
  if (dirty.length) alerts.push("Root main has uncommitted changes: "+dirty.join(", "));
  $("#alerts").innerHTML = alerts.map(x=>"<div>"+esc(x)+"</div>").join("");
}

async function refresh() {
  try {
    const state = await api("/api/state");
    renderRepo(state);
    for (const [lane,data] of Object.entries(state.lanes || {})) {
      updateCard(lane,data,state.coordination);
    }
  } catch(e) {
    $("#alerts").innerHTML = "<div>Dashboard refresh failed: "+esc(e.message)+"</div>";
  }
}

$("#refreshBtn").onclick = refresh;
$("#stopAllBtn").onclick = async () => {
  if (!confirm("Stop every supervised agent lane?")) return;
  await api("/api/stop-all",{method:"POST",body:"{}"});
  await refresh();
};

refresh();
setInterval(refresh, 1500);
