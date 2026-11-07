/* Launcher-only voice choices and explicit repair loop; existing Review remains shared. */
(() => {
  const $ = id => document.getElementById(id);
  window.addEventListener?.('pagehide',()=>{window.LauncherNavigating=true;});
  window.addEventListener?.('pageshow',()=>{window.LauncherNavigating=false;});
  let helpReady = false, voiceReady = false, renderBusy = false, repairBusy = false, uploadBusy = false, repairReady = false, reviewIncomplete = false, benchmarkBusy = false, renderJobPresent = false;
  let acceptedMode = $('render-mode').value, acceptedUltra = $('render-ultra-fast').checked, switchBusy = false, listSignature = '';
  const repairLogSeen = new Map();
  let voiceConsoleErrors = new Map(), voiceConsoleLive = [];
  let renderConsoleJob = 0;
  let renderConsoleLastStatus = null;
  let renderConsoleLines = new Map();
  let renderConsoleLive = [];
  let benchmarkConsoleLines = new Set(), benchmarkConsoleLive = [], benchmarkConsoleJob = 0;
  let flowRefreshInFlight = false, gameRefreshInFlight = false, helpRefreshInFlight = false;
  function reportError(source,error) {
    if(!error.launcherReported)window.LauncherConsole?.update(source,'Fehler: '+error.message,[],{status:'failed'});
  }
  function node(tag, text, attrs = {}) {
    const e = document.createElement(tag); if (text != null) e.textContent = text;
    Object.assign(e, attrs); return e;
  }
  function locks() {
    const busy = setupBusy || gameBusy || renderBusy || repairBusy || benchmarkBusy || window.LauncherRosettaBusy===true;

    $('benchmark-cancel').hidden=!benchmarkBusy;
    $('render-mode').disabled = setupBusy || gameBusy || repairBusy || benchmarkBusy || uploadBusy || switchBusy;
    const ultra = $('render-ultra-fast');
    ultra.disabled = setupBusy || gameBusy || repairBusy || benchmarkBusy || uploadBusy || switchBusy || !['fast','medium'].includes($('render-mode').value);
    if ($('render-mode').value === 'slow') ultra.checked = false;
    $('render-list').disabled = setupBusy || gameBusy || repairBusy || uploadBusy || switchBusy;
    $('repair-start').disabled = busy || uploadBusy || !repairReady;
    $('repair-confirm').disabled = busy || uploadBusy || !repairReady;
    window.FassungControls?.paint({canStart:voiceReady,blocked:setupBusy||gameBusy||repairBusy||uploadBusy||switchBusy||window.LauncherRosettaBusy===true});
    $('voices-demo').disabled = busy || uploadBusy;
    $('game').disabled = busy || uploadBusy || !helpReady || !window.LauncherConsents?.game || window.LauncherRosettaVerified!==true;
    $('wine').disabled = busy || !window.LauncherConsents?.wine;
    $('preparation-restart').disabled = busy || uploadBusy;
    for (const e of $('voice-roles').querySelectorAll('input,select,textarea,button')) e.disabled = busy || uploadBusy;
  }
  window.updateLauncherLocks = locks;
  let recipeApplied = false;
  window.LauncherRecipeDefaults = {apply(recipe) {
    const hint = $('render-recipe-hint');
    hint.hidden = !recipe;
    hint.textContent = recipe ? 'Rezept geladen · Verarbeitung ' + recipe.label + '. Die gespeicherten Vorgaben werden bei den zugehörigen Dialogen verwendet.' + (recipe.warnings?.length?'\n'+recipe.warnings.map(w=>w.startsWith('Warnung:')?w:'Warnung: '+w).join('\n'):'') : '';
    if (!recipe || recipeApplied || renderJobPresent || !['fast','medium','slow'].includes(recipe.mode)) return;
    recipeApplied = true;
    $('render-mode').value = recipe.mode;
    // Ultra Fast is a separate user choice. Only GPU cannot support it.
    if (recipe.mode === 'slow') $('render-ultra-fast').checked = false;
    acceptedMode = recipe.mode; acceptedUltra = $('render-ultra-fast').checked;
    locks();
  }};
  function modeChanged() { locks(); window.FassungControls?.modeChanged(); }
  $('render-mode').onchange = modeChanged;
  $('render-ultra-fast').onchange = modeChanged;
  async function get(path) {
    const r = await fetch(path, {headers}); const v = await r.json();
    if (!r.ok) throw Error(v.error || 'Status nicht verfügbar'); return v;
  }
  function paintVoices(s) {
    voiceReady = s.ready;
    window.LauncherConsole?.update('voices',(s.ready?'Alle Rollen gewählt':'Stimmwahl unvollständig')+' · Rollen '+s.roles.length);
    const errors=new Map(s.roles.filter(role=>role.error).map(role=>[role.role_id,role.role_id+' · '+role.error]));
    voiceConsoleLive.push(...[...errors].filter(([id,line])=>voiceConsoleErrors.get(id)!==line).map(([,line])=>line));
    voiceConsoleErrors=errors;voiceConsoleLive=voiceConsoleLive.slice(-80);
    window.LauncherConsole?.update('voices',null,voiceConsoleLive,'voices:errors');
    const list = $('voice-roles'); list.replaceChildren();
    for (const role of s.roles) {
      const item = node('details'); item.dataset.roleId = role.role_id;
      const choice = typeof role.choice === 'object' ? role.choice?.source : role.choice;
      item.append(node('summary', (role.name || role.role_id) + ' · ' + (choice === 'custom' ? 'eigene Stimme' : choice === 'original_demo' ? 'Originalstimme · Tech-Demo' : 'noch nicht gewählt')));
      const original = node('button', 'Originalstimme wählen · Tech-Demo', {type:'button'});
      original.onclick = async () => {
        try { paintVoices(await post('/api/voices/select', {role_id:role.role_id, source:'original_demo'})); }
        catch (e) { reportError('voices',e); }
      };
      item.append(original);
      const audio = node('input', null, {type:'file', accept:'.wav,.flac,.ogg'});
      const audioLabel = node('label', 'Eigene Sprechprobe '); audioLabel.append(audio);
      const language = node('select'); language.append(node('option','Deutsch',{value:'de'}),node('option','Englisch',{value:'en'}));
      const languageLabel = node('label','Sprache der Probe '); languageLabel.append(language);
      const transcript = node('textarea', null, {rows:3, placeholder:'Schreibe hier genau auf, was in deiner Probe gesprochen wird.', ariaLabel:'Transkript für '+(role.name||role.role_id)});
      const transcriptLabel = node('label','Gesprochener Text '); transcriptLabel.append(transcript);
      const confirmed = node('input',null,{type:'checkbox'});
      const confirmLabel = node('label'); confirmLabel.append(confirmed, document.createTextNode(' Die Probe enthält mindestens drei Sekunden gesprochene Sprache.'));
      const save = node('button','Als eigene Stimme verwenden',{type:'button'});
      const alternative = node('button','Diese Datei als zweite Probe hinzufügen',{type:'button'});
      alternative.hidden = choice !== 'custom';
      const note = node('p', role.choice?.alternative ? 'Eine zweite Aufnahme derselben Stimme steht für automatische Reparaturversuche bereit.' : 'Wenn einzelne Sätze mit dieser Probe nicht gut gelingen, kannst du nach dem Speichern eine zweite Aufnahme derselben Person für automatische Reparaturversuche hinzufügen.');
      async function upload(path) {
        try {
          const file = audio.files[0];
          if (!file) throw Error('Wähle zuerst eine Sprechprobe.');
          if (file.size > 20*1024*1024) throw Error('Die Sprechprobe darf höchstens 20 MiB groß sein.');
          if (!confirmed.checked) throw Error('Bestätige mindestens drei Sekunden gesprochene Sprache.');
          if (!transcript.value.trim()) throw Error('Trage den gesprochenen Text ein.');
          uploadBusy = true; locks(); window.LauncherConsole?.update('voices','Sprechprobe wird lokal geprüft und gespeichert …',[],{status:'running'});
          const encoded = await new Promise((resolve,reject) => { const reader=new FileReader(); reader.onload=()=>resolve(reader.result.split(',')[1]); reader.onerror=()=>reject(Error('Sprechprobe nicht lesbar')); reader.readAsDataURL(file); });
          paintVoices(await post(path,{role_id:role.role_id,source:'custom',filename:file.name,audio_base64:encoded,transcript:transcript.value,language:language.value,speech_confirmed:true}));
        } catch (e) { reportError('voices',e); }
        finally { uploadBusy=false; locks(); }
      }
      save.onclick=()=>upload('/api/voices/select'); alternative.onclick=()=>upload('/api/voices/alternative');
      item.append(audioLabel,languageLabel,transcriptLabel,confirmLabel,save,alternative,note); list.append(item);
    }
    locks();
  }
  $('voices-demo').onclick = async () => {
    try { paintVoices(await post('/api/voices/original-demo',{confirmed:true})); }
    catch(e) { reportError('voices',e); }
  };
  for (const [id,path] of [['render-pause','/api/render/pause']]) {
    $(id).onclick=async()=>{
      try { $(id).disabled=true; await post(path,{}); await refreshFlow(); }
      catch(e) { reportError('render',e); }
    };
  }
  $('benchmark-cancel').onclick=async()=>{
    try { await post('/api/benchmark/cancel',{}); await refreshBenchmark(); }
    catch(e){reportError('benchmark',e);}
  };
  async function refreshBenchmark(){
    try {
      const value=await get('/api/benchmark/status');
      const wasBusy=benchmarkBusy;
      benchmarkBusy=value.busy===true||['queued','waiting','pausing','preparing','warming','running','cancelling'].includes(value.status);
      if(benchmarkBusy&&!wasBusy){benchmarkConsoleJob++;benchmarkConsoleLines=new Set();benchmarkConsoleLive=[];}
      const phase={cached:'Gespeicherte Maschinenmessung',waiting_for_preparation:'Verfügbare Stimmreferenzen prüfen',waiting_for_pause:'Aktive Dialoge abschließen und pausieren',preparation:'Vergleich vorbereiten',warmup:'Modelle laden und aufwärmen',resource_admission:'RAM-Budget prüfen',generating:'Frische Vergleichsdialoge erzeugen',validation:'Audiodateien prüfen',report:'Vergleich abgeschlossen'};
      const names={gpu_single:'GPU',ane_single:'Eine ANE',ane_multi:'Mehrere ANE'};
      const status=(phase[value.phase]||value.phase||value.status||'Noch kein Benchmark vorgemerkt')+(value.variant?' · '+(names[value.variant]||value.variant):'')+(value.total?' · '+(value.completed||0)+' / '+value.total:'')+(value.error?' · '+value.error:'')+(value.solution?' · '+value.solution:'');
      const lines=(value.variants||[]).map(v=>(names[v.name]||v.name)+' · '+Number(v.makespan_seconds||0).toFixed(1)+' s · '+v.completed+' Dialoge · tatsächlich gleichzeitig '+v.actual_concurrency+' · Fehler '+v.errors);
      for(const probe of value.probes||[])lines.push('ANE-Stufe '+probe.workers+' · '+Number(probe.makespan_seconds||0).toFixed(1)+' s · tatsächlich gleichzeitig '+probe.actual_concurrency+(probe.speedup_vs_previous!=null?' · Faktor zur vorigen Stufe '+Number(probe.speedup_vs_previous).toFixed(2)+'×':''));
      if(value.selected_method)lines.push('Rendering: '+(names[value.selected_method]||value.selected_method)+(value.selected_workers?' · '+value.selected_workers+' Instanz(en)':'')+(value.selection_applied?' · übernommen':''));
      if(value.selection_reason)lines.push(value.selection_reason);
      if(value.stop_reason)lines.push(value.stop_reason);
      const ratios={ane_single_vs_gpu:'Eine ANE / GPU',ane_multi_vs_gpu:'Mehrere ANE / GPU',ane_multi_vs_ane_single:'Mehrere / eine ANE'};
      for(const [name,factor] of Object.entries(value.speedups||{}))lines.push((ratios[name]||name)+': '+Number(factor).toFixed(2)+'× Geschwindigkeit (unter 1 = langsamer)');
      if(value.ratio_reason)lines.push(value.ratio_reason);
      if(value.report)lines.push('Messbericht: '+value.report);
      const nextLines=new Set(lines);
      benchmarkConsoleLive.push(...[...nextLines].filter(line=>!benchmarkConsoleLines.has(line)));
      benchmarkConsoleLines=nextLines;benchmarkConsoleLive=benchmarkConsoleLive.slice(-80);
      window.LauncherConsole?.update('benchmark',status,benchmarkConsoleLive,'benchmark:results:'+benchmarkConsoleJob,{status:benchmarkBusy?'running':value.status==='failed'?'failed':'completed'});
      window.LauncherWizard?.update({renderBusy:renderBusy||benchmarkBusy});
      if(benchmarkBusy)$('render-pause').disabled=true;
      locks();
    } catch(e){reportError('benchmark',e);}
  }
  refreshBenchmark();setInterval(refreshBenchmark,1000);
  get('/api/startup/resume').then(value=>{
    if(value.resume_available===true){window.LauncherResumeVerified=true;window.LauncherWizard?.update({setupReady:true});window.LauncherWizard?.show('render');}
  }).catch(error=>{window.LauncherConsole?.startupCheckDone?.('resume',error);}).finally(()=>window.LauncherConsole?.startupCheckDone?.('resume'));
  $('repair-start').onclick = () => { $('repair-confirm-panel').hidden=false; };
  $('repair-cancel').onclick = () => { $('repair-confirm-panel').hidden=true; };
  $('repair-confirm').onclick = async () => {
    try { await post('/api/repair/start',{confirmed:true}); $('repair-confirm-panel').hidden=true; await refreshFlow(); }
    catch(e) { $('repair-status').textContent=e.message; window.LauncherConsole?.update('repair','Fehler: '+e.message); }
  };
  async function refreshFlow() {
    if (flowRefreshInFlight) return;
    flowRefreshInFlight = true;
    const startupCheck=!refreshFlow.startupChecked;refreshFlow.startupChecked=true;
    let checkError=null;
    try {
      const [render, repair] = await Promise.all([get('/api/render/status'),get('/api/repair/status')]);
      window.LauncherConsole?.update('host','Launcher erreichbar',[],{status:'completed'});
      const hasRenderJob=render.status!=='idle'&&render.status!=null;
      const terminalStatuses=['idle','completed','completed_with_errors','failed','interrupted'];
      if(hasRenderJob&&(renderConsoleJob===0||(!terminalStatuses.includes(render.status)&&terminalStatuses.includes(renderConsoleLastStatus)))){
        renderConsoleJob++;renderConsoleLines=new Map();renderConsoleLive=[];
      }
      renderConsoleLastStatus=render.status;
      renderJobPresent=hasRenderJob;
      window.FassungControls?.update(render,repair);
      renderBusy=['running','pausing','paused'].includes(render.status);
      const manual=Array.isArray(render.manual_review)?render.manual_review:[];
      reviewIncomplete=manual.length>0;
      window.LauncherWizard?.update({renderStarted:(render.render_lists||[]).some(item=>!!item.started_at||item.status!=='idle')||hasRenderJob,renderReady:render.selected_fassung?.status==='completed'&&render.selected_review_ready!==false,renderBusy:renderBusy||benchmarkBusy,repairBusy:!!repair.busy});
      $('render-pause').disabled=benchmarkBusy||render.status!=='running';
      repairBusy=!!repair.busy; repairReady=!reviewIncomplete&&repair.ready===true;
      if(!repairReady)$('repair-confirm-panel').hidden=true;
      $('repair-start').textContent=repair.pass_count || repair.round || repair.completed_passes ? 'Weiteren Reparaturversuch starten' : 'Automatischen Reparaturversuch starten';
      $('repair-status').textContent=(repair.phase||'bereit')+' · '+(repair.status||'noch nicht gestartet')+' · Dialoge '+(repair.completed||0)+' / '+(repair.total||0)+' · verbleibend '+(repair.remaining??Math.max(0,(repair.total||0)-(repair.completed||0)))+' · Warnungen '+(repair.warnings||0)+((repair.active_dialog_ids||[]).length?' · aktueller Dialog '+repair.active_dialog_ids.join(', '):repair.active_dialog_id?' · aktueller Dialog '+repair.active_dialog_id:'')+(repair.error?' · '+repair.error:'')+(!repairReady&&!repairBusy&&repair.ready_reason?' · '+repair.ready_reason:'');
      if(repair.active_backends?.length)$('repair-status').textContent+=' · Rendermodus '+repair.active_backends.join(' + ');
      $('repair-log').textContent=[...(repair.log||repair.messages||[]).map(x=>typeof x==='string'?x:x.text||JSON.stringify(x)),...(repair.decisions||[]).map(d=>d.dialog_id+': '+(d.phase||'bereit')+(d.skipped_anchor?' · '+d.skipped_anchor:'')+(d.reason?' · '+(typeof d.reason==='string'?d.reason:JSON.stringify(d.reason)):'')),...Object.entries(repair.errors||{}).map(([id,error])=>id+': '+error),...(repair.result?[repair.result]:[]),...(repair.job?.progress?.detail?[repair.job.progress.detail]:[])].join('\n');
      const repairChanges=$('repair-log').textContent.split('\n').filter(line=>{
        if(!line)return false;
        const split=line.indexOf(': '),key=split<0?line:line.slice(0,split);
        if(repairLogSeen.get(key)===line)return false;
        repairLogSeen.set(key,line);return true;
      });
      window.LauncherConsole?.update('repair',$('repair-status').textContent,repairChanges,{status:repair.status,busy:!!repair.busy,warning:(repair.warnings||0)>0});
      const renderWorkers=Object.entries(render.workers||{}).map(([name,w])=>[name,name+': '+w.dialog_id+' · '+w.role_id+' · '+w.text]);
      const qaWorkers=Object.entries(render.qa_workers||{}).map(([name,w])=>['QA '+name,'QA '+name+': '+w.dialog_id+' · '+w.role_id+' · '+w.text]);
      const workers=renderWorkers.concat(qaWorkers).map(([,line])=>line);
      if(render.adaptive_ane) {
        const a=render.adaptive_ane, t=a.telemetry||{};
        workers.unshift('ANE-Instanzen '+a.active_instances+' / '+a.maximum_instances+' · '+(a.instances||[]).join(', ')+(a.retiring_instances?.length?' · Auslaufend '+a.retiring_instances.join(', '):'')+' · Regler: '+a.reason+(a.next_expansion_in_seconds>0?' · Wartezeit '+a.next_expansion_in_seconds+' s':'')+' · Speicherdruck: '+(t.memory_pressure??'unbekannt')+' · Temperatur: '+(t.thermal_state??'unbekannt'));
      }
      const preparing=(render.phase||'').startsWith('preparation');
      const phases={manual_review:'Manuelle Review erforderlich',preparation:'Vorbereitung',preparation_sources:'Originalquellen vorbereiten',preparation_roles:'Rollen vorbereiten',preparation_prewarm:'Prewarm vorbereiten · GPU (auch im ANE-Modus)'};
      const preparation=preparing?' · '+(render.phase==='preparation_sources'?'Originalquellen':'Rollen')+' '+(render.preparation_completed??0)+' / '+(render.preparation_total??'noch unbekannt')+(render.role_name||render.role_id?' · '+(render.role_name||render.role_id):'')+(render.preparation_attempt!=null?' · Versuch '+render.preparation_attempt:'')+(render.preparation_duration_ms!=null?' · '+render.preparation_duration_ms+' ms':'')+(render.preparation_detail?' · '+render.preparation_detail:''):'';
      const status=(phases[render.phase]||render.phase||'bereit')+' · '+({pausing:'Hält nach aktuellem Schritt an',paused:'Angehalten · mit Rendering starten weiterarbeiten',running:'Läuft',completed_with_errors:'Übrige Dialoge verarbeitet'}[render.status]||render.status)+preparation+' · Dialoge '+render.completed+' / '+render.total+' · verbleibend '+render.remaining+' · Warnungen '+render.warnings+' · QA-Rückstand '+render.qa_backlog+(render.error?' · '+render.error:'')+(manual.length?' · übersprungen '+manual.length+' · für manuelle Review gespeichert':'');
      window.LauncherConsole?.update('render',status,[],{status:render.status,warning:(render.warnings||0)>0||manual.length>0,error:!!render.error});
      const prepStream='render:preparation:'+renderConsoleJob;
      window.LauncherConsole?.update('render',null,render.preparation_messages||[],prepStream);
      if(preparing&&render.preparation_log)window.LauncherConsole?.update('render',null,render.preparation_log.split('\n'),'render:preparation-log:'+renderConsoleJob+':'+(render.preparation_log_path||''));
      const snapshots=new Map(renderWorkers.concat(qaWorkers).map(([key,line])=>['worker:'+key,line]));
      if(render.adaptive_ane)snapshots.set('adaptive_ane',workers[0]);
      manual.forEach(row=>{const line='Manuelle Review · '+row.dialog_id+' · '+(row.stage||'Rendering')+' · Fehler: '+(row.error||row.error_type||'übersprungen');snapshots.set('manual:'+row.dialog_id,line);});
      if(render.restart_required===true)snapshots.set('restart','KI oder Whisper benötigt einen Neustart. Beende den Patch, öffne ihn erneut mit demselben Arbeitsordner und starte dieselbe Fassung erneut. Gesicherte Ergebnisse bleiben erhalten.');
      const changed=[];
      for(const [key,line] of snapshots)if(renderConsoleLines.get(key)!==line)changed.push(line);
      renderConsoleLines=snapshots;
      renderConsoleLive.push(...changed);
      renderConsoleLive=renderConsoleLive.slice(-80);
      window.LauncherConsole?.update('render',null,renderConsoleLive,'render:live:'+renderConsoleJob);
    } catch(e) {
      if(!window.LauncherNavigating&&e.name!=='AbortError'){
        checkError=e;
        $('repair-status').textContent=e.message;window.LauncherConsole?.update('host','Launcherstatus nicht verfügbar: '+e.message,[],{status:'failed'});
      }
    }finally{
      flowRefreshInFlight = false;
      if(startupCheck)window.LauncherConsole?.startupCheckDone?.('render',checkError);
    }
    locks();
  }
  async function refreshPreparationHelp() {
    if(helpRefreshInFlight)return;helpRefreshInFlight=true;
    try {
      const help = await get('/api/preparation/help');
      const steps = $('preparation-help-steps'); steps.replaceChildren();
      for (const step of help.steps) {
        steps.append(node('h4', step.title));
        for (const instruction of step.instructions) steps.append(node('p', instruction));
      }
      const target=$('patch-target-path'),path=help.paths.patch_target_wine||'Windows-Zielordner nicht verfügbar. Prüfe die Wine-Laufwerkszuordnung.';
      if(target.value!==path)target.value=path;
      window.LauncherPatchTargetResize?.();
      helpReady=true; locks();
      window.LauncherConsole?.update('preparation-help','Bedienhinweise geladen.',[],{status:'ready'});
    } catch(e) { helpReady=false; locks(); reportError('preparation-help',e); }
    finally{helpRefreshInFlight=false;}
  }
  window.refreshPreparationHelp = refreshPreparationHelp;
  refreshPreparationHelp();
  get('/api/voices/status').then(paintVoices).catch(e=>{reportError('voices',e);locks();});
  async function refreshGameProgress() {
    if(gameRefreshInFlight)return;gameRefreshInFlight=true;
    try {
      const s=await get('/api/preparation/game-status');
      gameBusy=s.status==='running';
      window.LauncherWizard?.update({gameReady:s.status==='completed'||s.prepared===true,gameBusy,
        gameInterrupted:s.interrupted===true});
      window.LauncherPatchWindow?.sync(s);
      locks();
      const phases={'idle':'Noch nicht gestartet','preparing':'Spieldaten vorbereiten','extractor':'Extraktionswerkzeug prüfen','hashing-source':'GOG-Installer prüfen','inspecting':'GOG-Installer erkennen','listing':'Benötigte Dateien ermitteln','extracting':'Monkey1.pak und audio extrahieren','verifying-checksums':'Extrahierte Dateien prüfen','verifying-reuse':'Vorhandene Extraktion prüfen','native_sox':'Native Audiowerkzeuge einrichten','native_flac':'Audiodateien nativ zu FLAC konvertieren','speech_archive':'Spracharchiv bauen','builder_finish':'UTE-Musik und Abschluss · Wine-Konsole bedienen','german_patch':'Deutscher Patch · Originaldialog bedienen','completed':'Vorbereitung abgeschlossen','failed':'Vorbereitung fehlgeschlagen'};
      const status=(phases[s.phase]||s.phase||'bereit')+' · '+(s.status||'idle')+(s.file_count!=null?' · Dateien '+s.file_count+(s.expected_file_count?' / '+s.expected_file_count:''):'')+(s.jobs!=null?' · parallele Prozesse '+s.jobs:'')+(s.error?' · '+s.error:'')+(s.archive_path?' · Frühere Vorbereitung gesichert: '+s.archive_path:'')+(s.removed_directories_count!=null?' · Alte Vorbereitungskopien gelöscht: '+s.removed_directories_count:'')+(s.log_path?' · Host-/Wine-Log '+s.log_path:'');
      window.LauncherConsole?.update('game',status,s.log?s.log.split('\n'):[],'game:log:'+(s.stage_id||'game'),{status:s.status});
      const preserve = s.preserve_previous === true;
      $('preparation-restart-description').textContent = preserve
        ? 'Der Patch sichert die bisherige Spielvorbereitung in einem separaten Ordner. Danach kannst du die Spielbasis erneut vorbereiten.'
        : 'Der Patch entfernt die bisherigen Vorbereitungskopien. Danach kannst du die Spielbasis erneut vorbereiten.';
      $('preparation-restart').textContent = preserve
        ? 'Vorbereitung sichern und neu beginnen'
        : 'Vorbereitung löschen und neu beginnen';
      $('preparation-restart-panel').hidden = !(preserve || (s.status==='failed' && (s.restart_available || (s.error||'').includes('Vorbereitungsstufe'))));
    } catch(e) { reportError('game',e); }
    finally{gameRefreshInFlight=false;}
  }
  $('preparation-restart').onclick = async () => {
    try {
      gameBusy=true; locks();
      await post('/api/preparation/restart',{confirmed:true});
      await refreshGameProgress(); await refreshPreparationHelp();
    } catch(e) { reportError('game',e); }
    finally { gameBusy=false; locks(); }
  };
  refreshGameProgress();setInterval(refreshGameProgress,1000);
  window.refreshLauncherFlow=refreshFlow;
  refreshFlow();setInterval(refreshFlow,1000);
})();
