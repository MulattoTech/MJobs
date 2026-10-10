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
  let lastHead = null;
  let displayedFingerprint = "";
  const allCategories = Object.keys(categoryNames);
  const categorySelect = $("#historyCategory");
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
    $("#historyProduct").textContent=String(data.code_commits??0);
    $("#historyBookkeeping").textContent=String(
      (counts.DOCUMENTATION||0)+(counts.BOOKKEEPING||0)
    );
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
    meter.innerHTML=allCategories.map(k=>{
      const num=counts[k]||0;
      if(!num)return "";
      const percentage=total>0?Math.max(0,100*num/total):0;
      return '<span title="'+escapes(categoryNames[k]+" · "+num+" commits")+
        '" style="background:'+badgeColor(k)+";width:"+percentage.toFixed(2)+'%"></span>';
    }).join("");
    $("#historyCategoryLegend").innerHTML=allCategories.filter(k=>counts[k]).map(k=>
      '<span class="history-category-pill" style="color:'+badgeColor(k)+'" title="'+
      escapes(categoryNames[k])+'">'+escapes(categoryNames[k])+" "+counts[k]+"</span>"
    ).join("");
    $("#historyStatus").textContent="Checked "+shortTime(data.observed_at)+" · 5s polling";
    $("#historyStatus").className="sync-pill";
  }
  function renderRows(force=false) {
    if(!latest)return;
    const category=categorySelect.value;
    const filtered=(latest.commits||[]).filter(x=>category==="ALL"||x.category===category);
    const key=[
      latest.head,filtered.length,category,limitSelect.value,
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
      }).join("\n"):"Merge/metadata commit: no changed-file record";
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
        escapes(categoryNames[category]||category)+'</span>'+
        '<span class="detail">'+escapes(commit.area||"Repository")+'</span>'+
        '<span class="detail">'+Number(commit.files_changed||0)+' files</span>'+
        '<span class="detail">+'+Number(commit.added||0)+' / -'+Number(commit.deleted||0)+' lines</span>'+
        (commit.issue_refs||[]).map(r=>'<span class="detail">'+escapes(r)+'</span>').join("")+
        '</div><details class="history-files" '+(openRows.has(commit.sha)?"open":"")+'>'+
        '<summary>Changed files and Git author</summary>'+
        '<div class="history-entry-meta">Git author: '+escapes(commit.author)+
        ' · Committed: '+escapes(readableTime(commit.timestamp))+
        ' · Categories reflect files, not verified acceptance</div>'+
        '<pre>'+escapes(paths+extra)+'</pre></details></article>';
    }).join("");
    feed.scrollTop=previousScroll;
  }
  async function refreshHistory(force=false) {
    if(inProgress)return;
    if(document.hidden && !force)return;
    inProgress=true;
    try{
      const depth=Number(limitSelect.value)||50;
      const response=await fetch("/api/history?limit="+depth+(force?"&refresh=true":""),
        {headers:{"Accept":"application/json"},cache:"no-store"});
      if(!response.ok){
        const e=await response.json().catch(()=>({}));
        throw new Error(e.detail||"HTTP "+response.status);
      }
      const data=await response.json();
      if(!data.available || !Array.isArray(data.commits)){
        throw new Error("Invalid local Git history response");
      }
      const oldHead=lastHead;
      latest=data;
      summaryMetrics(data);
      renderRows(force);
      if(oldHead && oldHead!==data.head){
        const at=data.commits.findIndex(c=>c.sha===oldHead);
        const newCount=at>0?at:1;
        $("#historyNewCommitNotice").textContent=newCount+
          " new local commit"+(newCount===1?"":"s")+" since the previous refresh.";
      }
      lastHead=data.head;
    }catch(error){
      $("#historyStatus").textContent="History unavailable";
      $("#historyStatus").className="sync-pill warn";
      if(!latest)feed.innerHTML='<div class="history-empty">Could not read Git history: '+
        escapes(error.message)+'</div>';
    }finally{
      inProgress=false;
    }
  }
  $("#historyRefresh").addEventListener("click",()=>{void refreshHistory(true);});
  categorySelect.addEventListener("change",()=>{displayedFingerprint="";renderRows(true);});
  limitSelect.addEventListener("change",()=>{displayedFingerprint="";void refreshHistory(true);});
  document.addEventListener("visibilitychange",()=>{
    if(!document.hidden)void refreshHistory(true);
  });
  // Poll only the local Git/filesystem endpoint. Never refresh model catalogs or AI agents.
  setInterval(()=>{void refreshHistory();},5000);
  void refreshHistory();
})();
