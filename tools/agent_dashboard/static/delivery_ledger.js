/* Unified, evidence-tagged delivery record. All API entries are escaped. */
(() => {
  "use strict";
  const $ = s => document.querySelector(s);
  const html = value => String(value ?? "").replace(/[&<>"']/g, ch =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
  const number = value => Number(value || 0).toLocaleString();
  const when = value => {
    const d = new Date(value);
    return value && Number.isFinite(d.valueOf()) ? d.toLocaleString() : "Time unavailable";
  };
  const allowedUrl = value => {
    const v = String(value || "");
    return /^https:\/\/github\.com\/MulattoTech\/MJobs\/(?:issues\/[0-9]+|pull\/[0-9]+|commit\/[0-9a-f]{40}|tree\/[A-Za-z0-9_./%-]+)$/i.test(v) ? v : null;
  };
  const kindText = {
    acceptance: "Committed acceptance", capture: "Acceptance capture",
    commit: "Git commit", branch: "GitHub branch HEAD", pr: "GitHub PR", issue: "GitHub issue",
    workspace: "Uncommitted workspace"
  };
  const pageSize = 60;
  let offset = 0, pending = false, queued = null, sourceTimestamp = null, visible = 0,
      generation = 0, selectedKind = "all", selectedSearch = "";

  const status = text => $("#ledgerStatus").textContent = text;
  const get = async (url, signal) => {
    const response = await fetch(url, {cache:"no-store", headers:{"Accept":"application/json"}, signal});
    if (!response.ok) throw new Error("History endpoint returned HTTP " + response.status);
    const result = await response.json();
    if (!result.available || !Array.isArray(result.events)) throw new Error("History response is not complete");
    return result;
  };
  function urlFor(kind, index, count, search="", refresh=false) {
    const q = new URLSearchParams({kind,offset:String(index),limit:String(count)});
    if (search) q.set("search", search);
    if (refresh) q.set("refresh","true");
    return "/api/delivery-ledger?" + q;
  }
  function showEvent(entry) {
    const link = allowedUrl(entry.url);
    const title = link
      ? '<a href="' + html(link) + '" target="_blank" rel="noopener noreferrer">' +
         html(entry.title) + '</a>'
      : html(entry.title);
    const values = [
      ["Source", entry.source], ["Git SHA", entry.sha],
      ["Requirement", entry.requirement_id],
      ["Evidence", entry.evidence],
      ["Author", entry.author],
      ["Git scopes", (entry.scopes || []).join(" / ")],
      ["Workspace", entry.workspace],
      ["Issue / PR", entry.number],
      ["Changed paths", (entry.paths || []).join("\n")],
    ].filter(([_,value]) => value !== null && value !== undefined && value !== "");
    const details = values.map(([name,value]) =>
      "<div><strong>" + html(name) + ":</strong> " + html(value) + "</div>").join("");
    const state = entry.verified_pass ? "accepted" : "";
    const before = entry.before && entry.after
      ? '<span class="ledger-transition">' + html(entry.before) + " → " + html(entry.after) + "</span>"
      : "";
    const publication = entry.kind === "commit"
      ? (entry.published_on_cached_main ? "On cached main" : "Local/branch commit (publication unverified)")
      : "";
    return '<article class="ledger-record ' + html(entry.kind) + " " + state +
      '" data-id="' + html(entry.id) + '"><div class="ledger-record-heading">' +
      '<span class="ledger-kind">' + html(kindText[entry.kind] || entry.kind) + "</span>" +
      '<time title="' + html(entry.at) + '">' + html(when(entry.at)) + "</time>" +
      "</div><div class=\"ledger-title\">" + title + " " + before + "</div>" +
      '<div class="ledger-source">' + html(entry.source) +
      (publication ? " · " + html(publication) : "") + "</div>" +
      '<details><summary>Provenance and details</summary><div class="ledger-proof">' +
      details + "</div></details></article>";
  }
  function showHighlights(data) {
    const entries = data.events || [];
    const panel = $("#ledgerHighlights");
    panel.innerHTML = '<div class="ledger-highlight-head"><strong>Latest recorded requirement PASS transitions</strong>' +
      '<small>Changes recorded in Git main or captured locally—not automatically attributed to an AI agent</small></div>' +
      (entries.length ? entries.map(entry => {
        const url = allowedUrl(entry.url);
        const title = url
          ? '<a href="' + html(url) + '" target="_blank" rel="noopener noreferrer">' +
              html(entry.requirement_id || entry.title) + "</a>"
          : html(entry.requirement_id || entry.title);
        return '<div class="ledger-highlight">' + title +
          '<span>' + html(entry.before) + " → " + html(entry.after) +
          " · " + html(when(entry.at)) + "</span></div>";
      }).join("") : '<p>No verified PASS transition is present in the available historical sources.</p>');
  }
  function showCoverage(data) {
    const c = data.coverage || {}, git = c.git || {}, matrix = c.matrix || {},
        gh = c.github || {}, ws = c.workspaces || {};
    const pull = gh.pull_requests || {}, issues = gh.issues || {}, branches = gh.branches || {}, comparisons = gh.branch_comparisons || {};
    const parts = [
      ["Git commits (deduplicated)", git.unique_commits],
      ["GitHub PR records", pull.available ? pull.records : "Unavailable"],
      ["GitHub issue records", issues.available ? issues.records : "Unavailable"],
      ["Published branch heads", branches.available ? branches.records : "Unavailable"],
      ["Compared remote branches", comparisons.inspected],
      ["Remote-only branch commit rows", comparisons.additional_commit_rows],
      ["Local workspaces inventoried", ws.registered_and_independent],
      ["Original matrix revisions", matrix.original?.revisions_examined],
      ["Reference matrix revisions", matrix.reference?.revisions_examined],
    ];
    $("#ledgerSummary").innerHTML = [
      ["Recorded events", number(data.total_recorded)],
      ["Known Git commits", number(git.unique_commits)],
      ["GitHub PRs", pull.available ? number(pull.records) : "Unavailable"],
      ["Original matrix revisions", number(matrix.original?.revisions_examined)],
    ].map(([label,value]) => '<div><small>' + html(label) + '</small><strong>' +
          html(value) + "</strong></div>").join("");
    $("#ledgerCoverage").innerHTML = '<details><summary>Source coverage, branch heads and limitations</summary>' +
      '<div class="ledger-coverage-grid">' + parts.map(([label,value]) =>
        '<div><strong>' + html(label) + "</strong>: " +
        html(value === undefined ? "Unavailable" : value) + "</div>").join("") + "</div>" +
      (matrix.original?.truncated || matrix.reference?.truncated || git.truncation_possible ||
       pull.may_be_truncated || issues.may_be_truncated || branches.may_be_truncated || comparisons.truncated
       ? "<p>Some historical sources reached their bounded inspection limit. The visible total is not exhaustive.</p>"
       : "") +
      '<div><strong>Remote comparison limitations:</strong> ' +
      html((comparisons.errors || []).map(x => x.name + ": " + (x.error || "ahead " + x.ahead_by + " > returned " + x.returned)).join("; ") || "None reported") +
      "</div>" +
      '<div><strong>Known published branch HEADs:</strong> ' +
      ((branches.heads || []).map(item => html(item.name) + " (" + html((item.sha || "").slice(0, 8)) + ")").join(", ") || "Unavailable") +
      "</div></details>";
    const workspaces = data.workspaces || [];
    $("#ledgerWorkspaceSummary").textContent =
      "All locally registered and retained workspaces (" + number(workspaces.length) + ")";
    $("#ledgerWorkspaces").innerHTML = workspaces.map(item =>
      '<div class="ledger-workspace"><code>' + html(item.workspace) +
      "</code> · " + html(item.branch || item.kind || "") +
      " · " + html(item.state || "not inspected") +
      (item.changed_count ? " · " + number(item.changed_count) + " changed files" : "") + "</div>"
    ).join("");
    $("#ledgerCaveat").textContent = data.note || "Historical sources were not verified.";
  }
  async function load({reset=false,refresh=false} = {}) {
    if (pending) {
      queued = {reset,refresh};
      return;
    }
    pending = true;
    const self = ++generation;
    $("#ledgerOlder").disabled = true;
    status("Reading historical sources...");
    try {
      if(reset) { offset = 0; visible = 0; sourceTimestamp = null; }
      const result = await get(urlFor(selectedKind, offset, pageSize, selectedSearch, refresh));
      if(self !== generation) return;
      if(sourceTimestamp && sourceTimestamp !== result.observed_at && offset) {
        status("Sources changed. Refresh to restart from latest record.");
        return;
      }
      sourceTimestamp = result.observed_at;
      showCoverage(result);
      const newRows = (result.events || []).map(showEvent).join("");
      const feed = $("#ledgerFeed");
      if(reset) feed.innerHTML = newRows || '<div class="history-empty">No matching events in recorded sources.</div>';
      else feed.insertAdjacentHTML("beforeend", newRows);
      visible += result.events.length;
      offset += result.events.length;
      $("#ledgerPaging").textContent = number(visible) + " of " +
          number(result.total_matching) + " matching records shown";
      $("#ledgerOlder").disabled = !result.has_more;
      status("Observed " + when(result.observed_at));
      if(reset) {
        try {
          const passes = await get(urlFor("pass", 0, 8));
          if(self === generation) showHighlights(passes);
        } catch (_) {
          $("#ledgerHighlights").textContent = "PASS transitions unavailable from these sources.";
        }
      }
    } catch (error) {
      status("Activity history unavailable");
      $("#ledgerPaging").textContent = error.message;
      if(reset) $("#ledgerFeed").textContent = "Recorded history could not be retrieved.";
    } finally {
      pending = false;
      if (queued) {
        const request = queued;
        queued = null;
        void load(request);
      }
    }
  }
  $("#ledgerKind").addEventListener("change", () => {
    selectedKind = $("#ledgerKind").value;
    void load({reset:true});
  });
  $("#ledgerSearchGo").addEventListener("click", () => {
    selectedSearch = $("#ledgerSearch").value.trim();
    void load({reset:true});
  });
  $("#ledgerSearch").addEventListener("keydown", event => {
    if(event.key === "Enter") {
      event.preventDefault();
      $("#ledgerSearchGo").click();
    }
  });
  $("#ledgerRefresh").addEventListener("click", () => { void load({reset:true,refresh:true}); });
  $("#ledgerOlder").addEventListener("click", () => { void load(); });
  $("#ledgerExport").addEventListener("click", async () => {
    const button = $("#ledgerExport");
    if(pending || button.disabled) return;
    button.disabled = true;
    const previous = button.textContent;
    const rows = [];
    let at = 0, stamp = null;
    let metadata = null;
    try {
      do {
        const part = await get(urlFor("all", at, 150));
        if(stamp && stamp !== part.observed_at) {
          throw new Error("Source changed during export. Retry export to avoid mixed versions.");
        }
        stamp = part.observed_at;
        metadata = part;
        rows.push(...part.events);
        at += part.events.length;
        button.textContent = "Exported " + number(rows.length) + " metadata events...";
        if(!part.has_more) break;
        if(part.events.length === 0) throw new Error("History pagination stalled");
      } while(at < 100000);
      const payload = JSON.stringify({
        created_at: new Date().toISOString(),
        note: metadata.note, coverage: metadata.coverage,
        workspaces: metadata.workspaces, events: rows,
      }, null, 2);
      const objectUrl = URL.createObjectURL(new Blob([payload],{type:"application/json"}));
      const download = document.createElement("a");
      download.href = objectUrl;
      download.download = "MJobs-recorded-delivery-history.json";
      document.body.appendChild(download);
      download.click();
      download.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 12000);
    } catch(error) {
      $("#ledgerPaging").textContent = "Export incomplete: " + error.message;
    } finally {
      button.textContent = previous;
      button.disabled = false;
    }
  });
  document.addEventListener("visibilitychange", () => {
    if(!document.hidden) void load({reset:true});
  });
  void load({reset:true});
  setInterval(() => { if(!document.hidden && !pending) void load({reset:true}); }, 120000);
})();
