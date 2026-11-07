/* One bounded session history shared by the Wizard and existing Review. */
(() => {
  const key='mi1-launcher-console', token=new URL(location.href).searchParams.get('session');
  const versionReady=fetch('/api/server/status',{headers:token?{'X-Launcher-Session':token}:{}})
    .then(async response=>{if(!response.ok)throw Error('Versionsstatus nicht verfügbar');return (await response.json()).launcher_version||'unbekannt';})
    .catch(()=>'unbekannt');
  let saved={};try{saved=JSON.parse(sessionStorage.getItem(key)||'{}');}catch{}
  const history=Array.isArray(saved.history)?saved.history.slice(-400).map(line=>String(line).slice(0,2000)):[],current=saved.current||{},tails=saved.tails||{},states=saved.states||{};
  let importedStartup=!!saved.importedStartup, latest=saved.latest||'setup', focused='setup';
  const startupChecks=new Set(document.getElementById('render-page')&&document.getElementById('setup-packages')?['packages','render','resume']:[]),startupErrors=[];
  delete states.startup;delete current.startup;
  const alertDuration=10000,ringStates=new WeakMap();
  let alertTimer=null;
  const labels={startup:'Startprüfung',setup:'Einrichtung',game:'Patch-Vorbereitung',rosetta:'Rosetta','preparation-help':'Bedienhinweise','patch-window':'Patchfenster',voices:'Stimmen',render:'Sprachausgabe / Prüfung',repair:'Reparatur',review:'Review','review-save':'Review speichern',export:'Spielordner',host:'Patch',benchmark:'Geschwindigkeitsvergleich'};
  const phases={native_sox:'Native Audiowerkzeuge einrichten',native_flac:'Audiodateien nativ zu FLAC konvertieren',speech_archive:'Spracharchiv bauen',builder_finish:'UTE-Musik und Abschluss · Wine-Konsole bedienen',german_patch:'Deutscher Patch · Originaldialog bedienen',completed:'Abgeschlossen',failed:'Fehlgeschlagen',idle:'Bereit',validating_evidence:'Audiobelege prüfen'};
  let section=document.getElementById('launcher-console');
  if(!section){section=document.createElement('section');section.id='launcher-console';section.className='launcher-console';document.body.append(section);}
  let log=document.getElementById('setup-log');if(!log){log=document.createElement('pre');log.id='setup-log';log.tabIndex=0;log.setAttribute('aria-label','Zentrales Patch-Protokoll');}
  section.replaceChildren();
  const heading=document.createElement('div');heading.className='console-heading';
  const title=document.createElement('h2');title.textContent='Konsole';
  const exportButton=document.createElement('button');exportButton.type='button';exportButton.textContent='Protokoll exportieren';exportButton.id='launcher-log-export';
  heading.append(title,exportButton);
  const active=document.createElement('p');active.id='launcher-console-current';active.setAttribute('role','status');
  const ring=document.createElement('span');ring.className='status-ring';ring.id='launcher-status-ring';ring.setAttribute('aria-hidden','true');
  const currentText=document.createElement('span');currentText.id='launcher-console-current-text';active.append(ring,currentText);
  const details=document.createElement('details');details.id='launcher-console-details';details.open=true;
  const summary=document.createElement('summary');summary.textContent='Protokoll ausblenden';details.addEventListener('toggle',()=>{summary.textContent=details.open?'Protokoll ausblenden':'Protokoll anzeigen';});details.append(summary,log);section.append(heading,active,details);
  function severity(value){
    const status=String(value?.status||'').toLowerCase();
    const terminal=['failed','error','crashed','interrupted','unfinished','unexpected_exit','completed_with_errors'].includes(status);
    const active=!terminal&&(['running','pausing','building','downloading','preparing'].includes(status)||value?.busy===true);
    return {state:terminal||value?.error?'error':value?.warning===true?'warning':'ready',active,terminal};
  }
  function timedState(next,prior,alertKey){
    const temporary=next.state!=='ready'&&!next.active;
    return {...next,alertKey,alertUntil:temporary?(prior?.alertKey===alertKey&&Number.isFinite(prior.alertUntil)?prior.alertUntil:Date.now()+alertDuration):null};
  }
  function visibleState(value){
    return !value.active&&Number.isFinite(value.alertUntil)&&Date.now()>=value.alertUntil?{...value,state:'ready',terminal:false}:value;
  }
  for(const [source,savedState] of Object.entries(states)){
    const value=typeof savedState==='string'?{state:savedState==='running'?'ready':savedState,active:savedState==='running',terminal:savedState==='error'}:savedState;
    states[source]=Number.isFinite(value?.alertUntil)||value?.active||value?.state==='ready'?value:timedState(value,null,source);
  }
  function paintRing(target, value) {
    if(!target)return;
    const prior=ringStates.get(target),next=severity(value);
    const alertKey=JSON.stringify([value?.status,next.state,next.active,value?.error,value?.warning]);
    const record=timedState(next,prior,alertKey);
    if(prior?.timer)clearTimeout(prior.timer);
    const state=visibleState(record);
    target.dataset.state=state.state;target.dataset.active=String(state.active);
    if(record.alertUntil>Date.now())record.timer=setTimeout(()=>{
      if(ringStates.get(target)!==record||target.isConnected===false)return;
      const expired=visibleState(record);target.dataset.state=expired.state;target.dataset.active=String(expired.active);
    },record.alertUntil-Date.now());
    ringStates.set(target,record);
  }
  function paint(){
    const entries=Object.entries(states).map(([source,value])=>[source,visibleState(value)]);
    const selected=entries.find(([source,v])=>source==='startup'&&v.active)||entries.find(([,v])=>v.terminal)||entries.find(([,v])=>v.state==='error')||entries.find(([,v])=>v.active&&v.state==='warning')||entries.find(([,v])=>v.active)||entries.find(([,v])=>v.state==='warning');
    const source=selected?.[0]||(current[focused]?focused:latest),value=selected?.[1]||{state:'ready',active:false};
    ring.dataset.state=value.state;ring.dataset.active=String(entries.some(([,v])=>v.active));
    currentText.textContent=current[source]?'['+(labels[source]||source)+'] '+current[source]:'Bereit · Status wird geladen …';
    const nearEnd=log.scrollHeight-log.scrollTop-log.clientHeight<60;
    const text=history.join('\n');
    if(text!==log.textContent){window.LauncherTerminal.paint(log,text);if(nearEnd)log.scrollTop=log.scrollHeight;}
    try{sessionStorage.setItem(key,JSON.stringify({history,current,tails,states,latest,importedStartup}));}catch{}
    clearTimeout(alertTimer);
    const deadlines=Object.values(states).map(value=>value.alertUntil).filter(until=>Number.isFinite(until)&&until>Date.now());
    alertTimer=deadlines.length?setTimeout(paint,Math.min(...deadlines)-Date.now()):null;
  }
  function append(source,text){if(!text)return;history.push(new Date().toLocaleTimeString('de-DE')+' ['+(labels[source]||source)+'] '+String(text).slice(0,2000));if(history.length>400)history.splice(0,history.length-400);}
  function update(source,text,lines=[],stream=source,state=null){
    if(typeof stream==='object'){state=stream;stream=source;}
    if(state){const next=severity(state),prior=states[source];if(source==='setup'&&((next.active&&!prior?.active)||next.terminal))details.open=true;states[source]=timedState(next,prior,JSON.stringify([state.status,next.state,next.active,text||current[source]||'']));}
    if(text&&current[source]!==text){current[source]=text;latest=source;append(source,text);}
    const next=lines.map(v=>String(typeof v==='object'?v.text||JSON.stringify(v):v)).filter(Boolean).slice(-400),prior=tails[stream]||[];
    let overlap=Math.min(prior.length,next.length);while(overlap&&!prior.slice(-overlap).every((v,i)=>v===next[i]))overlap--;
    next.slice(overlap).forEach(line=>append(source,line));if(next.length){delete tails[stream];tails[stream]=next;}
    Object.keys(tails).slice(0,Math.max(0,Object.keys(tails).length-20)).forEach(k=>delete tails[k]);paint();
  }
  function startupCheckDone(name,error=null){
    if(!startupChecks.delete(name))return;
    if(error)startupErrors.push(String(error.message||error));
    const labels={packages:'Pakete',render:'Renderstand',resume:'Wiederaufnahme'};
    const detail=startupChecks.size?'Noch prüfen: '+[...startupChecks].map(key=>labels[key]).join(', '):startupErrors.length?'Startprüfung nicht vollständig: '+startupErrors.join(' · '):'Pakete und Renderstand geprüft.';
    update('startup',detail,[],{status:startupChecks.size?'running':startupErrors.length?'failed':'completed',error:startupErrors.length>0});
  }
  exportButton.onclick=async()=>{
    const version=await versionReady;
    const text='MI German Talkie Patch · Patch-Protokoll\nVersion: '+version+'\nExportiert: '+new Date().toISOString()+'\n\n'+log.textContent+'\n';
    const bridge=window.webkit?.messageHandlers?.launcher;
    if(bridge){bridge.postMessage({action:'exportLog',text});return;}
    const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='MI German Talkie Patch - Protokoll.txt';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  };
  window.LauncherConsole={update,paintRing,startupCheckDone,focus(source){focused=source;paint();}};paint();
  if(startupChecks.size)update('startup','Pakete und Renderstand prüfen …',[],{status:'running'});
  fetch('/api/launcher/consents',{headers:token?{'X-Launcher-Session':token}:{}}).then(r=>r.json()).then(v=>{if(!importedStartup&&Array.isArray(v.startup_log)){v.startup_log.forEach(line=>append('setup',line));importedStartup=true;paint();}}).catch(()=>{});
  if(location.pathname.startsWith('/patch-review/')){
    window.LauncherReviewLoading=true;
    focused='review';update('review','Lade Dialoge …',[],{status:'running'});
    let reviewRefreshInFlight=false;
    async function refresh(){
      if(window.LauncherReviewLoading||reviewRefreshInFlight)return;
      reviewRefreshInFlight=true;
      try{const r=await fetch('/api/patch-review/status',{headers:token?{'X-Launcher-Session':token}:{}}),status=await r.json();if(!r.ok)throw Error(status.error||'Exportstatus nicht verfügbar');const s=status.patch_build||{},j=s.job||{};update('host','Patch erreichbar · Reviewstatus geladen',[],{status:'completed'});update('export',(phases[j.phase]||j.phase||'Bereit')+' · '+(j.status||'idle')+' · Dialoge '+(j.completed??0)+' / '+(j.total??s.total_dialogues??0)+(j.error?' · '+j.error:'')+(j.output_dir?' · Ziel '+j.output_dir:''),[],'export:'+j.id,{status:j.status||'idle',warning:!!j.warnings,error:!!j.error});}catch(e){update('export',e.message,[],{status:'failed'});}
      finally{reviewRefreshInFlight=false;}
    }
    refresh();setInterval(refresh,1000);
  }
})();
