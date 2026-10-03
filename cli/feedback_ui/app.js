'use strict';

const $ = (id) => document.getElementById(id);
const model = {tasks: [], reports: new Map(), active: null, token: '', key: '', saving: new Map(), timers: new Map(), errors: new Map(), conflicts: new Set(), currents:new Map()};
let storageFailed = false;

function globalError(message) { $('global-error').textContent = message; $('global-error').hidden = !message; }
function taskFor(report) { return model.tasks.find(task => task.id === report.task_id); }
function activeReport() { return model.reports.get(model.active); }
function clone(value) { return JSON.parse(JSON.stringify(value)); }
function cache() {
  try {
    localStorage.setItem(model.key, JSON.stringify({active: model.active, reports: [...model.reports.values()]}));
    storageFailed = false;
    return true;
  } catch {
    storageFailed = true;
    globalError('Browser recovery storage is unavailable. Keep this page open until “Saved on this computer” appears.');
    return false;
  }
}
function saveStatus() {
  const item = activeReport();
  if (!item) return;
  if (model.conflicts.has(item.id)) $('save-status').textContent = 'Newer version found — your local copy is preserved';
  else if (model.errors.has(item.id)) $('save-status').textContent = storageFailed ? 'Not saved — keep this page open' : 'Saved in this browser — computer save needs retry';
  else if (item._dirty) $('save-status').textContent = storageFailed ? 'Saving — keep this page open' : 'Saved in this browser · saving to computer…';
  else $('save-status').textContent = 'Saved on this computer';
  $('conflict').hidden = !model.conflicts.has(item.id);
}
async function api(path, payload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(path, payload ? {method:'POST', signal:controller.signal, headers:{'Content-Type':'application/json','X-MJobs-Token':model.token}, body:JSON.stringify(payload)} : {signal:controller.signal});
    const data = await response.json();
    if (!response.ok) { const error = new Error(data.error || 'Local service unavailable'); error.status = response.status; error.current=data.current; throw error; }
    return data;
  } finally { clearTimeout(timer); }
}
async function refreshToken() { const state = await api('/api/state'); model.token = state.csrf_token; }
function payload(item, action) {
  return {id:item.id, title:item.title, task_id:item.task_id, mode:item.mode, values:item.values, expected_revision:item.revision || 0, action};
}
async function post(item, action) {
  try { return await api('/api/reports', payload(item, action)); }
  catch (error) {
    if (error.status !== 403) throw error;
    // A restarted server rotates its token; refresh without dropping the draft.
    await refreshToken();
    return await api('/api/reports', payload(item, action));
  }
}
function schedule(id) {
  clearTimeout(model.timers.get(id));
  model.timers.set(id, setTimeout(() => { void persist(id); }, 250));
}
async function persist(id) {
  clearTimeout(model.timers.get(id));
  if (model.saving.has(id)) return model.saving.get(id);
  if (model.conflicts.has(id)) return false;
  const work = (async () => {
    let item = model.reports.get(id);
    while (item?._dirty) {
      const snapshot = clone(item);
      try {
        const saved = await post(snapshot, 'save');
        item = model.reports.get(id);
        item.revision = saved.revision;
        item.updated_at = saved.updated_at;
        if (item._generation === snapshot._generation) {
          Object.assign(item, saved, {_dirty:false});
        }
        model.errors.delete(id);
        cache();
        renderHistory();
        if (model.active === id) saveStatus();
      } catch (error) {
        model.errors.set(id, error.message);
        if (error.status === 409) { model.conflicts.add(id); model.currents.set(id,error.current); }
        if (model.active === id) saveStatus();
        return false;
      }
    }
    return true;
  })();
  model.saving.set(id, work);
  try { return await work; } finally { model.saving.delete(id); }
}
function changed() {
  let item = activeReport();
  if (!item) return;
  if (item.status === 'ready') {
    // Keep the submitted report and its receipt visible in history.
    item = {...clone(item), id:crypto.randomUUID(), revision:0, status:'draft', session_dir:null};
    model.reports.set(item.id,item); model.active=item.id;
  }
  item.title = $('report-title').value;
  item.mode = $('report-mode').value;
  for (const input of $('task-fields').querySelectorAll('[data-field]')) item.values[input.dataset.field] = input.value;
  item._generation = (item._generation || 0) + 1;
  item._dirty = true;
  item.status = 'draft';
  item.updated_at = new Date().toISOString();
  $('handoff-result').hidden = true;
  cache();
  saveStatus();
  renderHistory();
  renderPreview();
  schedule(item.id);
}
function renderHistory() {
  const history = $('history');
  history.replaceChildren();
  const items = [...model.reports.values()].sort((a,b) => (b.updated_at || '').localeCompare(a.updated_at || ''));
  $('report-count').textContent = items.length;
  $('history-empty').hidden = items.length > 0;
  for (const item of items) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'history-item' + (item.id === model.active ? ' active' : '');
    const title = item.title.trim() || `Untitled: ${taskFor(item)?.title || 'task'}`;
    button.setAttribute('aria-label', title);
    const name = document.createElement('span'); name.textContent = title;
    const info = document.createElement('small');
    info.textContent = `${item.status === 'ready' ? 'Ready for Codex' : 'Draft'} · ${item.mode === 'PERSONAL' ? 'Personal' : 'Fixture'}`;
    button.append(name,info);
    button.addEventListener('click', () => openReport(item.id));
    history.append(button);
  }
}
function renderCatalogue() {
  const search = $('task-search').value.trim().toLowerCase();
  const groups = new Map();
  for (const task of model.tasks) {
    if (!`${task.title} ${task.description} ${task.group}`.toLowerCase().includes(search)) continue;
    if (!groups.has(task.group)) groups.set(task.group, []);
    groups.get(task.group).push(task);
  }
  $('task-groups').replaceChildren();
  for (const [name,tasks] of groups) {
    const section = document.createElement('section');
    const heading = document.createElement('h3'); heading.textContent = name;
    const grid = document.createElement('div'); grid.className = 'task-grid';
    for (const task of tasks) {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'task-card'; button.dataset.taskId = task.id;
      const number = document.createElement('span'); number.className = 'task-number'; number.textContent = `TASK ${task.id.slice(0,2)}`;
      const title = document.createElement('strong'); title.textContent = task.title;
      const description = document.createElement('p'); description.textContent = task.description;
      button.append(number,title,description);
      button.addEventListener('click', () => newReport(task));
      grid.append(button);
    }
    section.append(heading,grid); $('task-groups').append(section);
  }
  $('no-tasks').hidden = groups.size > 0;
}
function newReport(task) {
  const existing = [...model.reports.values()].find(item => item.task_id === task.id && item.status === 'draft');
  if (existing) { openReport(existing.id); return; }
  const item = {id:crypto.randomUUID(),task_id:task.id,title:'',mode:'PERSONAL',values:{},revision:0,status:'draft',updated_at:new Date().toISOString(),_dirty:true,_generation:1};
  if (task.id === '01-session-start') {
    item.values.workspace = model.workspace;
    item.values.service_url = 'http://127.0.0.1:18701';
    item.values.branch = model.branch === 'unavailable' ? '' : model.branch;
    item.values.build_provenance = model.commit === 'unavailable' ? '' : model.commit;
  }
  if (task.id === '17-launch-diagnosis') { item.values.workspace=model.workspace; item.values.mode_url='PERSONAL · http://127.0.0.1:18701'; }
  if (['13-process-feedback','18-capture-diagnosis'].includes(task.id) && model.captures.length) item.values.session_dir=model.captures[0].path;
  model.reports.set(item.id,item);
  openReport(item.id);
  schedule(item.id);
}
function makeField(field, item) {
  const label = document.createElement('label'); label.htmlFor = `field-${field.id}`;
  label.append(document.createTextNode(field.label));
  if (field.required) { const badge = document.createElement('span'); badge.className='required'; badge.textContent='Required'; label.append(badge); }
  const input = document.createElement(field.kind === 'select' ? 'select' : field.kind === 'textarea' ? 'textarea' : 'input');
  input.id = `field-${field.id}`; input.dataset.field = field.id;
  if (field.kind === 'select') {
    const empty = document.createElement('option'); empty.value=''; empty.textContent=field.placeholder || 'Choose an option'; input.append(empty);
    for (const value of field.options || []) { const option = document.createElement('option'); option.value=value; option.textContent=value; input.append(option); }
  } else { input.placeholder = field.placeholder || ''; input.maxLength=20000; }
  input.required = Boolean(field.required);
  input.value = item.values[field.id] || '';
  label.append(input);
  if (field.kind === 'text' && ['evidence','local_evidence','session_dir','original_session','retest_session'].includes(field.id) && model.captures.length) {
    const list=document.createElement('datalist'); list.id=`captures-${field.id}`;
    for(const capture of model.captures) { const option=document.createElement('option'); option.value=capture.path; option.textContent=capture.id; list.append(option); }
    input.setAttribute('list',list.id); label.append(list);
  }
  return label;
}
function openReport(id) {
  model.active = id;
  const item = activeReport(); const task = taskFor(item);
  if (!task) { globalError('This saved task is unavailable. Your draft has been preserved.'); return; }
  $('catalogue').hidden = true; $('editor').hidden = false;
  $('task-title').textContent = task.title;
  $('task-group').textContent = task.group;
  $('task-description').textContent = task.description;
  $('report-title').value = item.title;
  $('report-mode').value = item.mode;
  const fields = $('task-fields'); fields.replaceChildren();
  const details = document.createElement('details'); details.className='optional-fields';
  const summary = document.createElement('summary'); summary.textContent='Additional details (optional)';
  const optional = document.createElement('div'); optional.className='optional-grid';
  details.append(summary,optional);
  for (const field of task.fields) (field.required ? fields : optional).append(makeField(field,item));
  if (optional.children.length) fields.append(details);
  details.open = task.fields.some(field => !field.required && item.values[field.id]);
  cache(); renderHistory(); renderPreview(); saveStatus();
  showHandoff(item);
}
function showCatalogue() {
  model.active = null; cache();
  $('editor').hidden = true; $('catalogue').hidden = false;
  $('task-search').value=''; renderCatalogue(); renderHistory();
}
function renderPreview() {
  const item = activeReport();
  const task = taskFor(item);
  $('preview').textContent = [item.title || '(Untitled draft)',task.title,`Mode: ${item.mode}`,...task.fields.filter(field => item.values[field.id]).map(field => `\n${field.label}\n${item.values[field.id]}`)].join('\n');
}
function showHandoff(item) {
  const result = $('handoff-result');
  result.hidden = item.status !== 'ready';
  if (item.status !== 'ready') return;
  result.replaceChildren();
  const title = document.createElement('strong'); title.textContent = 'Saved for Codex';
  const text = document.createElement('p'); text.textContent = 'Your report is ready. Tell Codex “process my latest feedback”; the local files are already prepared. Nothing has been sent or started automatically.';
  const path = document.createElement('p'); path.textContent = item.session_dir || '';
  result.append(title,text,path);
}
async function ready(event) {
  event.preventDefault();
  const item = activeReport();
  if (!item || !$('report-form').reportValidity()) return;
  if (model.conflicts.has(item.id)) return;
  $('form-fields').disabled = true; $('ready').disabled = true;
  try {
    if (!await persist(item.id)) throw new Error('Save needs attention. Your draft has been retained.');
    const saved = await post(clone(item),'ready');
    Object.assign(item,saved,{_dirty:false}); cache(); renderHistory(); showHandoff(item); saveStatus();
    globalError('');
  } catch (error) {
    if (error.status === 409) { model.conflicts.add(item.id); model.currents.set(item.id,error.current); }
    globalError(`Could not prepare the handoff: ${error.message}. Your draft has not been discarded.`); saveStatus();
  } finally { $('form-fields').disabled = false; $('ready').disabled = false; }
}
async function keepCopy() {
  const source = activeReport(); if (!source) return;
  const item = {...clone(source),id:crypto.randomUUID(),revision:0,status:'draft',session_dir:null,_dirty:true,_generation:1,title:`${source.title.slice(0,222)} (recovered copy)`};
  model.reports.set(item.id,item); openReport(item.id);
  const safelyCached = cache();
  if (safelyCached || await persist(item.id)) {
    const current=model.currents.get(source.id);
    if(current) model.reports.set(source.id,{...current,_dirty:false,_generation:0});
    model.conflicts.delete(source.id); model.errors.delete(source.id);
    cache(); renderHistory();
  }
  schedule(item.id);
}
async function hydrate(tasks,state) {
    model.tasks=tasks; model.token=state.csrf_token; model.workspace=state.workspace;
    model.branch=state.branch; model.commit=state.commit;
    model.captures=state.captures || [];
    model.key = `mjobs.feedback.v1:${state.output_root}`;
    try { localStorage.setItem('mjobs.feedback.bootstrap',JSON.stringify({tasks,state:{...state,reports:[],csrf_token:''}})); } catch { }
    let local = {};
    try { local=JSON.parse(localStorage.getItem(model.key) || '{}'); }
    catch { globalError('Browser recovery data could not be read. Computer-saved reports are still available.'); }
    for (const item of state.reports) model.reports.set(item.id,{...item,_dirty:false,_generation:0});
    for (const item of Array.isArray(local.reports) ? local.reports : []) {
      if (!item || typeof item.id !== 'string' || !model.tasks.some(task=>task.id===item.task_id) || typeof item.values !== 'object' || !item.values) continue;
      const remote = model.reports.get(item.id);
      if (!remote || item._dirty) {
        model.reports.set(item.id,item);
        if (remote && remote.revision !== item.revision) {
          if (JSON.stringify(payload({...item,revision:0},'save')) === JSON.stringify(payload({...remote,revision:0},'save'))) {
            model.reports.set(item.id,{...remote,_dirty:false,_generation:0});
          } else { model.conflicts.add(item.id); model.currents.set(item.id,remote); }
        }
      }
    }
    $('workspace-location').textContent = `Private workspace: ${state.workspace} · Drafts: ${state.output_root}`;
    renderCatalogue(); renderHistory();
    if (local.active && model.reports.has(local.active)) openReport(local.active);
    for (const item of model.reports.values()) if (item._dirty && !model.conflicts.has(item.id)) schedule(item.id);
}
async function init() {
  let tasks, state;
  try {
    [tasks,state] = await Promise.all([api('/api/tasks'),api('/api/state')]);
  } catch {
    try {
      ({tasks,state}=JSON.parse(localStorage.getItem('mjobs.feedback.bootstrap')));
      globalError('The local service is unavailable. Restored browser drafts are available below; keep this browser until computer saving resumes.');
    } catch { globalError('The feedback service could not be loaded. Reopen Start-Feedback.cmd, then refresh. Saved drafts are retained.'); return; }
  }
  if (!navigator.locks) { globalError('Use a current Edge or Chrome browser to protect drafts across windows. Existing drafts have not been changed.'); return; }
  void navigator.locks.request(`mjobs-feedback-editor:${state.output_root}`,{ifAvailable:true},async lock => {
    if (!lock) {
      $('new-task').disabled=true; $('task-search').disabled=true;
      globalError('Feedback is already open in another tab in this browser. Continue there, or close that tab and refresh this one. This prevents two windows from overwriting unsaved words.');
      return;
    }
    await hydrate(tasks,state);
    await new Promise(() => {}); // The browser releases the lock on close or crash.
  });
}
$('new-task').addEventListener('click',showCatalogue);
$('back').addEventListener('click',showCatalogue);
$('task-search').addEventListener('input',renderCatalogue);
$('report-form').addEventListener('input',changed);
$('report-form').addEventListener('submit',ready);
$('keep-copy').addEventListener('click',keepCopy);
$('retry-save').addEventListener('click',() => { const item=activeReport(); if(item) { item._dirty=true; cache(); void persist(item.id); } });
window.addEventListener('online',() => { for(const item of model.reports.values()) if(item._dirty) void persist(item.id); });
document.addEventListener('visibilitychange',() => { if(document.visibilityState==='hidden') { cache(); for(const item of model.reports.values()) if(item._dirty) void persist(item.id); } });
void init();
