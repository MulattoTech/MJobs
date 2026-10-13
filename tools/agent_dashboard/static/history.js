/* Mission Control development history: committed work, not invented AI turn attribution. */
(() => {
  "use strict";
  const $ = s => document.querySelector(s);
  const escapes = value => String(value ?? "").replace(/[&<>"']/g, c => ({
    "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"
  }[c]));
  const categoryNames = {
    CORE_FEATURE:"Core feature",
    PRODUCT_FEATURE:"Product feature",
    BUGFIX:"Bug fix",
    PRODUCT_CHANGE:"Product code",
    TEST:"Tests",
    DOCUMENTATION:"Documentation",
    BOOKKEEPING:"Bookkeeping",
    OPERATIONS:"Operations",
    OTHER:"Other / merge",
  };
  const categoryColors = {
    CORE_FEATURE:"#58dfaa",
    PRODUCT_FEATURE:"#71a9f8",
    BUGFIX:"#fdc27b",
    PRODUCT_CHANGE:"#66d3d5",
    TEST:"#eac46d",
    DOCUMENTATION:"#a99aff",
    BOOKKEEPING:"#eb99c4",
    OPERATIONS:"#a8bed6",
    OTHER:"#8597a9",
  };
  let latest = null;
  let inProgress = false;
  let sourceGeneration = 0;
  let lastHead = null;
  let lastSource = null;
  let displayedFingerprint = "";
  const allCategories = Object.keys(categoryNames);
  const categorySelect = $("#historyCategory");
  const sourceSelect = $("#historySource");
  const limitSelect = $("#historyLimit");
  const feed = $("#historyTimeline");

  function readableTime(value) {
    const time = new Date(value);
    return value && Number.isFinite(time.valueOf()) ?
      time.toLocaleString([], {month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}) :
      "Time unavailable";
  }
  function shortTime(value) {
    const t=new Date(value);
    return value && Number.isFinite(t.valueOf()) ?
      t.toLocaleTimeString([], {hour:"numeric",minute:"2-digit",second:"2-digit"}) :
      "unknown";
  }
  function relativeTime(value) {
    const d=new Date(value);
    if(!value || !Number.isFinite(d.valueOf()))return "Not observed";
    const secs=Math.max(0,Math.floor((Date.now()-d.valueOf())/1000));
    if(secs<60)return secs+"s ago";
    if(secs<3600)return Math.floor(secs/60)+"m ago";
    if(secs<86400)return Math.floor(secs/3600)+"h ago";
    return Math.floor(secs/86400)+"d ago";
  }
  function badgeColor(type) {
    return categoryColors[type] || categoryColors.OTHER;
  }
  function summaryMetrics(data) {
    const counts=data.categories||{};
    $("#historySample").textContent=data.sample_size==null?"—":String(data.sample_size);
    const hasClassification=data.classification_available !== false;
    $("#historyProduct").textContent=hasClassification?String(data.code_commits??0):"N/A";
    $("#historyBookkeeping").textContent=hasClassification?String(
      (counts.DOCUMENTATION||0)+(counts.BOOKKEEPING||0)
    ):"N/A";
    const model=data.codex||{};
    $("#historyTurn").textContent=model.available?
      (model.model||"Unknown model")+" · "+(model.effort||""):"Unverified";
    const stamp=model.last_turn_at||model.last_token_event_at;
    $("#historyTurnDetail").textContent=model.available?
      (stamp?"Last recorded turn "+relativeTime(stamp)+" · ":"")+
      "Independent session activity; not commit attribution.":
      "No local Codex root turn metadata found";
    const total=data.sample_size||0;
    const meter=$("#historyMeter");
    meter.innerHTML=hasClassification?allCategories.map(k=>{
      const num=counts[k]||0;
      if(!num)return "";
      const percentage=total>0?Math.max(0,100*num/total):0;
      return '<span title="'+escapes(categoryNames[k]+" · "+num+" commits")+
        '" style="background:'+badgeColor(k)+";width:"+percentage.toFixed(2)+'%"></span>';
    }).join(""):"";
    $("#historyCategoryLegend").innerHTML=hasClassification?allCategories.filter(k=>counts[k]).map(k=>
      '<span class="history-category-pill" style="color:'+badgeColor(k)+'" title="'+
      escapes(categoryNames[k])+'">'+escapes(categoryNames[k])+" "+counts[k]+"</span>"
    ).join(""):"GitHub main metadata: no file classifications were inferred";
    $("#historyStatus").textContent="Checked "+shortTime(data.observed_at)+" · "+
      (data.refresh_seconds||5)+"s source cache";
    $("#historySourceNote").textContent=data.source_description||"Unknown history source";
    $("#historyStatus").className="sync-pill";
  }
  function renderRows(force=false) {
    if(!latest)return;
    const category=categorySelect.value;
    const filtered=(latest.commits||[]).filter(x=>category==="ALL"||x.category===category);
    const key=[
      latest.history_source,latest.head,filtered.length,category,limitSelect.value,
      filtered.map(x=>x.sha).join("|")
    ].join(":");
    if(!force && displayedFingerprint===key)return;
    displayedFingerprint=key;
    const openRows=new Set(
      Array.from(feed.querySelectorAll(".history-entry details[open]"))
        .map(el=>el.closest(".history-entry")?.dataset.sha)
    );
    const previousScroll=feed.scrollTop;
    if(!filtered.length){
      feed.innerHTML='<div class="history-empty">No commits in this category within the selected history window.</div>';
      return;
    }
    feed.innerHTML=filtered.map(commit=>{
      const category=commit.category||"OTHER";
      const files=Array.isArray(commit.files)?commit.files:[];
      const paths=files.length?files.map(x=>{
        const a=Number(x.additions)||0,d=Number(x.deletions)||0;
        return "+"+a+" / -"+d+"  "+x.path;
      }).join("\n"):(commit.metadata_only ?
        "GitHub main metadata only; changed paths and line counts were not inspected" :
        "Merge/metadata commit: no changed-file record");
      const extra=commit.files_truncated?
        "\n… "+commit.files_truncated+" additional paths omitted": "";
      return '<article class="history-entry" data-sha="'+escapes(commit.sha)+'" style="--history-color:'+
        badgeColor(category)+'">'+
        '<div class="history-entry-head"><code>'+escapes(commit.short_sha||commit.sha?.slice(0,10))+'</code>'+
        '<span class="history-entry-subject">'+escapes(commit.subject)+'</span>'+
        '<time class="history-entry-time" title="'+escapes(commit.timestamp)+'">'+
        escapes(readableTime(commit.timestamp))+'</time></div>'+
        '<div class="history-entry-badges">'+
        '<span class="history-category" title="'+escapes(commit.category_explanation||category)+'">'+
        escapes(commit.metadata_only?"Published commit":categoryNames[category]||category)+'</span>'+
        '<span class="detail">'+escapes(commit.area||"Repository")+'</span>'+
        (commit.metadata_only?
          '<span class="detail">Published on GitHub main</span>'+
          '<span class="detail">Changed files not inspected</span>':
          '<span class="detail">'+Number(commit.files_changed||0)+' files</span>'+
          '<span class="detail">+'+Number(commit.added||0)+' / -'+Number(commit.deleted||0)+' lines</span>')+
        (commit.issue_refs||[]).map(r=>'<span class="detail">'+escapes(r)+'</span>').join("")+
        '</div><details class="history-files" '+(openRows.has(commit.sha)?"open":"")+'>'+
        '<summary>'+(commit.metadata_only?"Published commit details":"Changed files and Git author")+'</summary>'+
        '<div class="history-entry-meta">Git author: '+escapes(commit.author)+
        ' · Committed: '+escapes(readableTime(commit.timestamp))+
        ' · '+(commit.metadata_only ? "GitHub metadata only; files not inspected" :
          "Categories reflect files, not verified acceptance")+'</div>'+
        '<pre>'+escapes(paths+extra)+'</pre></details></article>';
    }).join("");
    feed.scrollTop=previousScroll;
  }
  async function refreshHistory(force=false) {
    if(inProgress)return;
    if(document.hidden && !force)return;
    inProgress=true;
    const requestedSource=sourceSelect.value;
    const requestedGeneration=sourceGeneration;
    const stillSelected=()=>requestedGeneration===sourceGeneration && sourceSelect.value===requestedSource;
    try{
      const depth=Number(limitSelect.value)||50;
      const source=sourceSelect.value;
      const response=await fetch("/api/history?source="+encodeURIComponent(source)+
        "&limit="+depth+(force?"&refresh=true":""),
        {headers:{"Accept":"application/json"},cache:"no-store"});
      if(!response.ok){
        const e=await response.json().catch(()=>({}));
        throw new Error(e.detail||"HTTP "+response.status);
      }
      const data=await response.json();
      if(!stillSelected())return;
      if(!data.available || !Array.isArray(data.commits) || data.history_source!==requestedSource){
        throw new Error("Invalid selected-source history response");
      }
      const oldHead=lastSource===data.history_source ? lastHead : null;
      latest=data;
      summaryMetrics(data);
      renderRows(force);
      if(oldHead && oldHead!==data.head){
        const at=data.commits.findIndex(c=>c.sha===oldHead);
        const newCount=at>0?at:1;
        $("#historyNewCommitNotice").textContent=newCount+
          " new commit"+(newCount===1?"":"s")+" in the selected source since the previous refresh.";
      }
      lastHead=data.head;
      lastSource=data.history_source;
    }catch(error){
      if(!stillSelected())return;
      $("#historyStatus").textContent="History unavailable";
      $("#historyStatus").className="sync-pill warn";
      if(!latest){
        $("#historySourceNote").textContent="The selected history source could not be verified.";
        feed.innerHTML='<div class="history-empty">Could not read Git history: '+
          escapes(error.message)+'</div>';
      }
    }finally{
      inProgress=false;
      // If the source was changed while a request was pending, refresh the new selection.
      if(!stillSelected())void refreshHistory(true);
    }
  }
  $("#historyRefresh").addEventListener("click",()=>{void refreshHistory(true);});
  sourceSelect.addEventListener("change",()=>{
    sourceGeneration++;
    lastHead=null;lastSource=null;displayedFingerprint="";
    $("#historyNewCommitNotice").textContent="";
    categorySelect.disabled=sourceSelect.value==="github";
    if(categorySelect.disabled)categorySelect.value="ALL";
    $("#historySourceNote").textContent="Switching history source...";
    $("#historyStatus").textContent="Loading selected source";
    for(const id of ["historySample","historyProduct","historyBookkeeping"]){
      $("#"+id).textContent="—";
    }
    $("#historyMeter").replaceChildren();
    $("#historyCategoryLegend").textContent="Waiting for selected source.";
    latest=null;
    feed.textContent="Loading the selected history source...";
    void refreshHistory(true);
  });
  categorySelect.addEventListener("change",()=>{displayedFingerprint="";renderRows(true);});
  limitSelect.addEventListener("change",()=>{displayedFingerprint="";void refreshHistory(true);});
  document.addEventListener("visibilitychange",()=>{
    if(!document.hidden)void refreshHistory(true);
  });
  // Poll the selected read-only source. GitHub-main requests are cached server-side.
  // No model calls, automatic Git fetch, checkout mutations or agent launches.
  setInterval(()=>{void refreshHistory();},5000);
  void refreshHistory();
})();
