'use strict';
const fs=require('node:fs'),path=require('node:path'),{randomUUID}=require('node:crypto');
class AgentPlans {
 constructor({root,validate,audit=()=>{}}){
  this.path=path.join(root,'agent-plans.json');this.validate=validate;this.audit=audit;
 }
 all(){try{const v=JSON.parse(fs.readFileSync(this.path,'utf8'));if(!Array.isArray(v.plans))throw Error('Invalid plans');return v;}catch(e){if(e.code==='ENOENT')return {plans:[]};throw Error('Saved agent plans unreadable; refusing to overwrite');}}
 persist(value){const tmp=this.path+'.tmp';fs.writeFileSync(tmp,JSON.stringify(value,null,2));fs.renameSync(tmp,this.path);}
 async save(session,input){
  if(!session||session.isTerminated)throw Error('Preserved session required');
  if(session.kind!=='worker')throw Error('Primary orchestrator changes require a separate controlled handoff; worker settings can be staged here.');
  const selection=await this.validate(input);
  const saved=this.all(),prior=saved.plans.find(x=>x.id===session.id);
  if(prior&&['applying','unverified'].includes(prior.status))throw Error('An earlier application is unverified; inspect the roster before editing this plan.');
  const plan={id:session.id,planId:randomUUID(),...selection,status:'pending',updatedAt:new Date().toISOString()};
  saved.plans=saved.plans.filter(x=>x.id!==session.id);saved.plans.push(plan);this.persist(saved);
  this.audit('agent_plan_saved',{id:plan.id,harness:plan.harness,model:plan.model,effort:plan.effort,planId:plan.planId});
  return {ok:true,plan,note:'Settings saved only. No agent started and no worktree changed.'};
 }
 begin(id){
  const saved=this.all(),plan=saved.plans.find(x=>x.id===id);
  if(!plan||plan.status!=='pending')throw Error('No pending plan; do not retry an unverified application.');
  plan.status='applying';plan.startedAt=new Date().toISOString();this.persist(saved);return {...plan};
 }
 finish(id,planId,status,detail={}){
  const saved=this.all(),plan=saved.plans.find(x=>x.id===id&&x.planId===planId);
  if(!plan)throw Error('Agent plan changed while applying');
  Object.assign(plan,detail,{status,finishedAt:new Date().toISOString()});this.persist(saved);return plan;
 }
}
module.exports={AgentPlans};
