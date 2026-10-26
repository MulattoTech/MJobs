(function(){
 'use strict';
 function start(doc,getSnapshot,control){
  let selected=null,lastRender=null;
  const dialog=doc.createElement('dialog');dialog.id='agent-details';dialog.setAttribute('aria-labelledby','agent-detail-title');
  const head=doc.createElement('div');head.className='detail-head';
  const title=doc.createElement('h2');title.id='agent-detail-title';
  const close=doc.createElement('button');close.type='button';close.textContent='Close';close.addEventListener('click',()=>dialog.close());
  head.append(title,close);const body=doc.createElement('div');body.id='agent-detail-body';dialog.append(head,body);doc.body.append(dialog);
  const el=(tag,text,cls)=>{const x=doc.createElement(tag);if(text!=null)x.textContent=text;if(cls)x.className=cls;return x;};
  const time=v=>v&&Number.isFinite(Date.parse(v))?new Date(v).toLocaleString():'Not reported';
  function render(){
   if(!selected)return;
   const snapshot=getSnapshot(),age=Date.now()-Date.parse(snapshot?.generatedAt||''),stale=!Number.isFinite(age)||age>45000||age < -10000;
   const agent=snapshot?.fleet?.sessions?.find(s=>s.id===selected);
   const proofAge=Date.now()-Date.parse(agent?.runtimeObservedAt||'');const proofExpired=agent?.runtimeVerified===true&&(!Number.isFinite(proofAge)||proofAge>45000||proofAge < -10000);
   const signature=String(snapshot?.generatedAt)+':'+selected+':'+stale+':'+proofExpired;if(signature===lastRender)return;lastRender=signature;
   title.textContent=agent?.name||selected;body.replaceChildren();
   if(stale||!agent){body.append(el('p',stale?'Live data is stale. Current runtime and task details are unavailable.':'This agent is no longer in the current roster.','notice warn'));return;}
   body.append(el('p',agent.id+' · '+(agent.role||'Role not reported'),'secondary'));
   const facts=el('dl',null,'detail-facts');
   const rows=[['Runtime',proofExpired?'Verification unavailable':agent.runtimeStatus||'Not reported'],['Controller type',agent.controlKind||'Not reported'],['Activity',agent.stateLabel||agent.activity||'Not reported'],['Provider / model',[agent.harness,agent.model].filter(Boolean).join(' / ')],['Branch',agent.branch||'Not reported'],['Created',time(agent.createdAt)],['Last activity',time(agent.lastActivity)],['Last runtime inspection',time(agent.runtimeObservedAt)],['Last dispatched instruction',time(agent.dispatchObservation?.lastSent)],['Last observed completion',time(agent.dispatchObservation?.completedAt)]];
   for(const [label,value] of rows)facts.append(el('dt',label),el('dd',value));body.append(facts);
   body.append(el('p',agent.mode==='tui'?'This is a terminal-backed agent, not a Chat controller. Runtime verification and whether it is doing work are separate measurements.':'Chat controller status is reported separately from the current agent turn.','caption'));
   body.append(el('h3','Assigned issues and file scope'));
   if(!agent.assignments?.length)body.append(el('p','No dispatcher-owned issue is recorded. The agent may have a direct primary assignment; no task is inferred.'));
   for(const assignment of agent.assignments||[]){const item=el('div',null,'detail-item');const link=el('a','#'+assignment.issue);link.href='https://github.com/MulattoTech/MJobs/issues/'+assignment.issue;link.target='_blank';link.rel='noopener noreferrer';item.append(link,el('span',' · '+(assignment.state||'recorded')));for(const file of assignment.scope||[])item.append(el('code',file));body.append(item);}
   body.append(el('h3','Tracked pull requests'));
   if(!agent.prs?.length)body.append(el('p','No pull request is currently tracked for this session.'));
   for(const pr of agent.prs||[]){const item=el('div',null,'detail-item');const link=el('a','PR #'+pr.number);link.href='https://github.com/MulattoTech/MJobs/pull/'+pr.number;link.target='_blank';link.rel='noopener noreferrer';item.append(link,el('p',[pr.state,'CI: '+(pr.ci||'not reported'),'Review: '+(pr.review||'not reported')].join(' · ')));body.append(item);}
   body.append(el('h3','Recent dispatcher activity'));
   if(!agent.recentActions?.length)body.append(el('p','No recent dispatcher action is recorded.'));
   for(const action of agent.recentActions||[])body.append(el('p',time(action.at)+' · '+action.type+' · '+action.status+(action.issue?' · #'+action.issue:''),'detail-event'));
   if(agent.dispatchObservation?.noProgressCount)body.append(el('p',agent.dispatchObservation.noProgressCount+' completed turn(s) produced no new dispatcher-observed progress. Unchanged work is not automatically repeated.','notice warn'));
   const ask=el('button','Request fresh status');ask.type='button';ask.disabled=!control?.available?.()||!agent.idle;ask.title='Sends one explicit status request and uses model quota; never automatically repeated.';
   ask.addEventListener('click',async()=>{ask.disabled=true;try{const result=await control.request('request-status',{id:agent.id});body.append(el('p',result.note||'Status request submitted. This confirms delivery, not completion.','notice good'));}catch(e){body.append(el('p',e.message,'notice warn'));}});
   if(control?.available?.())body.append(ask);else body.append(el('p','To ask this agent a fresh question, open its Manage panel in local Fleet Control.','caption'));
   body.append(el('p','These details contain operational metadata, not private prompts or raw terminal logs.','caption'));
  }
  doc.addEventListener('click',event=>{const target=event.target.closest('[data-agent-details]');if(!target)return;selected=target.dataset.agentDetails;render();if(!dialog.open)dialog.showModal();});
  dialog.addEventListener('close',()=>{const previous=selected;selected=null;lastRender=null;const button=Array.from(doc.querySelectorAll('[data-agent-details]')).find(x=>x.dataset.agentDetails===previous);button?.focus();});
  return {refresh:()=>{if(dialog.open)render();},open:id=>{selected=id;render();dialog.showModal();}};
 }
 if(typeof module==='object'&&module.exports)module.exports={start};
 else globalThis.FleetAgentDetails={start};
})();
