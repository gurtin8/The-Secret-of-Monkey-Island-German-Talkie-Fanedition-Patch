/* Local-only UI. Native code owns paths, confirmations, downloads and save panels. */
(() => {
  const $ = id => document.getElementById(id);
  const keys = ['packages','wine','game'];
  let state = {ready:false,busy:false,log:[]};
  function bridge(body) { window.webkit?.messageHandlers?.launcher?.postMessage(body); }
  function confirmed() { return keys.every(key => $(key+'-consent').checked); }
  function controls() {
    $('to-consents').disabled = !state.ready || state.busy;
    $('to-downloads').disabled = !confirmed() || state.busy;
    $('start-downloads').disabled = !state.ready || !confirmed() || state.busy;
    document.querySelectorAll('[data-pick],[data-back],#clear-correction').forEach(el => {el.disabled=state.busy;});
    keys.forEach(key => { $(key+'-consent').disabled = state.busy; });
  }
  function show(next) {
    ['paths','consents','downloads'].forEach((id,i) => {$(id).hidden=i!==next;});
    document.querySelectorAll('[data-step]').forEach((el,i) => {el.classList.toggle('active',i===next);if(i===next)el.setAttribute('aria-current','step');else el.removeAttribute('aria-current');});
    document.querySelectorAll('main section')[next].querySelector('h1').focus();
    window.scrollTo(0,0); controls();
  }
  function update(next) {
    const correctionChanged = next.correction != null && next.correction !== state.correction;
    state = {...state,...next};
    [['root','Kein Arbeitsordner gewählt'],['game','Keine Spielquelle gewählt'],['manifest','Dialogvorgaben fehlen'],['correction','Kein Rezept gewählt']].forEach(([field,empty]) => {
      $(field+'-path').textContent = state[field] || empty;
      const button=document.querySelector('[data-pick="'+field+'"]');button.textContent=field==='correction'?'Rezept laden …':state[field]?'Ändern …':'Auswählen …';
    });
    $('correction-pipeline').hidden=!state.correctionPipeline && !state.correctionPipelineError;
    $('correction-pipeline').textContent=state.correctionPipelineError || (state.correctionPipeline?'Rezept geladen. Für die neuen Aufnahmen übernimmt der Patch den gespeicherten Rendermodus ('+state.correctionPipeline+').':'') + (state.correctionWarnings?'\n'+state.correctionWarnings:'');
    $('data-root').textContent='Wähle später denselben Arbeitsordner, um deine Arbeit fortzusetzen.';
    $('download-root').textContent=state.root||state.dataRoot||'';
    $('game-kind').textContent=(state.game?state.gameKind+' · ':'')+'Deine Spielkopie wird im nächsten Schritt vorbereitet.';
    $('path-help').hidden=state.ready;
    $('bootstrap-size').textContent=state.bootstrap||'Die Größe wird ermittelt …';
    $('current-status').textContent=state.status||'Bereit';
    $('status-ring').className='status-ring '+(state.statusKind||'ready');
    const log=$('start-log'), atEnd=log.scrollHeight-log.scrollTop-log.clientHeight<50;
    const text=(state.log||[]).join('\n')||'MI German Talkie Patch gestartet.\nNoch keine Downloads gestartet.';
    if(window.LauncherTerminal)window.LauncherTerminal.paint(log,text);else log.textContent=text;
    if(atEnd)log.scrollTop=log.scrollHeight;
    if(state.terms && !$('package-terms').childElementCount) state.terms.forEach(term => {
      const row=document.createElement('div'), a=document.createElement('a');a.textContent=term.name;a.href=term.url;a.target='_blank';a.rel='noopener';row.append(a);$('package-terms').append(row);
    });
    controls();
    if(correctionChanged && state.correctionPipeline) $('correction-pipeline').scrollIntoView?.({block:'nearest'});
  }
  window.NativeStart = {update};
  document.querySelectorAll('[data-pick]').forEach(el=>{el.onclick=()=>bridge({action:'pick',field:el.dataset.pick});});
  document.querySelectorAll('[data-back]').forEach(el=>{el.onclick=()=>{if(!state.busy)show(Number(el.dataset.back));};});
  keys.forEach(key=>{$(key+'-consent').onchange=controls;});
  $('clear-correction').onclick=()=>bridge({action:'clearCorrection'});
  $('to-consents').onclick=()=>{if(state.ready&&!state.busy)show(1);};
  $('to-downloads').onclick=()=>{if(confirmed()&&!state.busy)show(2);};
  $('start-downloads').onclick=()=>{
    if(!state.ready||state.busy||!confirmed())return;
    state.busy=true;$('start-log').hidden=false;$('toggle-log').textContent='Protokoll ausblenden';$('toggle-log').setAttribute('aria-expanded','true');controls();
    bridge({action:'start',consents:Object.fromEntries(keys.map(key=>[key,$(key+'-consent').checked]))});
  };
  $('toggle-log').onclick=()=>{const log=$('start-log');log.hidden=!log.hidden;$('toggle-log').textContent=log.hidden?'Protokoll anzeigen':'Protokoll ausblenden';$('toggle-log').setAttribute('aria-expanded',String(!log.hidden));};
  $('export-log').onclick=()=>bridge({action:'exportLog',text:'MI German Talkie Patch '+(state.version||'')+' · Patch-Protokoll\nExport: '+new Date().toISOString()+'\n\n'+$('start-log').textContent+'\n'});
  bridge({action:'ready'});
})();
