(function(){
 'use strict';
 // No command proxy is installed. Do not make disabled remote controls look live.
 function start(doc){
  const panel=doc.getElementById('remote-controls');if(panel){
   const heading=doc.createElement('h2');heading.textContent='Fleet controls';
   const note=doc.createElement('p');note.className='control-note';note.textContent='Remote command controls are not enabled. This page provides live status and clickable agent details. Use Fleet Control on the workstation for pause/resume, status requests, task delegation and model changes.';
   const home=doc.createElement('a');home.href='/';home.textContent='Open the service hub for remote desktop access';home.className='control-note';
   panel.replaceChildren(heading,note,home);
  }
  return {available:()=>false,refresh:()=>{},request:async()=>{throw Error('Remote commands are not enabled. Use the local Fleet Control dashboard.');}};
 }
 if(typeof module==='object'&&module.exports)module.exports={start};else globalThis.FleetRemoteControls={start};
})();
