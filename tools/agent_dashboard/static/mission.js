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
  // Every launcher has independent preview/approval state. No agent starts merely
  // because options or prompts are previewed.
  const previewStates = new Map();
  let activePreviewCard = null;
  const dialog = $("#launchReviewDialog");

  async function fetchJson(url, init = {}) {
    const {headers = {}, ...other} = init;
    const response = await fetch(url, {
      ...other, headers: {"Content-Type":"application/json", ...headers}
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
        <label>Model from installed CLI <select class="launch-model" required aria-label="Select installed CLI model"></select></label>
        <label>Reasoning / effort <select class="launch-effort" aria-label="Supported reasoning effort"></select></label>
      </div>
      <div class="model-tools"><span class="small-note launch-model-source">Discovering CLI models…</span>
        <button type="button" class="launch-refresh-models">Refresh models</button></div>
      <div class="small-note launch-effort-help"></div>
      <div class="launch-capacity-settings">
        <label>CLI visibility
          <select class="launch-mode" aria-label="New coding CLI launch visibility">
            <option value="terminal">Visible Windows terminal (recommended)</option>
            <option value="headless">Headless background, dashboard logging</option>
          </select>
        </label>
        <div class="launch-codex-capacity" hidden>
          <div class="launch-split">
            <label>Maximum concurrent subagents
              <input class="launch-max-subagents" type="number" min="0" max="50" step="1" value="50"
                aria-label="Hard maximum concurrent Codex subagents">
            </label>
            <label>Intended active subagents
              <input class="launch-intended-subagents" type="number" min="0" max="50" step="1" value="8"
                aria-label="Suggested number of productive Codex subagents">
            </label>
          </div>
          <div class="small-note launch-capacity-note">Hard maximum enforced per CLI session. Intended target directs productive delegation, not a guaranteed minimum. Zero disables delegation.</div>
        </div>
      </div>
      <label>Agent / session name <select class="launch-agent-choice" aria-label="Agent session name"></select></label>
      <label class="launch-custom-wrap" hidden>Custom agent/session name
        <input class="launch-custom-agent" maxlength="80" spellcheck="false" placeholder="My Agent Name">
      </label>
      <div class="small-note">Names label the session and guide its prompt; they do not require an installed native CLI agent.</div>
      <label>Extra instructions (optional)
        <textarea class="launch-instructions" rows="3" maxlength="6000" placeholder="Anything else you want this agent to focus on…"></textarea>
      </label>
      <label class="check-row"><input type="checkbox" class="launch-autocontinue">
        Continue after a successful CLI run (off by default)
      </label>
      <div class="launch-actions">
        <button type="button" class="launch-preview">Read prompt</button>
        <button type="button" class="launch-start primary" disabled title="Read and approve the exact prompt first">Launch CLI</button>
        <button class="launch-stop danger">Stop</button>
        <button type="button" class="launch-view-terminal">View console / last output</button>
      </div>
      <div class="small-note launch-worktree"></div>
      <div class="small-note launch-runtime"></div>
      <div class="small-note launch-run-type"></div>
      <div class="status-line" role="status" aria-live="polite"></div>
      <details class="launch-preview-details">
        <summary>Exact effective prompt · synchronized with launch selections</summary>
        <div class="small-note launch-preview-note" role="status">Verifying selected model and prompt…</div>
        <pre class="launch-prompt-preview" tabindex="0">Preparing preview…</pre>
      </details>
    </div>`;
  }

  const EFFORT_LABELS = {
    low: "Low", medium: "Medium", high: "High", xhigh: "Extra high",
    max: "Maximum", ultra: "Ultra"
  };
  function currentSlot(lane) { return slots.find(item => item.lane === lane); }

  function renderEffortOptions(card, item, preferred = "") {
    const select = $(".launch-effort",card);
    const current = select.value;
    const modelId = $(".launch-model",card).value;
    const model = (item?.catalog?.models || []).find(m => m.id === modelId);
    const efforts = model?.efforts || [];
    select.replaceChildren();
    for (const effort of efforts) {
      const option = document.createElement("option");
      option.value = effort;
      option.textContent = EFFORT_LABELS[effort] || effort;
      select.appendChild(option);
    }
    const profile = profiles.find(p => p.slug === $(".launch-profile",card).value);
    select.value = [preferred, current, profile?.effort, model?.default_effort, efforts[0]]
      .find(value => efforts.includes(value)) || "";
    select.disabled = efforts.length <= 1;
    $(".launch-effort-help",card).textContent = model?.effort_embedded
      ? "Effort is built into this model ID; no conflicting CLI flags are needed."
      : model ? "Only supported reasoning levels are selectable." : "No model catalog available.";
  }

  function renderModelOptions(card, item, preferred = "", effort = "") {
    const catalog = item?.catalog || {};
    const models = catalog.models || [];
    const select = $(".launch-model",card);
    select.replaceChildren();
    for (const model of models) {
      const option = document.createElement("option");
      option.value = model.id;
      option.textContent = model.label + " · " + model.id;
      select.appendChild(option);
    }
    if (!models.length) {
      const option = document.createElement("option");
      option.value = ""; option.textContent = "No models discovered — refresh or check CLI";
      select.appendChild(option);
    }
    const profile = profiles.find(p => p.slug === $(".launch-profile",card).value);
    const chosen = [preferred, profile?.default_model, models[0]?.id]
      .find(candidate => models.some(model => model.id === candidate));
    select.value = chosen || "";
    select.disabled = models.length === 0;
    renderEffortOptions(card,item,effort || profile?.effort);
    const sources = {
      live_codex_catalog: "Codex CLI catalog",
      live_antigravity_catalog: "Antigravity CLI catalog",
      cli_documented_aliases: "Claude CLI aliases",
      cached_catalog: "Last cached CLI catalog",
      unavailable: "CLI catalog unavailable"
    };
    const origin = sources[catalog.source] || catalog.source || "Unavailable";
    const note = $(".launch-model-source",card);
    note.textContent = origin + " · " + models.length + " models" + (catalog.stale ? " · STALE" : "");
    note.title = catalog.warning || catalog.discovered_at || "";
    note.classList.toggle("warning",Boolean(catalog.warning));
  }

  function agentChoiceChanged(card) {
    const custom = $(".launch-agent-choice",card).value === "__custom__";
    $(".launch-custom-wrap",card).hidden = !custom;
    $(".launch-custom-agent",card).required = custom;
  }

  function renderAgentNames(card, item) {
    const select = $(".launch-agent-choice",card);
    const current = item.config?.agent_name || "";
    select.replaceChildren();
    const option = (value,label) => {
      const item = document.createElement("option");
      item.value = value; item.textContent = label;
      select.appendChild(item);
    };
    option("","Use profile default");
    for (const name of (item.catalog?.agent_presets || [])) option(name,name);
    option("__custom__","Custom name…");
    if (current && !(item.catalog?.agent_presets || []).includes(current)) {
      select.value = "__custom__";
      $(".launch-custom-agent",card).value = current;
    } else select.value = current;
    agentChoiceChanged(card);
  }

  function previewState(card) {
    const lane=card.dataset.slot;
    if(!previewStates.has(lane)) {
      previewStates.set(lane,{
        generation:0, timer:null, loaded:null, selectionKey:null,
        approvedHash:null, loading:false, error:null,
      });
    }
    return previewStates.get(lane);
  }
  function currentSelection(card) {
    const agentChoice=$(".launch-agent-choice",card).value;
    const custom=agentChoice==="__custom__";
    return {
      profile:$(".launch-profile",card).value,
      model:$(".launch-model",card).value,
      effort:$(".launch-effort",card).value,
      agent:custom ? $(".launch-custom-agent",card).value.trim() : agentChoice,
      instructions:$(".launch-instructions",card).value,
      auto_continue:$(".launch-autocontinue",card).checked,
      launch_mode:$(".launch-mode",card).value,
      max_subagents:card.dataset.slot==="codex"?
        Number.parseInt($(".launch-max-subagents",card).value,10):50,
      intended_subagents:card.dataset.slot==="codex"?
        Number.parseInt($(".launch-intended-subagents",card).value,10):8,
    };
  }
  function isReviewed(card) {
    const state=previewState(card);
    return Boolean(state.loaded && !state.loading && state.approvedHash &&
      state.approvedHash===state.loaded.preview_hash &&
      state.selectionKey===JSON.stringify(currentSelection(card)));
  }
  function updateLaunchGate(card) {
    const item=currentSlot(card.dataset.slot)||{};
    const running=Boolean(item.runtime?.pid) &&
      ["RUNNING","ADOPTED_RUNNING"].includes(item.runtime?.status);
    const ready=Boolean(item.available && (item.catalog?.models||[]).length && !running);
    const selected=currentSelection(card);
    const capacityValid=card.dataset.slot!=="codex" ||
      (Number.isInteger(selected.max_subagents) && selected.max_subagents>=0 &&
       selected.max_subagents<=50 && Number.isInteger(selected.intended_subagents) &&
       selected.intended_subagents>=0 &&
       selected.intended_subagents<=selected.max_subagents);
    const start=$(".launch-start",card);
    start.disabled=!ready || !capacityValid || !isReviewed(card);
    start.title=!ready ?
      "CLI not ready, or process already running." :
      (isReviewed(card) ? "Launch the reviewed, unchanged prompt" :
       "Open Read prompt and approve the exact current instructions first");
  }
  function renderModal(card) {
    if(activePreviewCard!==card || !dialog.open) return;
    const state=previewState(card), selected=currentSelection(card);
    $("#launchReviewTitle").textContent="Review "+(card.dataset.slot||"CLI")+" prompt";
    $("#launchReviewMetadata").textContent=[
      "Profile: "+selected.profile,"Model: "+(selected.model||"not selected"),
      "Effort: "+(selected.effort||"n/a"),
      "Agent: "+(selected.agent||"Default agent"),
      selected.auto_continue?"Auto-continue ON":"Auto-continue OFF",
      selected.launch_mode==="terminal"?"VISIBLE TERMINAL":"HEADLESS",
      card.dataset.slot==="codex"?
        "Max children: "+selected.max_subagents+" · intended: "+selected.intended_subagents:"",
    ].filter(Boolean).join(" · ");
    const text=state.loaded?.prompt || (state.error || "Assembling your exact prompt…");
    $("#launchReviewText").textContent=text;
    $("#launchReviewCommand").textContent=state.loaded ?
      state.loaded.command.join("\n") : "Waiting for validated CLI arguments.";
    const ok=isReviewed(card);
    const envInfo=state.loaded?.python_environment;
    const envBlocked=envInfo?.available === false;
    $("#launchReviewState").textContent=state.loading?"Refreshing from the launcher…":
      state.error ? "Cannot preview: "+state.error :
      envBlocked ? "Python test environment is not ready: "+envInfo.message :
      ok ? "APPROVED — matches the current launch settings." :
      state.loaded ? "Review the full prompt below. "+(
        envInfo?.available === true
          ? "Verified project Python: "+envInfo.interpreter+" · Playwright/SQLAlchemy available. "
          : ""
      )+"No CLI has started." :
      "Waiting for the preview endpoint.";
    $("#launchReviewState").className="launch-review-state "+
      (state.error||envBlocked?"error":ok?"success":state.loading?"loading":"");
    const approve=$("#launchReviewApprove");
    approve.disabled=state.loading || Boolean(state.error) ||
      envBlocked || !state.loaded || ok;
    approve.textContent=ok?"Approved ✓":"Approve reviewed prompt";
  }
  function invalidatePreview(card, explanation="Selection changed. Rebuilding the exact prompt…") {
    const state=previewState(card);
    state.generation++;
    state.loaded=null;
    state.selectionKey=null;
    state.approvedHash=null;
    state.error=null;
    state.loading=true;
    $(".launch-prompt-preview",card).textContent=explanation;
    $(".launch-preview-note",card).textContent="Approval cleared · "+explanation;
    updateLaunchGate(card);
    renderModal(card);
    clearTimeout(state.timer);
    state.timer=setTimeout(()=>{void loadPreview(card);},190);
  }
  async function loadPreview(card) {
    const state=previewState(card);
    clearTimeout(state.timer);
    const generation=++state.generation;
    const selected=currentSelection(card);
    const key=JSON.stringify(selected);
    state.loaded=null;
    state.selectionKey=null;
    state.loading=true;
    state.error=null;
    $(".launch-prompt-preview",card).textContent="Loading exact effective prompt…";
    $(".launch-preview-note",card).textContent="Validating model, profile and launch parameters…";
    updateLaunchGate(card);
    renderModal(card);
    if(!selected.profile || !selected.model || !selected.effort){
      state.loading=false;
      state.error="Select an installed CLI model, reasoning effort and development profile.";
      $(".launch-prompt-preview",card).textContent=state.error;
      $(".launch-preview-note",card).textContent="Preview unavailable.";
      renderModal(card);
      return;
    }
    try {
      const payload=await fetchJson(
        "/api/launcher/"+encodeURIComponent(card.dataset.slot)+"/preview",
        {method:"POST", headers:{"X-MJobs-Launch-Token":token},
         body:JSON.stringify(selected)}
      );
      if(state.generation!==generation || JSON.stringify(currentSelection(card))!==key)return;
      state.loaded=payload;
      state.selectionKey=key;
      if(state.approvedHash!==payload.preview_hash)state.approvedHash=null;
      const note="Verified preview · "+payload.model+" · "+payload.effort+
        " · "+(payload.auto_continue?"auto-continue ON":"manual completion");
      $(".launch-preview-note",card).textContent=note;
      $(".launch-prompt-preview",card).textContent=payload.prompt;
      labelStatus(card,"Preview ready. Click Read prompt and approve before launching.","");
    }catch(error){
      if(state.generation!==generation)return;
      state.error=error.message;
      state.approvedHash=null;
      $(".launch-preview-note",card).textContent="Preview unavailable — launch locked.";
      $(".launch-prompt-preview",card).textContent="Unable to build prompt: "+error.message;
      labelStatus(card,"Launch locked: "+error.message,"error");
    }finally{
      if(state.generation===generation){
        state.loading=false;
        updateLaunchGate(card);
        renderModal(card);
      }
    }
  }
  function openPreview(card) {
    activePreviewCard=card;
    // Reveal and synchronize the actual inline prompt as well as the modal.
    $(".launch-preview-details",card).open=true;
    if(!dialog.open)dialog.showModal();
    renderModal(card);
    void loadPreview(card);
  }
  $("#launchReviewClose").onclick=()=>dialog.close();
  $("#launchReviewDone").onclick=()=>dialog.close();
  $("#launchReviewRefresh").onclick=()=>{
    if(activePreviewCard)void loadPreview(activePreviewCard);
  };
  $("#launchReviewApprove").onclick=()=>{
    const card=activePreviewCard;
    if(!card)return;
    const state=previewState(card);
    if(state.loading || !state.loaded ||
       state.selectionKey!==JSON.stringify(currentSelection(card)))return;
    state.approvedHash=state.loaded.preview_hash;
    $(".launch-preview-note",card).textContent="APPROVED · "+state.loaded.model+
      " · "+state.loaded.effort+" · exact current prompt.";
    updateLaunchGate(card);
    renderModal(card);
    labelStatus(card,"Prompt approved. Launch CLI is now enabled until you change a selection.","success");
    dialog.close();
  };
  dialog.addEventListener("close",()=>{
    const card=activePreviewCard;
    activePreviewCard=null;
    if(card){
      // The inline preview is always synchronized with its current form,
      // including after Escape/Close. Closing never approves by itself.
      void loadPreview(card);
    }
  });

  function mountCard(item) {
    const wrapper=document.createElement("div");
    wrapper.innerHTML=cardHtml(item);
    const card=wrapper.firstElementChild;
    cards.set(item.lane,card);
    $("#launchGrid").appendChild(card);
    const picker=$(".launch-profile",card);
    for(const profile of profileList(item.lane)){
      const option=document.createElement("option");
      option.value=profile.slug;
      option.textContent=profile.label;
      picker.appendChild(option);
    }
    const cfg=item.config||{};
    if(cfg.launch_profile && profileList(item.lane).some(p=>p.slug===cfg.launch_profile))
      picker.value=cfg.launch_profile;
    $(".launch-instructions",card).value=cfg.prompt_override||"";
    $(".launch-autocontinue",card).checked=Boolean(cfg.auto_continue);
    $(".launch-mode",card).value=cfg.launch_mode||"terminal";
    $(".launch-codex-capacity",card).hidden=item.lane!=="codex";
    $(".launch-max-subagents",card).value=
      Number.isInteger(cfg.max_subagents)?cfg.max_subagents:50;
    $(".launch-intended-subagents",card).value=
      Number.isInteger(cfg.intended_subagents)?cfg.intended_subagents:8;
    renderAgentNames(card,item);
    renderModelOptions(card,item,cfg.model||"",cfg.reasoning||"");
    card.dataset.catalogSignature=JSON.stringify(item.catalog?.models||[])+
      "|"+item.catalog?.source+"|"+Boolean(item.catalog?.stale);

    picker.addEventListener("change",()=>{
      const profile=profiles.find(p=>p.slug===picker.value);
      renderModelOptions(card,currentSlot(item.lane),
        profile?.default_model||"",profile?.effort||"");
      invalidatePreview(card,"Profile changed. Loading its full effective prompt…");
    });
    $(".launch-model",card).addEventListener("change",()=>{
      renderEffortOptions(card,currentSlot(item.lane));
      invalidatePreview(card,"Model changed. Revalidating CLI flags and prompt…");
    });
    $(".launch-effort",card).addEventListener("change",()=>{
      invalidatePreview(card,"Reasoning effort changed. Refreshing prompt…");
    });
    $(".launch-agent-choice",card).addEventListener("change",()=>{
      agentChoiceChanged(card);
      invalidatePreview(card,"Agent/session name changed. Refreshing prompt…");
    });
    $(".launch-custom-agent",card).addEventListener("input",()=>{
      invalidatePreview(card,"Custom session name changed. Refreshing prompt…");
    });
    $(".launch-instructions",card).addEventListener("input",()=>{
      invalidatePreview(card,"Extra instructions changed. Refreshing complete prompt…");
    });
    $(".launch-autocontinue",card).addEventListener("change",()=>{
      invalidatePreview(card,"Continuation policy changed. Revalidating launch…");
    });
    $(".launch-mode",card).addEventListener("change",()=>{
      invalidatePreview(card,"Terminal launch mode changed. Revalidating launch…");
    });
    $(".launch-max-subagents",card).addEventListener("input",()=>{
      const max=Number.parseInt($(".launch-max-subagents",card).value,10);
      const target=$(".launch-intended-subagents",card);
      target.max=String(Math.max(0,max||0));
      if(Number(target.value)>max && Number.isInteger(max))target.value=String(max);
      invalidatePreview(card,"Native Codex subagent maximum changed. Rebuilding command…");
    });
    $(".launch-intended-subagents",card).addEventListener("input",()=>{
      invalidatePreview(card,"Intended active subagents changed. Updating orchestration prompt…");
    });
    $(".launch-preview-details",card).addEventListener("toggle",()=>{
      // Inline opening and closing refreshes from the real, read-only endpoint.
      if(!activePreviewCard || activePreviewCard!==card) void loadPreview(card);
    });
    $(".launch-refresh-models",card).onclick=async()=>{
      const button=$(".launch-refresh-models",card);
      button.disabled=true;
      try{
        const refreshed=await fetchJson("/api/launcher/models/refresh",{
          method:"POST",headers:{"X-MJobs-Launch-Token":token},
          body:JSON.stringify({})
        });
        await refreshOptions();
        invalidatePreview(card,"Model inventory refreshed. Revalidating prompt…");
        const status=refreshed[item.lane];
        labelStatus(card,status?.warning||"Installed CLI model lists refreshed.",
          status?.warning?"warn":"success");
      }catch(error){labelStatus(card,error.message,"error");}
      finally{button.disabled=false;}
    };
    $(".launch-preview",card).onclick=()=>openPreview(card);
    $(".launch-start",card).onclick=()=>doAction(item.lane,card,"launch");
    $(".launch-stop",card).onclick=()=>doAction(item.lane,card,"stop");
    $(".launch-view-terminal",card).onclick=async()=>{
      try{
        const reply=await fetchJson("/api/launcher/"+item.lane+"/terminal-window",{
          method:"POST", headers:{"X-MJobs-Launch-Token":token}, body:"{}",
        });
        labelStatus(card,reply.reused?
          "Reused existing console viewer (PID "+reply.pid+").":
          "Opened read-only live log viewer (PID "+reply.pid+").","success");
      }catch(error){labelStatus(card,error.message,"error");}
    };
    previewState(card);
    updateCard(item);
    invalidatePreview(card,"Loading the current installed CLI profile and selected settings…");
  }

  function updateCard(item) {
    const card = cards.get(item.lane);
    if (!card) return;
    const state = item.runtime || {};
    const catalog = item.catalog || {};
    const signature = JSON.stringify(catalog.models || []) +
      "|" + catalog.source + "|" + Boolean(catalog.stale);
    if (card.dataset.catalogSignature !== signature) {
      const preferred = $(".launch-model",card).value || item.config?.model || "";
      const effort = $(".launch-effort",card).value || item.config?.reasoning || "";
      card.dataset.catalogSignature = signature;
      renderModelOptions(card,item,preferred,effort);
      invalidatePreview(card,"CLI model catalogue changed. Revalidating your selection…");
    }
    const running = Boolean(state.pid) && ["RUNNING","ADOPTED_RUNNING"].includes(state.status);
    const badge = $(".cli-state",card);
    badge.textContent = (running ? "RUNNING" : (item.available ? "READY" : "NOT INSTALLED"));
    badge.className = "pill cli-state " + (running ? "running" : (item.available ? "completed" : "error"));
    $(".launch-worktree",card).textContent = "Worktree: " + item.worktree;
    $(".launch-run-type",card).textContent = item.lane === "codex"
      ? "Codex exec is a noninteractive, one-shot coding run. A visible log console is not the native interactive Codex TUI. Completion or PARTIAL means this run ended; use the next reviewed launch for another task."
      : "This supervised CLI is a noninteractive, bounded coding turn, not the native interactive terminal UI.";
    $(".launch-runtime",card).textContent = state.pid
      ? "PID " + state.pid + " · " + (state.status || "RUNNING") +
        " · " + (state.launch_mode==="terminal"?"VISIBLE WINDOW":"HEADLESS")+
        " · " + (state.effective_model || "")
      : (state.status||"Stopped")+" · branch " + item.branch +
        (state.launch_mode?" · last run "+state.launch_mode:"");
    $(".launch-stop",card).disabled = !running;
    updateLaunchGate(card);
    if (!item.available && !$(".status-line",card).textContent)
      labelStatus(card, item.name + " executable unavailable; no software will be installed.", "warn");
  }

  async function doAction(lane,card,action) {
    const state=previewState(card);
    if(action==="launch" && !isReviewed(card)){
      labelStatus(card,"Read and approve the current exact prompt before launching.","error");
      openPreview(card);
      return;
    }
    const btn=$(action==="launch"?".launch-start":".launch-stop",card);
    btn.disabled=true;
    labelStatus(card,action==="launch"?
      "Verifying approved prompt and preparing isolated worktree…":
      "Stopping supervised CLI…");
    try {
      const body=action==="launch"?
        {...currentSelection(card),preview_hash:state.approvedHash}:{};
      const result=await fetchJson("/api/launcher/"+lane+"/"+action,{
        method:"POST",
        headers:{"X-MJobs-Launch-Token":token},
        body:JSON.stringify(body),
      });
      if(action==="launch"){
        state.approvedHash=null;
        const warnings=result.preflight?.warnings||[];
        labelStatus(card,"Started process "+result.pid+". "+
          (warnings.length?warnings.join(" "):"Worktree prepared without changing main."),
          warnings.length?"warn":"success");
      }else{
        labelStatus(card,"Stop requested. This lane is now marked stopped.","success");
      }
    }catch(error){
      state.approvedHash=null;
      labelStatus(card,error.message,"error");
      // Any failed launch requires explicit re-approval: never reuse a stale
      // digest after a model, prompt, path or environment mismatch.
    }finally{
      updateLaunchGate(card);
      await refreshOptions();
    }
  }

  async function refreshOptions() {
    const data = await fetchJson("/api/launcher/options");
    token = data.token;
    document.body.classList.toggle("mission-reviewed-launch-only", Boolean(data.mission_instance));
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
  function areaRow(row, kind) {
    const label = row.name.replaceAll("_", " ");
    return `<details class="area-progress acceptance-row" data-disclosure-key="${kind}:${safe(row.name)}" data-history-kind="${kind}" data-history-name="${safe(row.name)}">
      <summary>
        <span class="name" title="${safe(label)}">${safe(label)}</span>
        <span class="bar" tabindex="0" role="img" aria-label="${safe(label)}: ${row.pass} of ${row.total} accepted"><span class="fill" style="width:${Number(row.accepted_percent)||0}%"></span></span>
        <span class="count">${row.pass}/${row.total} · ${row.accepted_percent}%</span>
      </summary>
      <div class="acceptance-row-detail">
        <span>PASS <strong>${row.pass}</strong></span>
        <span>PARTIAL <strong>${row.partial}</strong></span>
        <span>NOT RUN <strong>${row.not_run}</strong></span>
        <span class="acceptance-row-delta">Select a historical capture to compare.</span>
      </div>
    </details>`;
  }
  function renderProgress(data) {
    const parent = $("#releaseContent");
    if (!data.available) {
      parent.textContent = "Cannot validate acceptance matrices: " + (data.error || "unknown error");
      return;
    }
    const a = data.original, b = data.reference;
    const sha = data.git?.local_main_sha?.slice(0,10) || "?";
    const checkout = data.git?.checkout_head_sha?.slice(0,10) || "?";
    const checkoutBranch = data.git?.checkout_branch || "(detached HEAD)";
    const remote = data.git?.github_main_sha?.slice(0,10) || "?";
    const synced = data.git?.published_matches_local_main;
    const syncText = synced === true ? "GitHub main synced" : synced === false
      ? "GitHub differs from local main" : "GitHub verification unavailable";
    const priorityHtml = (data.priorities||[]).map(row=>areaRow(row,"priority")).join("");
    const areaHtml = (data.areas||[]).map(row=>areaRow(row,"area")).join("");
    const priorDisclosures = new Map([...parent.querySelectorAll("details[data-disclosure-key]")].map(node=>[node.dataset.disclosureKey,node.open]));
    parent.innerHTML = `
      <div class="release-summary">
        <details class="release-main-metric acceptance-disclosure" data-disclosure-key="summary:original" data-history-kind="original" open><summary>
          <div class="kicker">Original requirements accepted</div>
          <div class="release-big">${a.pass} / ${a.total} <strong>${a.accepted_percent}% accepted</strong></div>
          ${meter(a)}
          <div class="release-explain">${a.partial} partial · ${a.not_run} not yet validated</div>
          </summary><div class="acceptance-expanded">PASS <strong>${a.pass}</strong> · PARTIAL <strong>${a.partial}</strong> · NOT RUN <strong>${a.not_run}</strong><p class="history-insight" data-history-summary="original">Choose a historical capture to compare.</p></div>
        </details>
        <details class="release-side-metric acceptance-disclosure" data-disclosure-key="summary:reference" data-history-kind="reference" open><summary>
          <div class="kicker">Additional reference gaps</div>
          <div class="release-big">${b.pass} / ${b.total}</div>
          ${meter(b)}
          <div class="release-explain">${b.accepted_percent}% accepted · ${b.partial} partial · ${b.not_run} not run</div>
          </summary><div class="acceptance-expanded"><p class="history-insight" data-history-summary="reference">Expand reference IDs to inspect current statuses and history.</p><div id="acceptanceReferenceList" class="acceptance-reference-list">Reading reference statuses…</div></div>
        </details>
        <details class="release-side-metric acceptance-disclosure" data-disclosure-key="summary:readiness" open><summary>
          <div class="kicker">Release readiness</div>
          <div class="release-big">${safe(data.release_readiness)}</div>
          <div class="release-explain">Latest milestone: ${safe(data.milestone)}</div>
          </summary><div class="acceptance-expanded">Release readiness is a separate gate, not the acceptance percentage.<p>Original evidence: ${safe(fmtTime(data.original_as_of))}</p><p>Reference evidence: ${safe(fmtTime(data.reference_as_of))}</p><p>Checkpoint: ${data.checkpoint_current ? "Current" : "Stale / integrator refresh required"}</p></div>
        </details>
      </div>
      <div class="release-meta">Original evidence as of ${safe(fmtTime(data.original_as_of))};
         supplemental evidence as of ${safe(fmtTime(data.reference_as_of))}.
         Latest local check ${safe(fmtTime(data.observed_at))}.
      </div>
      <div class="release-meta">
        <span class="sync-pill ${synced ? "" : "warn"}">${syncText}</span>
        Local main <code>${safe(sha)}</code> · GitHub main <code>${safe(remote)}</code> ·
        Checkout <code>${safe(checkoutBranch)}</code> at <code>${safe(checkout)}</code>.
        Card checkpoint ${data.checkpoint_current ? "CURRENT" : "STALE / INTEGRATOR REFRESH REQUIRED"}.
        ${safe(data.git?.note || "")}
      </div>
      <div class="release-details">
        <details class="release-group acceptance-disclosure" data-disclosure-key="group:areas" open><summary>Feature areas · lowest acceptance first <span>Expand/collapse</span></summary><div>${areaHtml}</div></details>
        <details class="release-group acceptance-disclosure" data-disclosure-key="group:priorities" open><summary>Acceptance by priority <span>Expand/collapse</span></summary><div>${priorityHtml}</div></details>
      </div>
      <div class="progress-disclaimer">${safe(data.caveat)} The page reads original canonical requirements and
      reference gaps on every refresh. It never marks an unfinished requirement PASS or overwrites acceptance records.</div>`;
    for (const details of parent.querySelectorAll("details[data-disclosure-key]")) {
      if (priorDisclosures.has(details.dataset.disclosureKey)) details.open = priorDisclosures.get(details.dataset.disclosureKey);
    }
    window.MJobsAcceptanceTimeline?.onProgress(data,priorDisclosures);
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
