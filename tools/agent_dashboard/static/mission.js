/* MJobs Mission Control: same live canonical acceptance model as the mjobs-progress skill. */
(() => {
  "use strict";
  const $ = (s, root = document) => root.querySelector(s);
  const safe = v => String(v ?? "").replace(/[&<>"']/g, c => ({
    "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"
  }[c]));
  let token = "";
  let profiles = [];
  let slots = [];
  const cards = new Map();
  let inflight = false;

  async function fetchJson(url, init = {}) {
    const response = await fetch(url, {
      headers: {"Content-Type":"application/json", ...(init.headers || {})},
      ...init
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "HTTP " + response.status);
    return body;
  }

  function labelStatus(node, text, type = "") {
    const el = $(".status-line", node);
    el.textContent = text;
    el.className = "status-line " + type;
  }
  function profileList(lane) { return profiles.filter(x => x.lane === lane); }
  function cardHtml(lane) {
    return `<div class="launch-card" data-slot="${safe(lane.lane)}">
      <div class="launch-top">
        <div class="launch-name">${safe(lane.name)}</div>
        <span class="pill cli-state">Checking…</span>
      </div>
      <div class="launch-desc">${lane.lane === "codex" ? "Application automation • review-first ATS" :
        lane.lane === "antigravity" ? "AI matching • explainable scores" : "Delivery control • end-to-end tests"}</div>
      <label>Development profile
        <select class="launch-profile" aria-label="${safe(lane.name)} profile"></select>
      </label>
      <div class="launch-split">
        <label>CLI model ID <input class="launch-model" spellcheck="false" autocomplete="off" placeholder="Exact installed model ID"></label>
        <label>Reasoning <select class="launch-effort">
          <option value="low">Low</option><option value="medium">Medium</option>
          <option value="high">High</option><option value="xhigh">Extra high</option>
          <option value="max">Maximum</option>
        </select></label>
      </div>
      <label>Agent name (optional, Antigravity/Claude)
        <input class="launch-agent" spellcheck="false" placeholder="CLI agent name">
      </label>
      <label>Extra instructions (optional)
        <textarea class="launch-instructions" rows="3" maxlength="6000" placeholder="Anything else you want this agent to focus on…"></textarea>
      </label>
      <label class="check-row"><input type="checkbox" class="launch-autocontinue">
        Continue after a successful CLI run (off by default)
      </label>
      <div class="launch-actions">
        <button class="launch-preview">Read prompt</button>
        <button class="launch-start primary">Launch CLI</button>
        <button class="launch-stop danger">Stop</button>
      </div>
      <div class="small-note launch-worktree"></div>
      <div class="small-note launch-runtime"></div>
      <div class="status-line" role="status" aria-live="polite"></div>
      <details><summary>Selected prompt contents</summary>
        <pre class="launch-prompt-preview">Choose “Read prompt” to view the full instructions.</pre>
      </details>
    </div>`;
  }

  function mountCard(item) {
    const wrapper = document.createElement("div");
    wrapper.innerHTML = cardHtml(item);
    const card = wrapper.firstElementChild;
    cards.set(item.lane, card);
    $("#launchGrid").appendChild(card);
    const picker = $(".launch-profile", card);
    for (const p of profileList(item.lane)) {
      const option = document.createElement("option");
      option.value = p.slug;
      option.textContent = p.label;
      picker.appendChild(option);
    }
    const cfg = item.config || {};
    if (cfg.launch_profile && profileList(item.lane).some(p => p.slug === cfg.launch_profile))
      picker.value = cfg.launch_profile;
    $(".launch-instructions",card).value = cfg.prompt_override || "";
    $(".launch-autocontinue",card).checked = Boolean(cfg.auto_continue);
    $(".launch-agent",card).value = cfg.agent_name || "";
    function selectionChanged() {
      const p = profiles.find(p => p.slug === picker.value);
      $(".launch-model",card).value = p?.default_model || "";
      $(".launch-effort",card).value = p?.effort || "high";
      const note = p && !p.default_model ? "Enter the exact model ID reported by your CLI before launching." : "";
      labelStatus(card, note, note ? "warn" : "");
      $(".launch-prompt-preview",card).textContent = "Click Read prompt for the selected profile.";
    }
    picker.addEventListener("change", selectionChanged);
    selectionChanged();
    if (cfg.launch_profile) {
      $(".launch-model",card).value = cfg.model || $(".launch-model",card).value;
      $(".launch-effort",card).value = cfg.reasoning || "high";
    }
    $(".launch-preview",card).onclick = async () => {
      try {
        const payload = await fetchJson("/api/launcher/profiles/" + encodeURIComponent(picker.value));
        $(".launch-prompt-preview",card).textContent = payload.content;
        $(".launch-prompt-preview",card).parentElement.open = true;
      } catch (error) { labelStatus(card,error.message,"error"); }
    };
    $(".launch-start",card).onclick = () => doAction(item.lane, card, "launch");
    $(".launch-stop",card).onclick = () => doAction(item.lane, card, "stop");
    updateCard(item);
  }

  function updateCard(item) {
    const card = cards.get(item.lane);
    if (!card) return;
    const state = item.runtime || {};
    const running = Boolean(state.pid) && ["RUNNING","ADOPTED_RUNNING"].includes(state.status);
    const badge = $(".cli-state",card);
    badge.textContent = (running ? "RUNNING" : (item.available ? "READY" : "NOT INSTALLED"));
    badge.className = "pill cli-state " + (running ? "running" : (item.available ? "completed" : "error"));
    $(".launch-worktree",card).textContent = "Worktree: " + item.worktree;
    $(".launch-runtime",card).textContent = state.pid
      ? "PID " + state.pid + " · " + (state.status || "RUNNING") + " · " + (state.effective_model || "")
      : "Stopped · branch " + item.branch;
    $(".launch-start",card).disabled = !item.available || running;
    $(".launch-stop",card).disabled = !running;
    if (!item.available && !$(".status-line",card).textContent)
      labelStatus(card, item.name + " executable unavailable; no software will be installed.", "warn");
  }

  async function doAction(lane, card, action) {
    if (action === "launch") {
      const model = $(".launch-model",card).value.trim();
      if (!model) { labelStatus(card,"Enter a real CLI model ID to start this profile.","error"); return; }
    }
    const btn = $(action === "launch" ? ".launch-start" : ".launch-stop",card);
    btn.disabled = true;
    labelStatus(card,action === "launch" ? "Preparing your isolated worktree…" : "Stopping supervised CLI…");
    try {
      const body = action === "launch" ? {
        profile: $(".launch-profile",card).value,
        model: $(".launch-model",card).value.trim(),
        effort: $(".launch-effort",card).value,
        agent: $(".launch-agent",card).value.trim(),
        instructions: $(".launch-instructions",card).value,
        auto_continue: $(".launch-autocontinue",card).checked
      } : {};
      const result = await fetchJson("/api/launcher/" + lane + "/" + action, {
        method: "POST",
        headers: {"X-MJobs-Launch-Token":token},
        body: JSON.stringify(body)
      });
      if (action === "launch") {
        const warnings = result.preflight?.warnings || [];
        labelStatus(card,"Started process " + result.pid + ". " +
          (warnings.length ? warnings.join(" ") : "Worktree prepared without changing main."),warnings.length?"warn":"success");
      } else {
        labelStatus(card, "Stop requested. This lane is now marked stopped.", "success");
      }
    } catch (error) {
      labelStatus(card,error.message,"error");
    } finally {
      await refreshOptions();
    }
  }

  async function refreshOptions() {
    const data = await fetchJson("/api/launcher/options");
    token = data.token;
    profiles = data.profiles;
    slots = data.lanes;
    for (const item of slots) cards.has(item.lane) ? updateCard(item) : mountCard(item);
    $("#legacyRunner").textContent = data.legacy_opencode_available
      ? "Legacy OpenCode/Ollama runner detected (available in advanced agent controls below)."
      : "OpenCode/Ollama not installed. Claude CLI is the working third session; nothing will be reinstalled.";
  }

  function fmtTime(value) {
    if (!value || value === "UNKNOWN") return "Unknown";
    const t = new Date(value);
    return Number.isNaN(t.getTime()) ? value : t.toLocaleString();
  }
  function meter(item) {
    return `<div class="release-meter"><span style="width:${Number(item.accepted_percent)||0}%"></span></div>`;
  }
  function areaRow(row) {
    const label = row.name.replaceAll("_", " ");
    return `<div class="area-progress">
      <span class="name" title="${safe(label)}">${safe(label)}</span>
      <span class="bar"><span class="fill" style="width:${Number(row.accepted_percent)||0}%"></span></span>
      <span class="count">${row.pass}/${row.total} · ${row.accepted_percent}%</span>
    </div>`;
  }
  function renderProgress(data) {
    const parent = $("#releaseContent");
    if (!data.available) {
      parent.textContent = "Cannot validate acceptance matrices: " + (data.error || "unknown error");
      return;
    }
    const a = data.original, b = data.reference;
    const sha = data.git?.local_main_sha?.slice(0,10) || "?";
    const remote = data.git?.github_main_sha?.slice(0,10) || "?";
    const synced = data.git?.published_matches_local_main;
    const syncText = synced === true ? "GitHub main synced" : synced === false
      ? "GitHub differs from local main" : "GitHub verification unavailable";
    const priorityHtml = (data.priorities||[]).map(areaRow).join("");
    const areaHtml = (data.areas||[]).map(areaRow).join("");
    parent.innerHTML = `
      <div class="release-summary">
        <div class="release-main-metric">
          <div class="kicker">Original requirements accepted</div>
          <div class="release-big">${a.pass} / ${a.total} <strong>${a.accepted_percent}% accepted</strong></div>
          ${meter(a)}
          <div class="release-explain">${a.partial} partial · ${a.not_run} not yet validated</div>
        </div>
        <div class="release-side-metric">
          <div class="kicker">Additional reference gaps</div>
          <div class="release-big">${b.pass} / ${b.total}</div>
          ${meter(b)}
          <div class="release-explain">${b.accepted_percent}% accepted · ${b.partial} partial · ${b.not_run} not run</div>
        </div>
        <div class="release-side-metric">
          <div class="kicker">Release readiness</div>
          <div class="release-big">${safe(data.release_readiness)}</div>
          <div class="release-explain">Latest milestone: ${safe(data.milestone)}</div>
        </div>
      </div>
      <div class="release-meta">Original evidence as of ${safe(fmtTime(data.original_as_of))};
         supplemental evidence as of ${safe(fmtTime(data.reference_as_of))}.
         Latest local check ${safe(fmtTime(data.observed_at))}.
      </div>
      <div class="release-meta">
        <span class="sync-pill ${synced ? "" : "warn"}">${syncText}</span>
        Local main <code>${safe(sha)}</code> · GitHub main <code>${safe(remote)}</code> ·
        Card checkpoint ${data.checkpoint_current ? "CURRENT" : "STALE / INTEGRATOR REFRESH REQUIRED"}.
        ${safe(data.git?.note || "")}
      </div>
      <div class="release-details">
        <div><h3>Feature areas · lowest acceptance first</h3>${areaHtml}</div>
        <div><h3>Acceptance by priority</h3>${priorityHtml}</div>
      </div>
      <div class="progress-disclaimer">${safe(data.caveat)} The page reads original canonical requirements and
      reference gaps on every refresh. It never marks an unfinished requirement PASS or overwrites acceptance records.</div>`;
    $("#releaseRefreshTime").textContent = "Verified " + new Date(data.observed_at).toLocaleTimeString();
  }

  async function refreshProgress() {
    try { renderProgress(await fetchJson("/api/progress")); }
    catch (error) { $("#releaseContent").textContent = "Release tracker unavailable: " + error.message; }
  }
  async function refreshEverything() {
    if (inflight) return;
    inflight = true;
    try { await Promise.all([refreshOptions(),refreshProgress()]); }
    catch (error) { $("#launchStatus").textContent = "Launcher temporarily unavailable: " + error.message; }
    finally { inflight = false; }
  }
  $("#missionRefresh").onclick = refreshEverything;
  refreshEverything();
  setInterval(() => { if (!document.hidden) refreshEverything(); }, 11000);
})();
