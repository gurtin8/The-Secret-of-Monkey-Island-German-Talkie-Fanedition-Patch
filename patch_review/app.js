'use strict';
(() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const text = value => value == null || value === '' ? '—' : String(value);
  const categoryNames = {one_word_wrong:'Ein Wort falsch',most_words_wrong:'Die meisten Wörter falsch',single_word_error:'Einwortdialog fehlerhaft',missing_word:'Wort fehlt',extra_word:'Wort zu viel',no_words:'Kein Wort erkannt'};
  const seedNames = {fresh:'Vorschau mit neuem Seed',keep:'Seed beibehalten'};
  const sttNames = {completed:'STT abgeschlossen',absent:'STT fehlt',failed:'STT fehlgeschlagen',deferred:'STT zurückgestellt',not_required:'STT nicht erforderlich',invalid:'STT-Beleg ungültig'};
  const sttStageStatus = {completed:'abgeschlossen',passed:'bestanden',pass:'bestanden',failed:'fehlgeschlagen',absent:'fehlt',deferred:'zurückgestellt',invalid:'ungültig',not_run:'nicht ausgeführt',skipped:'übersprungen'};
  const outputLabel = output => output?.verified ? 'Beleg geprüft' : 'Beleg nicht vollständig geprüft';
  const reviewLabel = output => output?.good_confirmed ? 'Gut bestätigt' : output?.review_reason === 'named_date_equivalence' ? 'Review unauffällig · Datum äquivalent' : output?.review_reason === 'integer_number_equivalence' ? 'Review unauffällig · Zahlenschreibweise äquivalent' : ({pass:'Review unauffällig',warn:'Reviewwarnung',fail:'Reviewfehler'}[output?.review_status] || output?.review_status || output?.qa_status || 'QA-Status unbekannt');
  let exportBusy = false, completionLoading = false;
  let buildGameRoot = null, buildGameRootEdited = false, completionView = false, auditionContext = null;
  let dismissedBuild = null;
  let buildOutputRoot = null, buildOutputRootEdited = false, exportChoice = false, buildTargetError = '', targetRequest = 0, directoryRequest = 0;
  const directoryRequests = new Map();
  const readOnly = () => state?.read_only === true;
  const nativeBridge = () => window.webkit?.messageHandlers?.launcher;
  const control = selector => root?.querySelector(selector) || document.querySelector(selector);
  const expandedRowSections = new Set();
  const rowSaveCounts = new Map(), rowSaveErrors = new Map(), rowSaveTouched = new Set(), savedInputValues = new Map();
  function editorValueSaved(id, key, input, stored) {
    const ack=savedInputValues.get(String(id))?.[key];
    return sameUpdateValue(key,input,stored)||Boolean(ack&&ack.input===input&&ack.stored===stored);
  }
  function rowSaveStatus(id) {
    id=String(id);
    if(rowSaveErrors.has(id))return {phase:'error',label:`Speicherfehler: ${rowSaveErrors.get(id)} · Bitte erneut bearbeiten.`};
    if(pendingUpdates.has(id))return {phase:'pending',label:'Wird gespeichert …'};
    if(rowSaveCounts.get(id))return {phase:'saving',label:'Wird gespeichert …'};
    return {phase:rowSaveTouched.has(id)?'saved':'idle',label:rowSaveTouched.has(id)?'Gespeichert':''};
  }
  function rowSaveMarkup(id) {
    const status=rowSaveStatus(id);
    return `<span class="pr-save-status" role="status" aria-live="polite" data-save-status="${status.phase}">${esc(status.label)}</span>`;
  }
  function paintRowSaveStatus(id) {
    const node=root?.querySelector(`[data-row="${CSS.escape(String(id))}"] [data-save-status]`),status=rowSaveStatus(id);
    if(node){node.dataset.saveStatus=status.phase;node.textContent=status.label;}
  }
  window.addEventListener('beforeunload',event=>{
    if(pendingUpdates.size||pendingMutations.size){event.preventDefault();event.returnValue='';}
  });
  let root, apiBase = '/api/patch-review', state, rowsById = new Map(), hydratedResultSignature = '', page = 0, pageSize = 25, query = '', searchTimer, statusFilter = 'all', worklistFilter = 'todo', sortOrder = 'manifest', chosenCategories = new Set(), selected = new Set(), drafts = new Map(), pendingUpdates = new Map(), updateTimers = new Map(), pendingMutations = new Set(), rowUpdateVersions = new Map(), updateVersion = 0, busy = false, auditionStartingId = null, auditionPinnedId = null, auditionPinSuppressed = false, timer, mutationQueue = Promise.resolve(), started = false, message = '', messageError = false, messageRowId = null, batchBackend = '', cpuConfirmed = false, auditionCpuConfirmed = new Set(), uiDiagRun = null, uiDiagRunNumber = 0, uiDiagHeartbeatTimer = null;

  let reviewAudio = null, audioLifecycleObserver = null, currentAudio = null, audioRequest = 0, audioPlayersCreated = 0;

  function uiDiagNow() { return window.performance?.now?.() ?? Date.now(); }
  const searchTextCache=new WeakMap();
  let renderWorklists=null,renderJobIds=null,renderCompletedJobIds=null,renderReusedJobIds=null,normalizedQuery='';
  function searchText(row) {
    let text=searchTextCache.get(row);
    if(text===undefined){
      text=[row.dialog_id,row.role_name,row.original_text,row.display_text,row.tts_text,row.note,...(row.findings||[]),...(row.current?.findings||[]),row.current?.stt?.transcript].filter(Boolean).join(' ').toLocaleLowerCase('de');
      searchTextCache.set(row,text);
    }
    return text;
  }
  function automaticallyRepaired(row) { return Boolean((row.automatically_repaired===true||row.current?.automatically_repaired===true)&&automaticallyClean(row)); }
  function uiDiagCollecting() { return Boolean(uiDiagRun&&(uiDiagRun.active||uiDiagNow()<=uiDiagRun.postUntil)); }
  function uiDiagRecordDuration(run,countKey,totalKey,maxKey,startedAt) {
    if(!run)return;
    const duration=Math.max(0,uiDiagNow()-startedAt);
    run[countKey]++;run[totalKey]+=duration;run[maxKey]=Math.max(run[maxKey],duration);
  }
  function uiDiagSnapshot() {
    const run=uiDiagRun;
    const counts={renders:run.renders,render_total_ms:Math.round(run.renderTotalMs),render_max_ms:Math.round(run.renderMaxMs),draft_captures:run.draftCaptures,draft_capture_total_ms:Math.round(run.draftCaptureTotalMs),draft_capture_max_ms:Math.round(run.draftCaptureMaxMs),requests:run.requests,request_total_ms:Math.round(run.requestTotalMs),request_max_ms:Math.round(run.requestMaxMs),request_max_in_flight:run.requestMaxInFlight,polls:run.polls,poll_max_in_flight:run.pollMaxInFlight,staged_updates:run.stagedUpdates,audioplayers_created:run.audioCreated,audioplayers_reused:run.audioReused,audioplayers_disposed:run.audioDisposed,audio_baseline:run.audioBaseline,heartbeat_lag_max_ms:Math.round(run.heartbeatLagMaxMs),elapsed_ms:Math.round(uiDiagNow()-run.startedAt),rows_total:state?.rows?.length||0,rows_visible:root?.querySelectorAll('[data-row]')?.length||0,dom_nodes:root?.querySelectorAll('*')?.length||0,audio_nodes:reviewAudio?.isConnected?1:0,audio_pool_created:audioPlayersCreated,audio_slots:root?.querySelectorAll('[data-hear]')?.length||0,candidate_nodes:root?.querySelectorAll('.pr-output')?.length||0,history_nodes:root?.querySelectorAll('.pr-history-item')?.length||0};
    for(const [endpoint,count] of Object.entries(run.requestsByEndpoint))counts[`request_${endpoint}`]=count;
    return counts;
  }
  function uiDiagPublish(phase,lagMs=0) {
    if(!uiDiagRun)return;
    const metrics=uiDiagSnapshot(),warning=lagMs>500||metrics.heartbeat_lag_max_ms>500;
    const message=`[UI-Diagnose] Lauf ${uiDiagRun.number} · ${phase}${warning?' · WARN Hauptthread-Verzögerung >500 ms':''} · ${JSON.stringify(metrics)}`;
    try { window.LauncherConsole?.update('review',message,[],{status:uiDiagRun.active?'running':'completed',warning}); } catch {}
    try {
      const bridge=window.webkit?.messageHandlers?.launcher;
      if(bridge?.postMessage)bridge.postMessage({action:'diagnostic',event:'dom_ready',message:JSON.stringify({type:'review_ui_diagnostic',phase,run:uiDiagRun.number,metrics})});
    } catch {}
  }
  function uiDiagScheduleHeartbeat() {
    clearTimeout(uiDiagHeartbeatTimer);uiDiagHeartbeatTimer=null;
    if(!uiDiagRun)return;
    const now=uiDiagNow();if(!uiDiagRun.active&&now>=uiDiagRun.postUntil){uiDiagRun=null;return;}
    if(uiDiagRun.heartbeats>=120)return;
    const delay=uiDiagRun.active?12000:Math.max(1,Math.min(12000,uiDiagRun.postUntil-now));
    uiDiagRun.heartbeatDue=now+delay;
    uiDiagHeartbeatTimer=setTimeout(()=>{
      uiDiagHeartbeatTimer=null;if(!uiDiagRun)return;
      const lag=Math.max(0,uiDiagNow()-uiDiagRun.heartbeatDue);uiDiagRun.heartbeatLagMaxMs=Math.max(uiDiagRun.heartbeatLagMaxMs,lag);
      uiDiagRun.heartbeats++;
      uiDiagPublish(uiDiagRun.active?'heartbeat':uiDiagNow()>uiDiagRun.postUntil?'delayed final snapshot':'post-run heartbeat',lag);
      uiDiagScheduleHeartbeat();
    },delay);
  }
  function uiDiagBegin() {
    clearTimeout(uiDiagHeartbeatTimer);uiDiagRunNumber++;
    uiDiagRun={number:uiDiagRunNumber,active:true,startedAt:uiDiagNow(),postUntil:Infinity,heartbeats:0,renders:0,renderTotalMs:0,renderMaxMs:0,draftCaptures:0,draftCaptureTotalMs:0,draftCaptureMaxMs:0,requests:0,requestTotalMs:0,requestMaxMs:0,requestsByEndpoint:{},requestInFlight:0,requestMaxInFlight:0,polls:0,pollInFlight:0,pollMaxInFlight:0,stagedUpdates:0,audioCreated:0,audioReused:0,audioDisposed:0,audioBaseline:reviewAudio?1:0,heartbeatLagMaxMs:0};
    uiDiagPublish('audition start');uiDiagScheduleHeartbeat();
  }
  function uiDiagFinish(phase='completed') {
    if(!uiDiagRun?.active)return;
    uiDiagRun.active=false;uiDiagRun.postUntil=uiDiagNow()+30000;uiDiagPublish(`audition ${phase}`);uiDiagScheduleHeartbeat();
  }

  async function request(path, options = {}) {
    const run=uiDiagCollecting()?uiDiagRun:null,startedAt=run?uiDiagNow():0,endpoint=String(path).split('?')[0].split('/').filter(Boolean).pop()||'root';
    if(run){run.requests++;run.requestsByEndpoint[endpoint]=(run.requestsByEndpoint[endpoint]||0)+1;run.requestInFlight++;run.requestMaxInFlight=Math.max(run.requestMaxInFlight,run.requestInFlight);}
    try {
      const response = await fetch(`${apiBase}${path}`, options);
      const body = await response.json().catch(() => ({error:response.statusText}));
      if (!response.ok) { const error=new Error(body.error || response.statusText || `HTTP ${response.status}`);error.status=response.status;throw error; }
      return body;
    } finally { if(run){run.requestInFlight=Math.max(0,run.requestInFlight-1);const duration=Math.max(0,uiDiagNow()-startedAt);run.requestTotalMs+=duration;run.requestMaxMs=Math.max(run.requestMaxMs,duration);} }
  }
  const post = (path, payload) => {
    if(readOnly() && path!=='/open-output')return Promise.reject(new Error(state.read_only_reason||'Diese Fassung kann während des laufenden Auftrags nur angehört werden.'));
    return request(path, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  };
  function setMessage(value, error = false) {
    message = value || '';
    messageError = Boolean(error);
    messageRowId = null;
  }
  function confirmSuccess(id, value) {
    id=String(id);
    if(auditionPinnedId===id){auditionPinnedId=null;auditionPinSuppressed=true;}
    const row=rowById(id);
    const list=row&&worklistFor(row);
    setMessage(`${value}${list==='todo'?' Offene Änderungen bleiben vorgemerkt.':list==='manual_confirmed'?' Unter „Manuell bestätigt“ abgelegt.':''}`);
    messageRowId=id;
  }
  function showConfirmedRow(id) {
    const row=rowById(id);if(!row)return;
    captureDrafts();discardAuditionPinForNavigation();
    query='';normalizedQuery='';statusFilter='all';chosenCategories.clear();sortOrder='manifest';
    renderWorklists=null;worklistFilter=worklistFor(row);
    const rows=state.rows.filter(matches),index=rows.indexOf(row);
    page=Math.max(0,Math.floor(index/pageSize));
    render({captureDrafts:false});
    const target=root.querySelector(`[data-row="${CSS.escape(String(id))}"]`);
    target?.scrollIntoView?.({behavior:'smooth',block:'center'});
    target?.querySelector?.('[data-help-id="review.status-signs"]')?.focus?.({preventScroll:true});
  }
  async function saveRecipe() {
    if(exportBusy)return;
    const terminal=(value,status)=>window.LauncherConsole?.update('review-save',value,[],{status,error:status==='failed'});
    exportBusy=true;
    terminal('Rezept speichern: Änderungen speichern und ausgewählte Aufnahmen prüfen …','running');
    window.LauncherConsole?.focus?.('review-save');
    render();
    try {
      await flushAllRowUpdates();
      const saved=await post('/recipe-export',{revision:state.revision});
      const data=saved.recipe;
      if(!window.webkit?.messageHandlers?.launcher) {
        const blob=new Blob([JSON.stringify(data,null,2)+'\n'],{type:'application/json'}),a=document.createElement('a');
        a.href=URL.createObjectURL(blob);a.download=`mi1-rezept-${data.pipeline}.json`;a.click();URL.revokeObjectURL(a.href);
      }
      const success=`Rezept gespeichert: ${saved.path}`;
      terminal(success,'completed');setMessage(success);
    } catch(error) {
      const failure=`Rezept speichern fehlgeschlagen: ${error.message}`;
      terminal(failure,'failed');setMessage(failure,true);
    } finally {exportBusy=false;render();}
  }
  function setRows(rows) { state.rows=rows||[]; rowsById=new Map(state.rows.map(row=>[String(row.dialog_id),row])); }
  function rowById(id) { return rowsById.get(String(id)); }
  const reportedPreviewWarnings=new Set();
  let reportedPreviewJob=null;
  function previewWarningReasons(output) {
    if(!output)return [];
    const reasons=(output.findings||[]).filter(value=>typeof value==='string'&&value.trim()).map(value=>value.trim());
    if(output.stt?.cause)reasons.push(`${sttNames[output.stt.status]||'STT'}: ${output.stt.cause}`);
    for(const stage of output.stt?.stages||[])if(stage.cause)reasons.push(`${sttStageNames[stage.stage||stage.name]||'STT-Prüfung'}: ${stage.cause}`);
    if(output.verified===false)reasons.push('Audio-/Textbeleg ist nicht vollständig geprüft; Übernahme gesperrt.');
    if(!reasons.length)for(const id of output.categories||[])reasons.push(categoryNames[id]||state?.categories?.find(value=>value.id===id)?.label||id);
    if(!reasons.length&&['failed','absent','deferred','invalid'].includes(output.stt?.status))reasons.push(sttNames[output.stt.status]);
    return [...new Set(reasons.length?reasons:['Die QA meldet eine Warnung ohne weiteren gespeicherten Grund.'])];
  }
  function reportPreviewWarnings() {
    const job=state?.job;
    if(job?.intent!=='audition'||!job.id)return;
    if(reportedPreviewJob!==job.id){reportedPreviewWarnings.clear();reportedPreviewJob=job.id;}
    for(const id of job.dialog_ids||[])for(const output of rowById(String(id))?.candidates||[]) {
      if(String(output.job_id)!==String(job.id)||!(output.warning||output.verified===false))continue;
      const reasons=previewWarningReasons(output),key=JSON.stringify([id,output.id,output.binding,reasons]);
      if(reportedPreviewWarnings.has(key))continue;
      reportedPreviewWarnings.add(key);
      window.LauncherConsole?.update('review',`Dialog ${id} · Vorschau${Number.isInteger(output.preset?.seed)?` · Seed ${output.preset.seed}`:''}: Warnung – ${reasons.join(' · ')}`,[],{status:'completed',warning:true});
    }
  }
  function updateAuditionTerminal(job) {
    if(job?.intent!=='audition')return;
    const queuedJob=job.audition_queue===true||(job.dialog_ids||[]).length>1;
    const id=String(job.active_dialog_id??job.dialog_ids?.[0]??'');
    const row=rowById(id),view=row?draftFor(row):{},preset=auditionContext?.dialog_id===id?auditionContext.preset:(view.next_preset||{});
    const backend=auditionContext?.dialog_id===id?auditionContext.backend:(view.backend||batchBackend||'unbekannt');
    const choices={keep:'beibehalten',fresh:'neue einmalige Variante',on:'an',off:'aus'};
    const phaseNames={preparing:'Renderauftrag vorbereiten',memory_check:'Speicher prüfen',loading_model:'Modell laden',model_ready:'Modell bereit',rendering:'Vorschau rendern',qa:'QA · Whisper Base',qa_medium:'QA · Whisper Medium',saving:'Audio und QA-Belege sichern',reusing:'freigegebene Probe wiederverwenden'};
    const phase=phaseNames[job.progress?.phase]||job.progress?.phase||'Renderphase nicht gemeldet';
    const status=String(job.status||'').toLowerCase(),failed=['failed','error','cancelled','interrupted'].includes(status);
    if(uiDiagRun?.active&&['completed','failed','error','cancelled','interrupted'].includes(status))uiDiagFinish(failed?'failed':'completed');
    const error=Object.values(job.errors||{}).find(value=>typeof value==='string'&&value.trim());
    const detail=typeof job.progress?.detail==='string'?job.progress.detail:
      typeof job.progress?.detail?.detail==='string'?job.progress.detail.detail:'';
    const role=row?.role_name||row?.role_id||'Rolle unbekannt';
    const selection=`Backend ${String(backend).toUpperCase()} · Seedwahl ${choices[preset.seed]||'unbekannt'} · Anker ${choices[preset.anchor]||'unbekannt'} · Prewarm ${choices[preset.prewarm]||'unbekannt'}`;
    let message;
    if(queuedJob&&failed)message=`Vorschau-Warteliste fehlgeschlagen${error?': '+error:''} · Fehler je Dialog stehen in der Warteliste`;
    else if(queuedJob&&status==='completed')message='Vorschau-Warteliste abgeschlossen · Ergebnisse und QA-Status stehen bei den Dialogen';
    else if(queuedJob)message=`Vorschau-Warteliste · ${phase}${detail?` · ${detail.slice(0,180)}`:''}`;
    else if(failed)message=`Vorschau für Dialog ${id} (${role}) fehlgeschlagen${error?': '+error:''}`;
    else if(status==='completed'&&(job.reused||[]).map(String).includes(id))message=`Vorschau für Dialog ${id} (${role}) abgeschlossen · passende freigegebene Probe wiederverwendet`;
    else if(status==='completed')message=`Vorschauauftrag für Dialog ${id} (${role}) abgeschlossen · Ergebnis und QA-Status stehen beim Dialog`;
    else message=`Vorschau für Dialog ${id} (${role}) · ${phase} · ${selection}${detail?` · ${detail.slice(0,180)}`:''}`;
    window.LauncherConsole?.update('render',message,[],{status:failed?'failed':status==='running'?'running':'completed',error:failed});
  }
  function acceptMutation(result, id) {
    if (Number.isInteger(result?.revision) && result.revision < (state.revision||0)) return false;
    if(Object.prototype.hasOwnProperty.call(result,'recipe'))state.recipe=result.recipe;
    if (Number.isInteger(result?.revision)) state.revision = result.revision;
    if (typeof result?.audition_queue_enabled === 'boolean') state.audition_queue_enabled = result.audition_queue_enabled;
    if (Array.isArray(result?.audition_queue)) state.audition_queue = result.audition_queue.map(String);
    if (Array.isArray(result?.rows)) { const imported=new Map(result.rows.map(row=>[String(row.dialog_id),row]));setRows(state.rows.map(row=>imported.get(String(row.dialog_id))||row)); }
    if (result?.row) {
      const key=String(id??result.row.dialog_id),old=rowById(key);
      if (old) { const updated={...old,...result.row}; state.rows[state.rows.indexOf(old)]=updated; rowsById.set(key,updated); }
    }
    if (result?.job) { state.job = result.job; updateAuditionTerminal(state.job); }
    return true;
  }
  function mutate(path, payload, id = payload.dialog_id, {quiet=false} = {}) {
    const action = mutationQueue.catch(() => undefined).then(async () => {
      const body = await post(path, {...payload, revision:state.revision});
      if(!acceptMutation(body,id)){if(id)await refreshScopedRows([id],{skipStatus:true});else await refreshRows();}
      if(id&&path==='/update'){
        const values=savedInputValues.get(String(id))||{};
        for(const key of ['tts_text','note'])if(Object.prototype.hasOwnProperty.call(payload,key))values[key]={input:payload[key],stored:rowById(id)?.[key]};
        savedInputValues.set(String(id),values);
      }
      if(id)clearConfirmedPendingUpdate(id,rowById(id));
      if(id)captureDrafts();
      if(!quiet)setMessage(id&&rowById(id)&&hasDraftChanges(rowById(id))?'Weitere Änderungen noch nicht gespeichert.':'Gespeichert.'); render();
      return body;
    });
    mutationQueue = action.then(() => undefined, () => undefined);
    const result=action.catch(error => { if(!quiet){setMessage(error.message, true);render();} throw error; });
    const tracked=result.finally(()=>pendingMutations.delete(tracked));pendingMutations.add(tracked);
    return tracked;
  }
  function stageRowUpdate(id, payload) {
    if(readOnly())return;
    if(uiDiagCollecting())uiDiagRun.stagedUpdates++;
    id=String(id);rowSaveTouched.add(id);rowSaveErrors.delete(id);
    let versions=rowUpdateVersions.get(id);if(!versions){versions=new Map();rowUpdateVersions.set(id,versions);}
    for(const key of Object.keys(payload))versions.set(key,++updateVersion);
    pendingUpdates.set(id,{...(pendingUpdates.get(id)||{}),...payload});
    clearTimeout(updateTimers.get(id));
    updateTimers.set(id,setTimeout(()=>flushRowUpdate(id).catch(()=>undefined),220));
    paintRowSaveStatus(id);
  }
  function flushRowUpdate(id) {
    id=String(id);clearTimeout(updateTimers.get(id));updateTimers.delete(id);
    const payload=pendingUpdates.get(id);if(!payload)return Promise.resolve(null);
    const versions=rowUpdateVersions.get(id),capturedVersions=new Map(Object.keys(payload).map(key=>[key,versions?.get(key)]));
    pendingUpdates.delete(id);
    rowSaveCounts.set(id,(rowSaveCounts.get(id)||0)+1);paintRowSaveStatus(id);
    const save=mutate('/update',{dialog_id:id,...payload},id,{quiet:true}).then(body=>{
      rowSaveErrors.delete(id);return body;
    }).catch(error=>{
      const row=rowById(id)||{},retry={};
      for(const [key,value] of Object.entries(payload)){
        if(versions?.get(key)!==capturedVersions.get(key)||editorValueSaved(id,key,value,row[key]))continue;
        retry[key]=value;
      }
      if(Object.keys(retry).length){pendingUpdates.set(id,{...retry,...(pendingUpdates.get(id)||{})});rowSaveErrors.set(id,error.message);}
      throw error;
    }).finally(()=>{
      const count=(rowSaveCounts.get(id)||1)-1;
      if(count)rowSaveCounts.set(id,count);else rowSaveCounts.delete(id);
      paintRowSaveStatus(id);
    });
    return save;
  }
  function jsonValueEqual(left,right) {
    if(left===right)return true;
    if(left===null||right===null||typeof left!=='object'||typeof right!=='object')return false;
    if(Array.isArray(left)||Array.isArray(right))return Array.isArray(left)&&Array.isArray(right)&&left.length===right.length&&left.every((value,index)=>jsonValueEqual(value,right[index]));
    const leftKeys=Object.keys(left).sort(),rightKeys=Object.keys(right).sort();
    return leftKeys.length===rightKeys.length&&leftKeys.every((key,index)=>key===rightKeys[index]&&jsonValueEqual(left[key],right[key]));
  }
  function fieldwisePresetEqual(left,right) { return jsonValueEqual(left||{},right||{}); }
  function sameUpdateValue(key,left,right) { return key==='next_preset'?fieldwisePresetEqual(left,right):left===right; }
  function clearConfirmedPendingUpdate(id,row) {
    if(!row)return;
    id=String(id);const pending=pendingUpdates.get(id);if(!pending)return;
    for(const [key,value] of Object.entries(pending))if(editorValueSaved(id,key,value,row[key]))delete pending[key];
    if(!Object.keys(pending).length){pendingUpdates.delete(id);clearTimeout(updateTimers.get(id));updateTimers.delete(id);}
  }
  async function flushAllRowUpdates() {
    while(true){
      await Promise.all([...pendingUpdates.keys()].map(id=>flushRowUpdate(id)));
      await Promise.all([...pendingMutations]);
      if(!pendingUpdates.size&&!pendingMutations.size)break;
    }
  }
  function discardAuditionPinForNavigation() {
    auditionPinnedId=null;
    auditionPinSuppressed=true;
  }
  function captureDrafts() {
    if (!root || readOnly()) return;
    const diagnosticRun=uiDiagCollecting()?uiDiagRun:null,startedAt=diagnosticRun?uiDiagNow():0;
    try {
      const gameRootNode=root.querySelector('#pr-build-game-root');if(gameRootNode)buildGameRoot=buildGameRootEdited?gameRootNode.value:null;
      const outputRootNode=root.querySelector('#pr-build-output-root');if(outputRootNode&&buildOutputRootEdited)buildOutputRoot=outputRootNode.value;
      root.querySelectorAll('[data-tts]').forEach(node=>{
        const id=node.dataset.tts, row=rowById(id), old=drafts.get(id)||{};
        const value = key => root.querySelector(`[data-${key}="${CSS.escape(id)}"]`)?.value;
        drafts.set(id,{tts_text:node.value,note:root.querySelector(`[data-note="${CSS.escape(id)}"]`)?.value??old.note??row?.note??'',next_preset:{...(row?.next_preset||{}),...(old.next_preset||{}),seed:'keep',anchor:value('anchor')??old.next_preset?.anchor??row?.next_preset?.anchor??'keep',prewarm:value('prewarm')??old.next_preset?.prewarm??row?.next_preset?.prewarm??'keep'},generation_mode:value('generation-mode')??old.generation_mode??row?.generation_mode??'synthesize',backend:(value('backend')??old.backend??row?.backend??null)||null});
      });
    } finally { uiDiagRecordDuration(diagnosticRun,'draftCaptures','draftCaptureTotalMs','draftCaptureMaxMs',startedAt); }
  }
  function draftFor(row) { return {...row,...(drafts.get(String(row.dialog_id))||{})}; }
  function canReuseAccepted(row, requestedBatchBackend = batchBackend) {
    const probe=row?.accepted_probe;
    if(!probe?.reusable||!probe.effective_backend||row.generation_mode==='copy_original')return false;
    const view=draftFor(row),savedPreset=row.next_preset||{},draftPreset=view.next_preset||{};
    if(view.generation_mode!==(row.generation_mode||'synthesize')||!editorValueSaved(row.dialog_id,'tts_text',view.tts_text,row.tts_text))return false;
    if(!fieldwisePresetEqual(draftPreset,savedPreset))return false;
    const rowBackend=view.backend||null;
    if(rowBackend!==(row.backend||null))return false;
    const effectiveBackend=rowBackend||requestedBatchBackend||state?.backends?.[0]||probe.effective_backend;
    return effectiveBackend===probe.effective_backend;
  }
  async function saveRowForAudition(id) {
    captureDrafts();
    const row=rowById(id),view=drafts.get(String(id))||draftFor(row);
    const current=row||{};
    const payload={dialog_id:String(id)};
    if(!editorValueSaved(id,'tts_text',view.tts_text,current.tts_text))payload.tts_text=view.tts_text;
    if(!editorValueSaved(id,'note',view.note??'',current.note||''))payload.note=view.note??'';
    if(!fieldwisePresetEqual(view.next_preset,current.next_preset))payload.next_preset=view.next_preset;
    if(view.generation_mode!==(current.generation_mode||'synthesize'))payload.generation_mode=view.generation_mode;
    if((view.backend||null)!==(current.backend||null))payload.backend=view.backend||null;
    if(Object.keys(payload).length===1)return {row};
    return mutate('/update',payload);
  }
  async function saveCurrentDraft(id) {
    await flushRowUpdate(id);
    await mutationQueue;
    captureDrafts();
    const view=drafts.get(String(id))||{},current=rowById(id)||{},tts_text=root.querySelector(`[data-tts="${CSS.escape(String(id))}"]`)?.value??view.tts_text,note=root.querySelector(`[data-note="${CSS.escape(String(id))}"]`)?.value??view.note,payload={dialog_id:String(id)};
    if(!editorValueSaved(id,'tts_text',tts_text,current.tts_text))payload.tts_text=tts_text;
    if(!editorValueSaved(id,'note',note,current.note||''))payload.note=note;
    if(!fieldwisePresetEqual(view.next_preset,current.next_preset))payload.next_preset=view.next_preset;
    if((view.generation_mode||'synthesize')!==(current.generation_mode||'synthesize'))payload.generation_mode=view.generation_mode;
    if((view.backend||null)!==(current.backend||null))payload.backend=view.backend||null;
    if(Object.keys(payload).length===1){setMessage('Keine Änderungen zum Speichern.');render();return;}
    await mutate('/update',payload,String(id));
  }
  function pendingAction(row) {
    if (row?.next_action === 'render' || row?.next_action === 'copy_original') return row.next_action;
    if (row?.marked) return row.generation_mode === 'copy_original' ? 'copy_original' : 'render';
    return 'none';
  }
  function processAction(row) {
    const pending = pendingAction(row);
    return pending !== 'none' ? pending : (row?.generation_mode === 'copy_original' ? 'copy_original' : 'render');
  }
  function batchSelection(rows) {
    const selectedRows=rows.filter(row=>selected.has(String(row.dialog_id)));
    const single=selected.size===1;
    const entries=selectedRows.map(row=>({row,action:single?processAction(row):pendingAction(row)}));
    const eligible=entries.filter(({row,action})=>single||action!=='none'||canReuseAccepted(row));
    return {entries,eligible,skipped:Math.max(0,selected.size-eligible.length)};
  }
  function batchStartDisabled(eligibleCount,hasSynthesis,cpuSelected,cpuChoice=cpuConfirmed) {
    return readOnly()||busy||state.job?.status==='running'||state.patch_build?.job?.status==='running'||!eligibleCount||(hasSynthesis&&!state.render_enabled)||(cpuSelected&&!cpuChoice);
  }
  function manuallyConfirmed(row) {
    const output=row.current;
    // Approval is supplied on the exact verified output by the service. A row
    // flag or an approved historic/preview binding cannot approve another WAV.
    return Boolean(output?.verified===true&&sameOutputBinding(output.binding,output.binding)&&String(output.binding.dialog_id)===String(row.dialog_id)&&(output.good_confirmed===true||output.approved===true));
  }
  function automaticallyClean(row) {
    const output=row.current,copied=['copy_original','original','no_render'].includes(row.processing),qa=String(output?.review_status??output?.qa_status??'').toLowerCase();
    return Boolean(output?.verified===true&&!output.warning&&!row.warning&&(copied||output.stt?.status==='not_required'&&!qa||['pass','passed','ok','good'].includes(qa)));
  }
  function classify(row, generationMode=row.generation_mode) {
    const current=row.current,copied=['copy_original','original','no_render'].includes(row.processing);
    const plannedOriginal=generationMode==='copy_original'&&current?.id!=='original';
    const confirmed=manuallyConfirmed(row),repaired=automaticallyRepaired(row);
    const candidateWarning=!confirmed&&!repaired&&activeReviewCandidates(row).some(candidate=>candidate.warning||candidate.verified!==true);
    const warning=Boolean((plannedOriginal?!originalChoiceValid(row):!confirmed&&(row.warning||current?.warning||(!current&&!copied)||(current&&current.verified!==true)))||candidateWarning||(!confirmed&&!repaired&&latestJobError(row)));
    const invalidOriginalChoice=plannedOriginal&&!originalChoiceValid(row);
    const render=Boolean(!invalidOriginalChoice&&(pendingAction(row)!=='none'||(plannedOriginal&&originalChoiceValid(row))||hasDraftChanges(row)));
    return {good:Boolean(!render&&!warning&&(confirmed||automaticallyClean(row))),warning,render};
  }
  function hasDraftChanges(row) {
    const view=draftFor(row);
    return !editorValueSaved(row.dialog_id,'tts_text',view.tts_text,row.tts_text)||!fieldwisePresetEqual(view.next_preset||row.next_preset,row.next_preset)||(view.generation_mode||'synthesize')!==(row.generation_mode||'synthesize')||(view.backend||null)!==(row.backend||null);
  }
  function originalChoiceValid(row) { return row.original_choice_valid??(row.original_release_enabled===true); }
  function currentAudioStatus(row) {
    const current=row.current;
    if(manuallyConfirmed(row))return {kind:'manual',label:'Aktuelle Ausgabe manuell bestätigt'};
    if(current?.verified===true&&automaticallyRepaired(row))return {kind:'repaired',label:'Aktuelle Ausgabe automatisch repariert'};
    if(automaticallyClean(row))return {kind:'clean',label:'Aktuelle Ausgabe automatisch unauffällig'};
    const active=state?.job?.status==='running'&&String(state.job.active_dialog_id||'')===String(row.dialog_id);
    if(active&&!current?.verified)return {kind:'active',label:'Aktuelle Ausgabe wird erzeugt'};
    const qa=String(current?.review_status??current?.qa_status??'').toLowerCase();
    if(current?.verified===false||['fail','failed','error','render_error','invalid'].includes(qa)||!current&&latestJobError(row))return {kind:'invalid',label:'Aktuelle Ausgabe ungültig oder fehlerhaft'};
    if(current&&(row.warning||current.warning||['warn','warning'].includes(qa)))return {kind:'warning',label:'Aktuelle Ausgabe mit offenen Warnungen'};
    return {kind:'open',label:'Aktuelle Ausgabe offen oder noch nicht bewertet'};
  }
  function currentAudioStatusMarkup(row) {
    const status=currentAudioStatus(row);
    return `<button type="button" class="pr-current-status pr-current-status-${status.kind}" data-help-id="review.status-signs" data-review-status="${status.kind}" aria-label="${esc(status.label)}. Hilfe: Umschalt-Klick, F1 oder Fragezeichen."><img src="/wiki/status-${status.kind}.svg" width="24" height="24" alt="" aria-hidden="true"${status.kind==='active'?' class="pr-status-spinner"':''}></button>`;
  }

  function jobHasDialog(id) {
    return renderJobIds?renderJobIds.has(String(id)):(state?.job?.dialog_ids||[]).some(value=>String(value)===String(id));
  }
  function jobHasCompletedDialog(id) {
    return renderCompletedJobIds?renderCompletedJobIds.has(String(id)):(state?.job?.completed||[]).some(value=>String(value)===String(id));
  }
  function jobHasReusedDialog(id) {
    return renderReusedJobIds?renderReusedJobIds.has(String(id)):(state?.job?.reused||[]).some(value=>String(value)===String(id));
  }
  function latestJobError(row) {
    const job=state?.job,id=String(row.dialog_id);
    if(job?.status!=='failed'||!jobHasDialog(id))return '';
    return job.errors?.[id]||(job.errors?.interrupted&&!jobHasCompletedDialog(id)?job.errors.interrupted:'')||'';
  }
  function acceptedCandidateCutoff(row) {
    const acceptedIds=new Set((row.accepted_candidate_ids||[]).map(String));
    if(!Array.isArray(row.accepted_candidate_ids)&&row.current)acceptedIds.add(String(row.current.id));
    let cutoff=-1;
    (row.candidates||[]).forEach((candidate,index)=>{
      if(acceptedIds.has(String(candidate.id))||(row.current&&sameOutputBinding(row.current.binding,candidate.binding)))cutoff=index;
    });
    return cutoff;
  }
  function activeReviewCandidates(row) {
    if(row.generation_mode==='copy_original')return [];
    const cutoff=acceptedCandidateCutoff(row),candidates=row.candidates||[];
    return openCandidates(row).filter(candidate=>candidates.indexOf(candidate)>cutoff);
  }
  function reviewCandidate(row) { return activeReviewCandidates(row).at(-1)||null; }
  function worklistFor(row) {
    if(renderWorklists?.has(row))return renderWorklists.get(row);
    const classification=classify(row);
    const runningJob=state?.job?.status==='running'&&jobHasDialog(row.dialog_id);
    const result=classification.render?'todo':manuallyConfirmed(row)?'manual_confirmed':automaticallyRepaired(row)?'auto_repaired':runningJob||activeReviewCandidates(row).length||classification.warning||!row.current?.verified?'todo':classification.good?'auto_clean':'todo';
    renderWorklists?.set(row,result);return result;
  }
  function displayedFindingCount(row) {
    const candidate=reviewCandidate(row),output=latestOpenCandidate(row)||row.current,plannedOriginal=draftFor(row).generation_mode==='copy_original'&&row.current?.id!=='original';
    let findings=candidate?(candidate.findings||[]):plannedOriginal?[]:latestJobError(row)?[latestJobError(row)]:
      row.current?.id==='original'&&row.current?.verified===true||manuallyConfirmed(row)?[]:[...(row.findings||[]),...(row.current?.findings||[])];
    const cause=output?.stt?.cause?.trim();
    findings=findings.filter(item=>!(cause&&(item===cause||item.includes(cause)))&&!(output?.stt?.status==='deferred'&&/^STT zurückgestellt\b/i.test(item)));
    return new Set(findings.filter(Boolean).map(String)).size;
  }
  function todoDiagnostic() {
    const reasonCounts={running_job:0,planned_render:0,open_candidates:0,warning:0,current_unverified:0,not_reviewed_or_good:0};
    const todo=[];
    for(const row of state.rows){
      const classification=classify(row),runningJob=state?.job?.status==='running'&&jobHasDialog(row.dialog_id);
      const candidates=activeReviewCandidates(row),reasons=[];
      if(runningJob)reasons.push('running_job');
      if(classification.render)reasons.push('planned_render');
      if(candidates.length)reasons.push('open_candidates');
      if(classification.warning)reasons.push('warning');
      if(!row.current?.verified)reasons.push('current_unverified');
      const isTodo=worklistFor(row)==='todo';
      if(isTodo&&!reasons.length)reasons.push('not_reviewed_or_good');
      if(!isTodo)continue;
      for(const reason of reasons)reasonCounts[reason]++;
      const current=row.current||{};
      todo.push({dialog_id:String(row.dialog_id),reasons,processing:row.processing,next_action:row.next_action,
        row_warning:Boolean(row.warning),row_good:Boolean(row.good),current_verified:Boolean(current.verified),
        current_warning:Boolean(current.warning),qa_status:current.qa_status||null,review_status:current.review_status||null,
        stt_status:current.stt?.status||null,findings:[...(row.findings||[]),...(current.findings||[])],
        qa_reason_codes:current.qa_reason_codes||[],
        open_candidates:candidates.map(candidate=>({id:candidate.id,verified:Boolean(candidate.verified),warning:Boolean(candidate.warning)})),
        original_text:row.original_text||'',display_text:row.display_text||'',tts_text:row.tts_text||'',qa_expected_text:row.qa_expected_text||''});
    }
    return {format:'patch-review-todo-diagnostic/1',manifest_id:state.manifest_id||null,manifest_sha256:state.manifest_sha256||null,
      revision:state.revision,total_rows:state.rows.length,todo_rows:todo.length,reviewed_rows:state.rows.length-todo.length,
      reason_counts_overlap:true,reason_counts:reasonCounts,rows:todo};
  }
  function categoryIdsFor(row) {
    const candidate=reviewCandidate(row);
    if(candidate)return candidate.categories||[];
    if(row.generation_mode==='copy_original'&&row.current?.id!=='original')return [];
    return row.good?[]:[...(row.categories||[]),...(row.current?.categories||[])];
  }
  function categoriesFor(row, latestCandidate = null) {
    const candidate = latestCandidate || reviewCandidate(row);
    const all = new Set(candidate ? (candidate.categories || []) : row.generation_mode==='copy_original'&&row.current?.id!=='original'?[]:row.good ? [] : [...(row.categories || []), ...(row.current?.categories || [])]);
    return [...all].map(id => `<span>${esc(categoryNames[id] || state.categories.find(c=>c.id===id)?.label || id)}</span>`).join('') || '<span>Keine Fehlerkategorie</span>';
  }
  function findingList(items) {
    const values = (items || []).filter(Boolean);
    return values.length ? `<ul class="pr-findings">${values.map(item=>`<li>${esc(item)}</li>`).join('')}</ul>` : '<p class="muted pr-stt">Es sind keine weiteren Hinweise gespeichert.</p>';
  }
  function sttMarkup(output) {
    if (!output) return '<p class="pr-stt">Für diesen Dialog gibt es noch keine erzeugte Aufnahme.</p>';
    const stt = output.stt || {status:'invalid'};
    const explanation = {
      absent:'Die Spracherkennung hat noch keinen Text geliefert. Höre die Aufnahme selbst an.',
      failed:`Die STT-Prüfung ist fehlgeschlagen${stt.cause ? `: ${stt.cause}` : '.'}`,
      deferred:'',
      not_required:'Für diese Ausgabe war keine STT-Prüfung erforderlich.',
      invalid:'Das gespeicherte Ergebnis der Spracherkennung ist ungültig und zählt deshalb nicht als Prüfung.'
    }[stt.status];
    const parsedStages=(stt.stages||[]).map(stage=>{
      if(typeof stage!=='string')return stage;
      try {return JSON.parse(stage);} catch {return {cause:stage};}
    });
    const recognized=stage=>stage?.transcript??stage?.recognized_text??stage?.normalized_transcript;
    const firstTranscript=recognized(parsedStages[0]);
    const stages=parsedStages.map((stage,i)=>{
      if(!stage||typeof stage!=='object')return '';
      const status=stage.status?sttStageStatus[stage.status]||stage.status:'';
      const model=stage.model||stage.model_name;
      const transcript=recognized(stage);
      const cause=stage.cause||stage.error||stage.reason;
      const sameAsFirst=i>0&&transcript!=null&&firstTranscript!=null&&String(transcript).trim()===String(firstTranscript).trim();
      const result=sameAsFirst?' · Stimmt mit der ersten Spracherkennungsprüfung überein.':transcript!=null?` · Erkannt: ${esc(transcript||'(erfolgreich, leer erkannt)')}`:'';
      return `<li><b>Spracherkennungsprüfung ${i+1}</b>${status?` · ${esc(status)}`:''}${model?` · ${esc(model)}`:''}${result}${cause?` · Ursache: ${esc(cause)}`:''}</li>`;
    }).join('')+(parsedStages.length===1&&stt.status==='completed'&&stt.second_check_required===false?'<li><b>Spracherkennungsprüfung 2</b> · Nicht erforderlich</li>':'');
    const qaEvidence=(output.good_confirmed||(output.qa_status&&output.qa_status!==output.review_status)||output.qa_reason_codes?.length)?`<details class="pr-history pr-accepted-evidence"><summary>${output.good_confirmed?'Gut bestätigte Ausgabe · frühere QA-/STT-Befunde':'Gespeicherter QA-Ausgangsbefund'}</summary><p>Gespeicherter QA-Status: <b>${esc(output.qa_status||'unbekannt')}</b>; Reviewentscheidung: <b>${esc(output.good_confirmed?'Gut bestätigt':output.review_status||'unbekannt')}</b>.</p>${output.review_reason==='named_date_equivalence'?'<p>Der vollständige Textvergleich behandelt nur die ausgeschriebene und numerische Form desselben benannten Datums als gleichwertig.</p>':''}${output.review_reason==='integer_number_equivalence'?'<p>Der vollständige Textvergleich behandelt dieselbe ganze Zahl als Ziffern und ausgeschrieben gleichwertig. Der ursprüngliche STT- und QA-Beleg bleibt erhalten.</p>':''}${output.qa_reason_codes?.length?`<p><b>Gespeicherte QA-Gründe:</b> ${esc(output.qa_reason_codes.join(', '))}</p>`:''}${findingList([...new Set([...(output.qa_evidence_findings||[]),...(output.findings||[])])])}</details>`:'';
    return `<div class="pr-stt"><p><b>${esc(sttNames[stt.status] || text(stt.status))}</b>${stt.model ? ` · ${esc(stt.model)}` : ''}</p>${!parsedStages.some(stage=>recognized(stage)!=null)&&stt.transcript != null ? `<p><b>Erkannt:</b> ${esc(stt.transcript || '(erfolgreich, leer erkannt)')}</p>` : ''}${explanation ? `<p>${esc(explanation)}</p>` : ''}${stt.cause && stt.status !== 'failed' ? `<p>${esc(stt.cause)}</p>` : ''}${stages ? `<p><b>Prüfschritte:</b></p><ul>${stages}</ul>` : ''}${qaEvidence}</div>`;
  }
  function presetMarkup(output) {
    if (!output?.preset) return '<p class="pr-stt">Tatsächliches Preset: unbekannt (kein Presetbeleg).</p>';
    const p = output.preset;
    const value = (name, val) => val == null ? `${name}: unbekannt` : `${name}: ${val}`;
    return `<p class="pr-stt"><b>Tatsächlich gerendert:</b> ${esc([value('Seed',p.seed),value('Anker',p.anchor == null ? null : p.anchor ? 'an' : 'aus'),value('Prewarm',p.prewarm == null ? null : p.prewarm ? 'an' : 'aus'),value('Rendermodus',p.backend),value('Methode',p.method)].join(' · '))}</p>`;
  }
  function originalOutput(output) { return output?.id==='original'||output?.binding?.fingerprint==='original'||['copy_original','original','no_render'].includes(output?.processing); }
  function hearingContext(row,label,output) {
    return `Dialog ${row.dialog_id}${row.role_name?` · ${row.role_name}`:''} · ${label}${output?.id!=null?` · Fassung ${output.id}`:''}${Number.isInteger(output?.preset?.seed)?` · Seed ${output.preset.seed}`:''}`;
  }
  function audioPlayerMarkup(label, output, cls = '', playerKey = null, row = null) {
    if (!output) return `<section class="pr-audio-player ${cls}"><h4>${esc(label)}</h4><p class="pr-stt">Keine Ausgabe vorhanden.</p></section>`;
    const identity=String(output.id||output.audio_url||'output'),role=originalOutput(output)||row&&['copy_original','original','no_render'].includes(row.processing)?'original':'selected';
    const context=row?hearingContext(row,label,output):label;
    return `<section class="pr-audio-player ${cls}"><h4>${esc(label)} · ${esc(reviewLabel(output))}</h4>${output.audio_url ? audioPlayerSlotMarkup(playerKey||`output-${identity}`,output.audio_url,context,role,row?.dialog_id,output.id,row&&output.verified===true?`data-preview-current="${esc(row.dialog_id)}"`:'') : '<p class="pr-stt">Keine Audio-URL vorhanden.</p>'}</section>`;
  }
  function audioPlayerSlotMarkup(playerKey,src,label,role='preview',dialogId='',outputId='',extra='') {
    return `<span class="pr-hear-control"><button type="button" class="pr-hear" data-hear data-player-key="${esc(playerKey)}" data-player-src="${esc(src)}" data-player-label="${esc(label)}" data-player-role="${esc(role)}" data-player-dialog="${esc(dialogId)}" data-player-output="${esc(outputId)}" aria-label="${esc(`Abspielen · ${label}`)}" aria-controls="pr-review-audio" aria-pressed="false" ${extra}><span aria-hidden="true">▶</span> Abspielen</button><span class="pr-hear-error" role="status" aria-live="polite" hidden></span></span>`;
  }
  function sameAudioSource(button,record=currentAudio) {
    return Boolean(record&&button.dataset.playerSrc===record.src&&button.dataset.playerDialog===record.dialogId&&button.dataset.playerOutput===record.outputId);
  }
  function updateAudioButtons() {
    for(const button of root?.querySelectorAll('[data-hear]')||[]) {
      const matches=sameAudioSource(button),playing=matches&&currentAudio.wantsPlay;
      button.dataset.playing=String(Boolean(playing));
      button.setAttribute('aria-pressed',String(Boolean(playing)));
      button.setAttribute('aria-label',`${playing?'Pause':'Abspielen'} · ${button.dataset.playerLabel}`);
      button.innerHTML=`<span aria-hidden="true">${playing?'⏸':'▶'}</span> ${playing?'Pause':'Abspielen'}`;
      const error=button.parentElement.querySelector('.pr-hear-error');
      if(error){error.hidden=!(matches&&currentAudio.error);error.textContent=matches?currentAudio.error||'':'';}
    }
  }
  function mountReviewAudio() {
    if(!reviewAudio) {
      reviewAudio=document.createElement('audio');reviewAudio.id='pr-review-audio';reviewAudio.dataset.reviewAudio='';
      reviewAudio.controls=false;reviewAudio.hidden=true;reviewAudio.preload='none';reviewAudio.loop=false;reviewAudio.autoplay=false;
      reviewAudio.setAttribute('aria-hidden','true');
      // Keep the one native player outside render()'s replaced subtree.
      root.before(reviewAudio);audioPlayersCreated++;
      if(uiDiagCollecting())uiDiagRun.audioCreated++;
      for(const event of ['play','playing','pause','ended','error','loadstart','loadedmetadata','loadeddata','canplay','emptied'])reviewAudio.addEventListener(event,()=>{
        if(!currentAudio)return;
        if((event==='play'||event==='playing')&&!currentAudio.wantsPlay){reviewAudio.pause();return;}
        if(event==='playing'&&!reviewAudio.paused)currentAudio.pending=false;
        if(event==='pause'&&reviewAudio.paused&&!currentAudio.pending)currentAudio.wantsPlay=false;
        // A late play() settlement must not re-arm playback after natural end.
        if(event==='ended'&&reviewAudio.ended){audioRequest++;currentAudio.wantsPlay=false;currentAudio.pending=false;reviewAudio.pause();}
        if(event==='error'&&reviewAudio.error){currentAudio.wantsPlay=false;currentAudio.pending=false;currentAudio.error='Audio konnte nicht geladen werden. Erneut abspielen.';}
        updateAudioButtons();
      });
    }
    if(!reviewAudio.isConnected)root.before(reviewAudio);
    audioLifecycleObserver?.disconnect();
    const mountedRoot=root;
    audioLifecycleObserver=new MutationObserver(()=>{if(!mountedRoot.isConnected){clearReviewAudio();audioLifecycleObserver.disconnect();}});
    audioLifecycleObserver.observe(document.documentElement,{childList:true,subtree:true});
  }
  async function hearAudio({role,src,label,key,dialogId='',outputId=''}) {
    if(!reviewAudio||!src||completionView||!root?.isConnected)return;
    dialogId=String(dialogId);outputId=String(outputId??'');
    const same=currentAudio&&currentAudio.src===src&&currentAudio.dialogId===dialogId&&currentAudio.outputId===outputId;
    const request=++audioRequest;
    if(same&&currentAudio.wantsPlay){currentAudio.wantsPlay=false;currentAudio.pending=false;reviewAudio.pause();updateAudioButtons();return;}
    const reload=!same||Boolean(reviewAudio.error);
    if(reload) {
      reviewAudio.pause();
      currentAudio={role,src,label,key,dialogId,outputId,wantsPlay:true,pending:true,error:''};
      reviewAudio.dataset.playerKey=key;reviewAudio.setAttribute('src',src);reviewAudio.load();
    } else {
      if(uiDiagCollecting())uiDiagRun.audioReused++;
      currentAudio.wantsPlay=true;currentAudio.pending=true;currentAudio.error='';
      if(reviewAudio.ended)reviewAudio.currentTime=0;
    }
    updateAudioButtons();
    // load() and play() stay in the source button's native user gesture.
    try {
      await reviewAudio.play();
      if(request!==audioRequest)return;
      currentAudio.pending=false;currentAudio.wantsPlay=!reviewAudio.paused&&!reviewAudio.ended;
    } catch(error) {
      if(request!==audioRequest)return;
      currentAudio.pending=false;currentAudio.wantsPlay=false;
      if(error.name!=='AbortError')currentAudio.error=`Wiedergabe nicht möglich: ${error.message||'Erneut abspielen.'}`;
    }
    updateAudioButtons();
  }
  function clearReviewAudio() {
    audioRequest++;const hadSource=Boolean(currentAudio);currentAudio=null;
    if(reviewAudio){reviewAudio.pause();reviewAudio.removeAttribute('src');reviewAudio.removeAttribute('data-player-key');if(hadSource){reviewAudio.load();if(uiDiagCollecting())uiDiagRun.audioDisposed++;}}
    updateAudioButtons();
  }
  function outputDetailsMarkup(output, includeFindings = true) {
    if (!output) return '';
    return `<div class="pr-output-details">${output.tts_text != null ? `<p class="pr-stt"><b>Verwendeter Sprachausgabetext:</b> ${esc(output.tts_text)}</p>` : ''}${sttMarkup(output)}${presetMarkup(output)}${includeFindings?findingList(output.findings):''}</div>`;
  }
  function audioBlock(label, output, cls = '', includeFindings = true) {
    if (!output) return `<section class="${cls}"><h4>${esc(label)}</h4><p class="pr-stt">Keine Ausgabe vorhanden.</p></section>`;
    return `<section class="${cls}">${audioPlayerMarkup(label,output)}${outputDetailsMarkup(output,includeFindings)}</section>`;
  }
  function sameOutput(left,right) {
    return Boolean(left&&right&&(left.id!=null&&right.id!=null&&String(left.id)===String(right.id)||sameOutputBinding(left.binding,right.binding)));
  }
  function otherOutputs(row,latest) {
    const outputs=[];
    for(const output of [...(row.history||[]),...(row.candidates||[])]) {
      if(sameOutput(output,row.current)||sameOutput(output,latest))continue;
      if(!outputs.some(existing=>sameOutput(existing,output)))outputs.push(output);
    }
    return outputs.reverse();
  }
  function historyMarkup(row,latest) {
    const outputs=otherOutputs(row,latest);
    if(!outputs.length)return '';
    return `<details class="pr-history pr-version-history"><summary>Verlauf · ${outputs.length} frühere Versuche</summary><p class="muted">Hier findest du frühere Sprachausgaben mit ihren Texten, Einstellungen und Prüfergebnissen.</p>${outputs.map(output=>candidateMarkup(row,output,false,true,true)).join('')}</details>`;
  }
  function sameOutputBinding(left,right) {
    if(!left||!right)return false;
    const keys=['manifest_sha256','dialog_id','fingerprint','wav_sha256','text_sha256'];
    return keys.every(key=>left[key]!=null&&right[key]!=null&&left[key]===right[key]);
  }
  function openCandidates(row) {
    const current=row.current,history=row.history||[];
    return (row.candidates||[]).filter(candidate=>{
      if(current&&(candidate.id===current.id||sameOutputBinding(candidate.binding,current.binding)))return false;
      if(history.some(output=>candidate.id===output.id||sameOutputBinding(candidate.binding,output.binding)))return false;
      return true;
    });
  }
  function verifiedCandidates(row) { return (row.candidates||[]).filter(candidate=>candidate.verified===true&&candidate.audio_url&&candidate.job_id); }
  function latestOpenCandidate(row) {
    if(row.generation_mode==='copy_original')return null;
    const inJob=jobHasDialog(row.dialog_id);
    const errored=Boolean(inJob&&state.job?.errors?.[row.dialog_id]);
    const verified=activeReviewCandidates(row).filter(item=>item.verified===true&&item.audio_url&&item.job_id).filter(item=>!errored||String(item.job_id)!==String(state.job?.id));
    const candidate=inJob&&jobHasCompletedDialog(row.dialog_id)&&!errored?(verified.filter(item=>String(item.job_id)===String(state.job?.id)).at(-1)||verified.at(-1)):verified.at(-1)||null;
    if(!candidate||String(candidate.id)===String(row.current?.id))return null;
    if((row.history||[]).some(output=>String(output.id)===String(candidate.id)||sameOutputBinding(candidate.binding,output.binding)))return null;
    return candidate;
  }
  function restorableAuditionPin(job) {
    if(job?.intent!=='audition'||(job.dialog_ids||[]).length!==1)return null;
    const id=String(job.dialog_ids[0]),row=rowById(id);
    if(!row||manuallyConfirmed(row)&&job.status!=='running')return null;
    if(job.status==='running')return id;
    const candidate=latestOpenCandidate(row);
    if(candidate&&String(candidate.job_id)===String(job.id))return id;
    return ['failed','error'].includes(job.status)&&Boolean(job.errors?.[id])?id:null;
  }
  function progressLabel(progress) {
    if(!progress)return 'Rendern und Prüfen läuft …';
    const phases={preparing:'Renderauftrag wird vorbereitet',memory_check:'Speicher wird geprüft',loading_model:'KI lädt',model_ready:'KI geladen',rendering:progress.model_loaded===true?'KI geladen · Rendert':'Audio wird gerendert',saving:'Audio prüfen und speichern',reusing:'Angenommene Probe wird wiederverwendet'};
    const label=phases[progress.phase]||'Rendern und Prüfen läuft';
    const detail=String(progress.detail||'').trim();
    return detail&&detail!==label?`${label} · ${detail}`:label;
  }
  function memoryAdvisoryText() {
    const advisory=state.job?.memory_advisory;
    if(!advisory)return '';
    const gib=value=>typeof value==='number'&&Number.isFinite(value)&&value>=0?`${(value/1073741824).toFixed(2)} GiB`:'nicht messbar';
    const parts=[`Vor Modellstart: RAM frei ${gib(advisory.free_ram_bytes)}`,`frei + inaktiv (Schätzung) ${gib(advisory.available_ram_estimate_bytes)}`,`Modellbedarf (Dateigrößen + 1 GiB Reserve, Schätzung) ${gib(advisory.estimated_required_bytes)}`];
    if(advisory.pressure===true)parts.push('Speicher knapp: Die verfügbare RAM-Schätzung liegt unter dem geschätzten Modellbedarf.');
    else if(advisory.pressure==null)parts.push('Speichersituation nicht vollständig messbar.');
    if(typeof advisory.metal_recommended_working_set_bytes==='number')parts.push(`Metal-Empfehlung ${gib(advisory.metal_recommended_working_set_bytes)}; Metal-Spielraum vor Modellladen ${gib(advisory.metal_available_working_set_bytes)} (kein freier RAM)`);
    if(advisory.metal_pressure===true)parts.push('GPU-Speicher knapp (Schätzung): Der geschätzte Bedarf liegt über dem empfohlenen Metal-Spielraum; Rendering kann langsamer sein.');
    parts.push('Dynamischer Spitzenbedarf ist unbekannt.');
    return parts.join(' · ');
  }
  function refreshProgress() {
    const jobProgress=root?.querySelector('[data-job-progress]');
    if(jobProgress)jobProgress.textContent=state.job?.progress?.detail?` · ${state.job.progress.detail}`:'';
    const memory=root?.querySelector('[data-memory-advisory]');
    if(memory){memory.textContent=memoryAdvisoryText();memory.hidden=!state.job?.memory_advisory;}
    const activeProgress=root?.querySelector('.pr-rendering');
    if(activeProgress){const spinner=activeProgress.querySelector('.spinner');activeProgress.replaceChildren(...(spinner?[spinner,document.createTextNode(progressLabel(state.job?.progress))]:[document.createTextNode(progressLabel(state.job?.progress))]));}
  }
  function rowDisclosure(id, section, label, content, extraClass = '') {
    if (!content) return '';
    const key = `${id}:${section}`;
    return `<details class="pr-row-disclosure ${extraClass}" data-row-disclosure="${esc(key)}" ${expandedRowSections.has(key)?'open':''}><summary>${esc(label)}</summary>${content}</details>`;
  }
  function fixedSeedMarkup(row) {
    if(row.generation_mode==='copy_original')return '<span class="pr-seed-value">Originalton · kein Syntheseseed</span>';
    const accepted=row.accepted_seed,recipe=row.recipe_seed,current=row.current?.preset?.seed;
    const seed=Number.isInteger(recipe)?recipe:Number.isInteger(accepted)?accepted:current;
    const label=Number.isInteger(recipe)&&recipe!==accepted?'Rezept-Seed':Number.isInteger(accepted)?'Festgehaltener Seed':'Vorgegebener Seed';
    return `<span class="pr-seed-value"><b>${label}: ${Number.isInteger(seed)?esc(seed):'noch keine Fassung ausgewählt'}</b>${Number.isInteger(accepted)&&seed!==accepted?`<span class="muted">Abgenommene Fassung: Seed ${esc(accepted)}</span>`:''}</span>`;
  }
  function candidateMarkup(row,candidate,previewable=false,allowRelease=true,historical=false) {
    const warn=Boolean(candidate.warning),verified=candidate.verified===true&&Boolean(candidate.audio_url);
    const seed=Number.isInteger(candidate.preset?.seed)?`Seed ${candidate.preset.seed}`:'Seed nicht belegt';
    const title=previewable?'Neueste Vorschau · noch nicht gut bestätigt':historical?'Frühere Fassung':'Vorschau · noch nicht gut bestätigt';
    const adopt=allowRelease&&verified&&candidate.id!=='original';
    const label=previewable?'Vorschau als gut bestätigen':'Diese Fassung als gut bestätigen';
    const controls=adopt?`<div class="controls"><button data-release="${esc(row.dialog_id)}" data-candidate="${esc(candidate.id)}" class="pr-confirm-good">${label}</button></div>`:'';
    return `<div class="pr-output ${previewable?'pr-latest-output':'pr-older-output'}" data-output="${esc(candidate.id)}"><div class="pr-output-title">${title} · ${esc(seed)} · ${esc(reviewLabel(candidate))}</div>${verified?`<section class="pr-audio-player pr-preview-audio">${audioPlayerSlotMarkup(`variant-${row.dialog_id}-${candidate.id||candidate.audio_url}`,candidate.audio_url,hearingContext(row,previewable?'Vorschau':'Frühere Fassung',candidate),originalOutput(candidate)?'original':'preview',row.dialog_id,candidate.id,previewable?`data-preview="${esc(row.dialog_id)}" data-candidate="${esc(candidate.id)}"`:'')}</section>`:'<p class="pr-experimental">Diese Aufnahme fehlt oder konnte nicht vollständig geprüft werden. Du kannst sie deshalb noch nicht übernehmen.</p>'}${warn?findingList(previewWarningReasons(candidate)):''}<details class="pr-history pr-version-evidence"><summary>Text, Spracherkennung und Einstellungen</summary>${outputDetailsMarkup(candidate,!warn)}</details>${controls}</div>`;
  }
  function rowMarkup(row) {
    const id = String(row.dialog_id), current = row.current, view=draftFor(row), stateClass=classify(row,view.generation_mode);
    const latestCandidate=latestOpenCandidate(row), displayedOutput=current;
    const unresolvedCandidate=reviewCandidate(row),plannedOriginal=view.generation_mode==='copy_original'&&current?.id!=='original',originalUnavailable=plannedOriginal&&!originalChoiceValid(row);
    const noteFromEarlierAudio=Boolean(row.note&&row.note_binding?.audio?.fingerprint&&row.note_binding.audio.fingerprint!==displayedOutput?.binding?.fingerprint);
    const action=pendingAction(row);
    const activeOutput=state.job?.status==='running'&&String(state.job.active_dialog_id||'')===id;

    const latestWarning=Boolean(unresolvedCandidate&&(unresolvedCandidate.warning||unresolvedCandidate.verified!==true));
    const confirmedCurrent=manuallyConfirmed(row);
    const rowFindings=[...new Set(latestJobError(row)?[latestJobError(row)]:plannedOriginal?(originalUnavailable?['Der Originalton fehlt oder konnte nicht vollständig geprüft werden. Du kannst ihn deshalb noch nicht verwenden.']:[]):current?.id==='original'&&current?.verified===true?[]:manuallyConfirmed(row)?[]:[...(row.findings||[]),...(current?.findings||[])])].filter(item=>{
      const cause=displayedOutput?.stt?.cause?.trim();
      if(cause&&(item===cause||item.includes(cause)))return false;
      return !(displayedOutput?.stt?.status==='deferred'&&/^STT zurückgestellt\b/i.test(item));
    });
    const originalAudio=row.original_audio_url?`<section class="pr-original-audio"><h4>Englische Sprachausgabe</h4>${audioPlayerSlotMarkup(`original-${id}`,row.original_audio_url,hearingContext(row,'Englische Sprachausgabe'),'original',id,'original')}</section>`:'';
    const preset=view.next_preset||{};
    const copied=['copy_original','original','no_render'].includes(row.processing)||current?.processing==='copy_original';
    const currentLabel=plannedOriginal?'Bisherige Ausgabe':copied?'Englische Sprachausgabe':`Ausgewählte Sprachausgabe${Number.isInteger(current?.preset?.seed)?` · Seed ${current.preset.seed}`:''}`;
    const earlierGermanEvidence=plannedOriginal&&current?`<details class="pr-history pr-previous-german"><summary>Vorherige deutsche Ausgabe · QA/STT-Befunde</summary>${outputDetailsMarkup(current,true)}</details>`:'';
    const currentDetails=`<p class="pr-stt"><b>Technische Dialogkennung:</b> ${esc(id)}</p>`+(plannedOriginal?'':copied?`<div class="pr-output-details">${current?.tts_text!=null?`<p class="pr-stt"><b>Englischer Untertitel:</b> ${esc(current.tts_text)}</p>`:''}${sttMarkup({stt:{status:'not_required'}})}</div>`:`${outputDetailsMarkup(displayedOutput,false)}${displayedOutput?.qa_expected_text!=null&&displayedOutput.qa_expected_text!==displayedOutput.tts_text?`<p class="pr-expected"><b>QA-Solltext:</b> ${esc(displayedOutput.qa_expected_text)}</p>`:''}`);
    const originalMode=view.generation_mode==='copy_original';
    const effectiveBackend=view.backend||batchBackend||state.backends?.[0]||'gpu';
    const renderAvailability=state.render_availability||{};
    const rendererAvailable=renderAvailability.available??(state.render_enabled===true);
    const rendererBusy=Boolean(renderAvailability.busy||state.job?.status==='running');
    const canAudition=!originalMode&&rendererAvailable&&!rendererBusy;
    const acceptedProbe=row.accepted_probe;
    const probeReusable=canReuseAccepted(row);
    const probeStatus=acceptedProbe?`<p class="pr-probe-status ${probeReusable?'pr-probe-reusable':'pr-probe-stale'}" data-probe-status="${esc(id)}">${probeReusable?'Die übernommene Vorschau passt zu deinen Einstellungen. Beim Verarbeiten wird diese Aufnahme wiederverwendet.':'Deine Einstellungen haben sich seit der Übernahme geändert. Beim Verarbeiten wird eine neue Aufnahme erzeugt.'}</p>`:'';
    const cpuAudition=effectiveBackend==='cpu';
    const rowIsRunning=state.job?.status==='running'&&jobHasDialog(id)&&!jobHasCompletedDialog(id);
    const rowIsStarting=auditionStartingId===id;
    const rowIsRendering=activeOutput;
    const cpuNeedsConfirmation=cpuAudition&&!auditionCpuConfirmed.has(id);
    const auditionEnabled=canAudition&&!cpuNeedsConfirmation&&!busy;
    const auditionReason=rendererBusy?(activeOutput?'Dieser Dialog wird gerade gerendert.':rowIsRunning?'Dieser Dialog wartet im aktiven Auftrag.':'Ein Renderauftrag läuft bereits; starte die Vorschau nach dessen Ende.') : !rendererAvailable?'' : cpuNeedsConfirmation?'CPU ist ungetestet. Bestätige unten ausdrücklich, bevor ein CPU-Vorhörversuch startet.':'';
    const progress=state.job?.progress;
    const progressText=progressLabel(progress);
    const queueEnabled=state.audition_queue_enabled===true, queued=(state.audition_queue||[]).map(String).includes(id);
    const auditionControls=originalMode?'':`<div class="pr-audition-controls">${fixedSeedMarkup(row)}${queueEnabled?`<button data-audition-queue="${esc(id)}" ${rendererBusy||busy?'disabled':''}>${queued?'Aus Warteliste entfernen':'Zur Warteliste hinzufügen'}</button>`:''}<button data-audition="${esc(id)}" ${auditionEnabled?'':'disabled'}>${rowIsStarting?'Auftrag startet …':rowIsRendering?'Rendering läuft …':'Vorschau mit gleichem Seed'}</button><button data-audition="${esc(id)}" data-new-seed="true" ${auditionEnabled?'':'disabled'}>Vorschau mit neuem Seed</button>${rowIsStarting?'<span class="pr-rendering" role="status" aria-live="polite"><span class="spinner" aria-hidden="true"></span>Renderauftrag wird gestartet …</span>':rowIsRendering?`<span class="pr-rendering" role="status" aria-live="polite"><span class="spinner" aria-hidden="true"></span>${esc(progressText)}</span>`:''}${cpuAudition?`<label class="pr-check pr-audition-cpu"><input type="checkbox" data-audition-cpu="${esc(id)}" ${auditionCpuConfirmed.has(id)?'checked':''}> CPU für dieses Vorhören ungetestet bestätigen</label><span class="pr-experimental">CPU gilt für diesen Dialog, auch außerhalb der Auswahl.</span>`:''}${auditionReason?`<span class="pr-audition-reason" role="status">${esc(auditionReason)}</span>`:''}</div>`;
    const pendingLabel=stateClass.render&&action==='copy_original'?'Verarbeite diesen Dialog, um den Originalton als aktuelle Ausgabe auszuwählen.':stateClass.render&&action==='render'?'Deine Änderungen sind vorgemerkt. Erzeuge eine Vorschau zum Vergleichen oder verarbeite diesen Dialog.':'';
    const previous=row.previous_selection;
    const previousLabel=previous?(Number.isInteger(previous.seed)?`Vorige Auswahl: Seed ${previous.seed}`:'Vorige Auswahl: Originalton'):'Keine vorherige Auswahl gespeichert.';
    const restoreAction=(row.good||previous)?`<span class="pr-restore-selection"><button data-restore-previous="${esc(id)}" ${previous&&!busy?'':'disabled'} title="${esc(previousLabel)}">Zurück zur letzten Version</button><small class="muted">${esc(previousLabel)}</small></span>`:'';
    const goodAction=plannedOriginal?'':`${!manuallyConfirmed(row)&&!row.automatically_repaired?`<button data-good="${esc(id)}" data-good-value="true" class="pr-confirm-good" ${current?.verified&&!busy?'':'disabled'}>Aktuelle Ausgabe als gut bestätigen</button>`:''}${restoreAction}`;

    const originalTextMarkup = `<div class="pr-original-text"><p><b>Englischer Untertitel:</b> ${esc(text(row.original_text))}</p>${row.display_text?`<p><b>Deutscher Untertitel:</b> ${esc(row.display_text)}</p>`:''}</div>`;
    const noteEditor = rowDisclosure(id, 'note', `${noteFromEarlierAudio?'Notiz zur früheren Fassung':'Notiz'}${view.note?' · vorhanden':''}`, `<label class="pr-note">${noteFromEarlierAudio?'<span class="pr-experimental">Notiz zur früheren Audiofassung</span>':'Notiz'}<textarea rows="2" maxlength="2000" data-note="${esc(id)}">${esc(view.note)}</textarea></label>`, 'pr-note-editor');
    const currentInspection = rowDisclosure(id, 'inspection', `Prüfdetails · ${sttNames[displayedOutput?.stt?.status]||'STT-Status offen'}`, currentDetails, 'pr-current-inspection');
    const visibleFindings = [...rowFindings];
    const sttCause = displayedOutput?.stt?.cause?.trim();
    if (sttCause && ['failed','absent','invalid','deferred'].includes(displayedOutput.stt.status) && !visibleFindings.includes(sttCause)) visibleFindings.push(sttCause);
    return `<article class="dialogue-row" data-row="${esc(id)}"><div class="pr-row-main"><div class="pr-row-top"><label class="pr-row-select" title="Dialog wählen"><input type="checkbox" data-select="${esc(id)}" ${selected.has(id)?'checked':''}><span class="pr-sr-only">Dialog wählen</span></label><h3>Dialog ${esc(id.split(':').at(-1))}${row.role_name?` · ${esc(row.role_name)}`:''}</h3>${unresolvedCandidate?`<span class="pr-status pr-status-candidate${latestWarning?' pr-status-warning':''}">${confirmedCurrent?'Andere':'Neue'} Vorschau${activeOutput&&state.job?.intent==='audition'?' · läuft':latestWarning?' · Warnungen':''}</span>`:activeOutput&&state.job?.intent==='audition'?'<span class="pr-status pr-status-candidate">Andere Vorschau · läuft</span>':''}<span class="pr-tags">${categoriesFor(row)}</span>${currentAudioStatusMarkup(row)}</div><div class="pr-row-body"><section class="pr-row-edit">${originalTextMarkup}<div class="pr-edit"><label>Sprachausgabetext für den nächsten Versuch<textarea rows="2" maxlength="4000" data-tts="${esc(id)}">${esc(view.tts_text)}</textarea></label><div class="pr-edit-tools">${noteEditor}${rowSaveMarkup(id)}</div></div>${row.note_history?.length?`<details class="pr-history pr-note-history"><summary>Notizverlauf · ${row.note_history.length}</summary>${row.note_history.map(item=>`<p>${esc(item.note)} · ${item.binding?.wav_sha256?`WAV ${esc(item.binding.wav_sha256.slice(0,12))}…`:'Fassung unbekannt'}</p>`).join('')}</details>`:''}</section><section class="pr-row-evidence">${visibleFindings.length?findingList(visibleFindings):''}<div class="pr-media pr-audio-players">${originalAudio}${audioPlayerMarkup(currentLabel,displayedOutput,'current',`main-${id}-${displayedOutput?.id||displayedOutput?.audio_url||'empty'}`,row)}</div>${currentInspection}${goodAction?`<div class="pr-selection-controls">${goodAction}</div>`:''}</section></div><div class="pr-row-actions"><div class="pr-settings-controls"><div class="pr-methods"><label><a href="#launcher.anchor" data-help-open="launcher.anchor">Stimmanker</a><select data-anchor="${esc(id)}">${[['keep','Beibehalten'],['on','An'],['off','Aus']].map(([value,name])=>`<option value="${value}" ${preset.anchor===value?'selected':''}>${name}</option>`).join('')}</select></label><label><a href="#review.pronunciation" data-help-open="review.pronunciation">Prewarm</a><select data-prewarm="${esc(id)}">${[['keep','Beibehalten'],['on','An'],['off','Aus']].map(([value,name])=>`<option value="${value}" ${preset.prewarm===value?'selected':''}>${name}</option>`).join('')}</select></label><label>Rendermodus<select data-backend="${esc(id)}"><option value="" ${!view.backend?'selected':''}>Wie Auswahl</option>${(state.backends||[]).map(value=>`<option value="${esc(value)}" ${view.backend===value?'selected':''}>${esc(value.toUpperCase())}${value==='cpu'?' · experimentell':''}</option>`).join('')}</select></label><label>Ausgabe<select data-generation-mode="${esc(id)}"><option value="synthesize" ${!originalMode?'selected':''}>Deutsch</option><option value="copy_original" ${originalMode?'selected':''} ${row.original_release_enabled!==true?'disabled':''}>Originalton verwenden</option></select></label></div>${view.backend==='cpu'&&action==='render'?`<p class="pr-experimental">CPU ist ungetestet und gilt für diesen Dialog.</p>`:''}${originalUnavailable?'<p class="pr-experimental">Der Originalton fehlt oder konnte nicht vollständig geprüft werden. Du kannst ihn deshalb noch nicht verwenden.</p>':''}${pendingLabel?`<p class="pr-original-mode">${pendingLabel}</p>`:''}</div><div class="pr-preview-controls">${auditionControls}${probeStatus}</div></div>${latestCandidate?`<section class="pr-row-preview">${candidateMarkup(row,latestCandidate,true,true)}</section>`:''}${earlierGermanEvidence}${historyMarkup(row,latestCandidate)}</div></article>`;
  }
  function matches(row) {
    const q = normalizedQuery;
    if (q && !searchText(row).includes(q)) return false;
    if(worklistFilter!=='all'&&worklistFor(row)!==worklistFilter)return false;
    if(statusFilter==='unaccepted'&&!activeReviewCandidates(row).length)return false;
    const categories = new Set(categoryIdsFor(row));
    if (chosenCategories.size && ![...chosenCategories].some(id=>categories.has(id))) return false;
    const classification=classify(row);
    if (statusFilter==='good' && !classification.good) return false;
    if (statusFilter==='warnings' && (!classification.warning||classification.render)) return false;
    if (statusFilter==='render' && !classification.render) return false;
    return true;
  }
  function buildStartDisabled(gameRoot) {
    const build=state.patch_build||{};
    const ready=build.available||(build.audio_available&&build.source_required&&build.ffmpeg_available&&String(gameRoot??'').trim());
    return readOnly()||completionLoading||exportChoice||busy||build.busy||build.job?.status==='running'||state.job?.status==='running'||!ready;
  }
  function buildMarkup() { return ''; }
  function selectedOutputRoot() { return String(buildOutputRootEdited?buildOutputRoot:state.patch_build?.output_root||'').trim(); }
  function successfulBuildVisible() {
    const job=state.patch_build?.job||{};
    return job.status==='completed'&&(job.id||job.output_dir)!==dismissedBuild&&
      (!Number.isInteger(job.review_revision)||job.review_revision===state.revision);
  }
  function completionMarkup() {
    const build=state.patch_build||{},job=build.job||{},running=job.status==='running';
    const gameRoot=buildGameRootEdited?buildGameRoot:build.game_root??'';
    const total=Number(job.total)||Number(build.total_dialogues)||0,completed=Number(job.completed)||0;
    const progress=running?`<progress max="${total||1}" value="${completed}">${completed} / ${total}</progress><p>${completed} / ${total} Dialoge · ${total?Math.min(100,Math.round(completed/total*100)):0}%</p>`:'';
    const report=job.report_path?`<p><a href="${esc(apiBase)}/build-report" download="patch-build-report.json">Erstellungsbericht herunterladen</a></p>`:'';
    if(successfulBuildVisible())return `<section class="pr-build-success" aria-labelledby="pr-success-title" role="status"><span class="pr-success-mark" aria-hidden="true">✓</span><h2 id="pr-success-title">Patch erfolgreich erstellt</h2><p>Dein fertiger Spielordner enthält die ausgewählten Aufnahmen aller Dialoge.</p><p>Füge diesen Ordner in ScummVM als Spiel hinzu und wähle in den Spieleinstellungen <b>Deutsch</b> als Sprache.</p><p>Fertiger Spielordner: <code>${esc(job.output_dir)}</code></p>${report}<p class="muted">Zum Spielen benötigst du <a href="https://www.scummvm.org/" target="_blank" rel="noopener noreferrer">ScummVM</a>.</p></section>`;
    const outputs=build.existing_outputs||[];
    const blocker=!build.available&&!running&&build.reason&&(!build.source_required||!/Spielbasis|Spielordner|Vorbereitungsbeleg/i.test(build.reason))?String(build.reason).replace('Lokales FFmpeg fehlt.','Ein Audiowerkzeug fehlt. Gehe zur Einrichtung zurück und richte die Hilfsprogramme ein.').replace('Lokales ScummRP für den Standard-Intro-Patch fehlt; Runtime einrichten.','Ein Hilfsprogramm für das Intro fehlt. Gehe zur Einrichtung zurück und richte die Hilfsprogramme ein.').replace('Ein Renderauftrag läuft; Patchbuild erst nach Abschluss starten.','Die Aufnahmen werden noch verarbeitet. Warte bis zum Abschluss, bevor du den Spielordner erstellst.').replace('Ein Patchbuild läuft bereits.','Der Spielordner wird bereits erstellt.'):'';
    return `<h2>Patch fertigstellen</h2><p>Erstelle einen Spielordner mit den ausgewählten Aufnahmen. Du kannst ihn anschließend in ScummVM spielen. <a href="#review.build" data-help-open="review.build">Hilfe zur Erstellung</a></p><section class="pr-build" aria-labelledby="pr-build-title"><h3 id="pr-build-title">Spielordner erstellen</h3><p role="status"><b>Aufnahmen bereit: ${esc(build.available_dialogues??0)} / ${esc(build.total_dialogues??state.rows.length)}</b>${blocker?`<br>${esc(blocker)}`:''}</p>${build.source_required?`<p>Der vorbereitete Spielordner wurde nicht gefunden. Wähle den deutschen Spielordner der Ultimate Talkie Edition 1.02. <a href="#review.build" data-help-open="review.build">Hilfe zur Spielquelle</a></p><div class="pr-path-field"><label for="pr-build-game-root">Vorbereiteter deutscher Spielordner<input id="pr-build-game-root" type="text" value="${esc(gameRoot)}" spellcheck="false" autocomplete="off" placeholder="Pfad zum Spielordner" ${running||busy||readOnly()?'disabled':''}></label><button id="pr-choose-game-root" type="button" ${running||busy||readOnly()?'disabled':''}>Ordner wählen …</button></div>`:''}<div class="pr-path-field"><label for="pr-build-output-root">Speicherort für den fertigen Spielordner<input id="pr-build-output-root" type="text" value="${esc(selectedOutputRoot())}" spellcheck="false" autocomplete="off" ${running||busy||readOnly()?'disabled':''}></label><button id="pr-choose-output-root" type="button" ${running||busy||readOnly()?'disabled':''}>Ordner wählen …</button></div><p class="muted">Neuer Spielordner: <span id="pr-target-name">${esc(build.target_name||'Monkey Island Deutsch – Datum und Uhrzeit')}</span></p>${outputs.length?`<p>Hier ${outputs.length===1?'liegt bereits ein erstellter Spielordner':`liegen bereits ${outputs.length} erstellte Spielordner`}. Du kannst einen weiteren erstellen oder einen vorhandenen ersetzen.</p>`:''}${buildTargetError?`<p class="pr-error">${esc(buildTargetError)}</p>`:''}</section>${running||job.error||report?`<section class="pr-build-status" role="status" aria-live="polite"><h3>${running?'Spielordner wird erstellt …':job.error?'Erstellung fehlgeschlagen':'Vorherige Erstellung'}</h3>${progress}${job.error?`<p class="pr-error">${esc(job.error)}</p>`:''}${report}</section>`:''}${exportChoice?exportChoiceMarkup(outputs):''}`;
  }
  function exportChoiceMarkup(outputs) {
    const chosen=state.patch_build?.last_output?.path;
    return `<div class="pr-modal-backdrop"><section class="pr-export-dialog" role="dialog" aria-modal="true" aria-labelledby="pr-export-dialog-title" tabindex="-1"><h2 id="pr-export-dialog-title">Spielordner vorhanden</h2><p>Erstelle einen weiteren Spielordner oder ersetze einen zuvor mit dem Patch erstellten Ordner. Der bisherige Ordner bleibt erhalten, bis der neue fertig ist.</p><label for="pr-replace-output">Vorhandener Spielordner<select id="pr-replace-output">${outputs.map(output=>`<option value="${esc(output.path)}" ${output.path===chosen?'selected':''}>${esc(output.name)}</option>`).join('')}</select></label><div class="pr-dialog-actions"><button type="button" id="pr-export-cancel">Abbrechen</button><button type="button" id="pr-export-replace">Ausgewählten Spielordner ersetzen</button><button type="button" id="pr-export-new" class="primary">Weiteren Spielordner erstellen</button></div></section></div>`;
  }
  async function fetchBuildStatus() {
    const destination=selectedOutputRoot(),serial=++targetRequest;
    const status=await request('/build-status'+(destination?'?'+new URLSearchParams({output_root:destination}):''));
    if(serial===targetRequest){state.patch_build=status;buildTargetError='';}
    return status;
  }
  async function refreshExportTarget() {
    try{await fetchBuildStatus();}catch(error){buildTargetError=error.message;}
    render();
  }
  async function openCompletion() {
    if(completionLoading||readOnly())return;
    captureDrafts();completionView=true;completionLoading=true;exportChoice=false;
    setMessage('Spielordner und Aufnahmen werden geprüft …');render();
    try { const status=await request('/status');applyStatus(status);await fetchBuildStatus(); }
    catch(error) { setMessage(`Die Angaben zur Erstellung konnten nicht geladen werden: ${error.message}`,true); }
    finally { completionLoading=false;if(!messageError)setMessage(''); }
    render();
    window.scrollTo?.(0,0);
  }
  function pickDirectory(kind) {
    const bridge=nativeBridge();
    if(!bridge?.postMessage){setMessage('Trage den gewünschten Ordnerpfad in das Pfadfeld ein.',true);render();return;}
    const requestId=`mi1-review-${Date.now()}-${++directoryRequest}`;
    directoryRequests.set(requestId,kind);
    bridge.postMessage({action:'chooseDirectory',requestId,kind});
  }
  window.MI1NativeDirectoryPicked=(requestId,path)=>{
    const kind=directoryRequests.get(String(requestId));directoryRequests.delete(String(requestId));
    if(!kind||!path||readOnly())return;
    if(kind==='export-target'){
      buildOutputRoot=String(path);buildOutputRootEdited=true;
      const input=root.querySelector('#pr-build-output-root');if(input)input.value=buildOutputRoot;
      return refreshExportTarget();
    }else{
      buildGameRoot=String(path);buildGameRootEdited=true;
      const input=root.querySelector('#pr-build-game-root');if(input)input.value=buildGameRoot;
      render();
    }
  };
  async function startExport(policy,replaceOutput) {
    if(readOnly())return;
    exportChoice=false;busy=true;render();
    try {
      await flushAllRowUpdates();
      const build=state.patch_build||{};
      const payload={revision:state.revision,output_root:selectedOutputRoot(),output_policy:policy,
        ...(replaceOutput?{replace_output:replaceOutput}:{}),
        ...(build.source_required&&buildGameRoot?.trim()?{game_root:buildGameRoot.trim()}:{})};
      const body=await post('/build',payload);state.patch_build=body.patch_build||body;
      setMessage('Deine Spielfassung wird jetzt erstellt.');
      const status=await request('/status');applyStatus(status);
    }catch(error){setMessage(error.message,true);if(error.status===409)await refreshRows().catch(()=>undefined);}
    finally{busy=false;render();schedulePoll();}
  }
  function quitApp() {
    const bridge=nativeBridge();
    if(!bridge?.postMessage)throw new Error('Schließe dieses Browserfenster, um den Patch zu verlassen.');
    bridge.postMessage({action:'quitApp'});
  }
  function updateActionFooter() {
    const patch=state.mode==='patch',success=completionView&&successfulBuildVisible();
    const summary=document.querySelector('#pr-summary');if(summary)summary.hidden=completionView;
    const footer=document.querySelector('#pr-action-footer');if(footer)footer.hidden=!patch;
    for(const [id,visible] of [['pr-review-actions',!completionView],['pr-completion-actions',completionView&&!success],['pr-success-actions',success]]){
      const group=document.querySelector('#'+id);if(group)group.hidden=!patch||!visible;
    }
    const finish=document.querySelector('#pr-finish-global');if(finish){finish.hidden=!patch||completionView;finish.disabled=readOnly();}
    const back=document.querySelector('#pr-back-render');if(back)back.hidden=completionView||!root.hasAttribute?.('data-launcher-review');
    const buildButton=control('#pr-build-start');if(buildButton)buildButton.disabled=buildStartDisabled(buildGameRootEdited?buildGameRoot:state.patch_build?.game_root);
    const goBack=()=>{const job=state.patch_build?.job||{};if(job.status==='completed')dismissedBuild=job.id||job.output_dir;exportChoice=false;completionView=false;setMessage('');render();};
    for(const id of ['#pr-back-review','#pr-success-back-review']){const button=control(id);if(button)button.onclick=goBack;}
    const quit=control('#pr-quit-app');if(quit)quit.onclick=()=>{try{quitApp();}catch(error){setMessage(error.message,true);render();}};
    const open=control('#pr-open-output-quit');if(open)open.onclick=async()=>{
      open.disabled=true;
      try{await post('/open-output',{});quitApp();}
      catch(error){setMessage(error.message,true);open.disabled=false;render();}
    };
  }
  function applyReadOnlyControls() {
    if(!readOnly())return;
    const selectors=['[data-tts]','[data-note]','[data-backend]','[data-anchor]','[data-prewarm]','[data-generation-mode]',
      '[data-audition]','[data-new-seed]','[data-select]','[data-audition-queue]','[data-audition-cpu]','[data-release]','[data-ignore]','[data-good]',
      '[data-restore-previous]','[data-queue-remove]','[data-queue-clear]','#pr-start','#pr-audition-queue-start',
      '#pr-select-matches','#pr-clear-selection','#pr-export-recipe','#pr-export','#pr-import','#pr-batch-backend','#pr-confirm-cpu'];
    for(const selector of selectors)for(const node of root.querySelectorAll(selector))node.disabled=true;
  }
  function bindBuildControls() {
    const source=root.querySelector('#pr-build-game-root');
    if(source)source.oninput=()=>{buildGameRoot=source.value;buildGameRootEdited=true;updateActionFooter();};
    const destination=root.querySelector('#pr-build-output-root');
    if(destination){destination.oninput=()=>{buildOutputRoot=destination.value;buildOutputRootEdited=true;};destination.onchange=()=>refreshExportTarget();}
    const chooseDestination=root.querySelector('#pr-choose-output-root');if(chooseDestination)chooseDestination.onclick=()=>pickDirectory('export-target');
    const chooseSource=root.querySelector('#pr-choose-game-root');if(chooseSource)chooseSource.onclick=()=>pickDirectory('game-source');
    const button=control('#pr-build-start');if(button)button.onclick=async()=>{
      if(readOnly()||busy||completionLoading)return;
      busy=true;render();
      try{
        const status=await fetchBuildStatus();
        busy=false;
        if(status.existing_outputs?.length){exportChoice=true;render();root.querySelector('.pr-export-dialog')?.focus?.();}
        else await startExport('new');
      }catch(error){busy=false;setMessage(error.message,true);render();}
    };
    const cancel=root.querySelector('#pr-export-cancel');if(cancel)cancel.onclick=()=>{exportChoice=false;render();control('#pr-build-start')?.focus?.();};
    const create=root.querySelector('#pr-export-new');if(create)create.onclick=()=>startExport('new');
    const replace=root.querySelector('#pr-export-replace');if(replace)replace.onclick=()=>startExport('replace',root.querySelector('#pr-replace-output')?.value);
    const dialog=root.querySelector('.pr-export-dialog');if(dialog)dialog.onkeydown=event=>{
      if(event.key==='Escape'){event.preventDefault();exportChoice=false;render();control('#pr-build-start')?.focus?.();}
      if(event.key==='Tab'){
        const controls=[...dialog.querySelectorAll('button,select')],first=controls[0],last=controls[controls.length-1];
        if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}
        else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}
      }
    };
  }

  function render(options = {}) {
    const diagnosticRun=uiDiagCollecting()?uiDiagRun:null,startedAt=diagnosticRun?uiDiagNow():0;
    renderWorklists=new Map();renderJobIds=new Set((state?.job?.dialog_ids||[]).map(String));renderCompletedJobIds=new Set((state?.job?.completed||[]).map(String));renderReusedJobIds=new Set((state?.job?.reused||[]).map(String));normalizedQuery=query.trim().toLocaleLowerCase('de');
    try { const result=renderImpl(options);updateActionFooter();applyReadOnlyControls();updateAudioButtons();return result; }
    finally { renderWorklists=null;renderJobIds=null;renderCompletedJobIds=null;renderReusedJobIds=null;uiDiagRecordDuration(diagnosticRun,'renders','renderTotalMs','renderMaxMs',startedAt); }
  }
  function renderImpl(options = {}) {
    if (!root || !state) return;
    if (options.captureDrafts !== false) captureDrafts();
    for (const node of root.querySelectorAll('[data-row-disclosure]')) {
      if (node.open) expandedRowSections.add(node.dataset.rowDisclosure);
      else expandedRowSections.delete(node.dataset.rowDisclosure);
    }
    if(completionView) {
      clearReviewAudio();
      root.innerHTML=`${message?`<p id="pr-message" class="${messageError?'pr-error':'pr-job'}" role="status">${esc(message)}${messageRowId?` <button type="button" id="pr-show-confirmed" data-confirmed-row="${esc(messageRowId)}">Anzeigen</button>`:''}</p>`:''}${completionMarkup()}`;
      bindBuildControls();
    reportPreviewWarnings();
      return;
    }
    const active=root.contains(document.activeElement)?document.activeElement:null;
    const focusId=active?.id||'',focusData=active?Object.entries(active.dataset||{}).find(([key])=>key!==''):null;
    const selection=active&&typeof active.selectionStart==='number'?[active.selectionStart,active.selectionEnd]:null;
    const categories = state.categories || Object.entries(categoryNames).map(([id,label])=>({id,label}));
    const rows = state.rows.filter(matches);
    if(sortOrder!=='manifest'){
      const order=new Map(state.rows.map((row,index)=>[row,index]));
      const counts=new Map(rows.map(row=>[row,displayedFindingCount(row)]));
      rows.sort((left,right)=>{
        const delta=counts.get(left)-counts.get(right);
        return (sortOrder==='most'?-delta:delta)||(order.get(left)-order.get(right));
      });
    }
    const pages = Math.max(1,Math.ceil(rows.length/pageSize)); page=Math.min(page,pages-1);
    const shown=rows.slice(page*pageSize,(page+1)*pageSize);
    const pinnedAuditionId=auditionPinSuppressed?null:auditionPinnedId||auditionStartingId;
    const pinnedAuditionRow=pinnedAuditionId?rowById(pinnedAuditionId):null;
    const pinnedAuditionDetached=Boolean(pinnedAuditionRow&&!shown.some(row=>String(row.dialog_id)===String(pinnedAuditionId)));
    const visibleRows=pinnedAuditionDetached?[pinnedAuditionRow,...shown]:shown;
    let pinnedViewport=null;
    if(pinnedAuditionId){
      const oldPinnedNode=root.querySelector(`[data-row="${CSS.escape(String(pinnedAuditionId))}"]`);
      if(oldPinnedNode){
        const rect=oldPinnedNode.getBoundingClientRect();let container=oldPinnedNode.parentElement;
        while(container&&container!==document.body){const overflow=window.getComputedStyle(container).overflowY;if(/auto|scroll|overlay/.test(overflow)&&container.scrollHeight>container.clientHeight)break;container=container.parentElement;}
        const pageScroll=!container||container===document.body||container===document.documentElement||container===document.scrollingElement;
        const scrollTarget=pageScroll?(document.scrollingElement||document.documentElement):container;
        const containerBounds=pageScroll?{top:0,bottom:window.innerHeight}:container.getBoundingClientRect();
        const bounds={top:Math.max(0,containerBounds.top),bottom:Math.min(window.innerHeight,containerBounds.bottom)};
        if(rect.bottom>bounds.top&&rect.top<bounds.bottom)pinnedViewport={top:rect.top,scrollTarget};
      }
    }
    const batchInfo=batchSelection(state.rows),eligibleActions=batchInfo.eligible;
    const hasSynthesisRequest=eligibleActions.some(({row,action})=>action==='render'||canReuseAccepted(row));
    const hasSynthesis=eligibleActions.some(({row,action})=>action==='render'&&!canReuseAccepted(row));
    const cpuSelected=eligibleActions.some(({row,action})=>action==='render'&&!canReuseAccepted(row)&&(draftFor(row).backend||batchBackend)==='cpu');
    const jobRunning=state.job?.status==='running',buildRunning=state.patch_build?.job?.status==='running';
    const auditionQueueIds=(state.audition_queue||[]).map(String),visibleAuditionQueueIds=auditionQueueIds.slice(0,8),hiddenAuditionQueueCount=Math.max(0,auditionQueueIds.length-visibleAuditionQueueIds.length);
    const disabled=batchStartDisabled(eligibleActions.length,hasSynthesis,cpuSelected);
    const showSynthesisOptions=!selected.size||hasSynthesisRequest;
    const jobIntent=state.job?.intent==='audition'?(state.job?.audition_queue===true||(state.job?.dialog_ids||[]).length>1?'Vorschau-Warteliste':'Einzelprobe'):'Gesamtlauf';
    const reusedIds=state.job?.reused||[];
    const worklistCounts={todo:0,manual_confirmed:0,auto_clean:0,auto_repaired:0};
    for(const row of state.rows)worklistCounts[worklistFor(row)]++;
    root.innerHTML = `${message?`<p id="pr-message" class="${messageError?'pr-error':'pr-job'}" role="status">${esc(message)}${messageRowId?` <button type="button" id="pr-show-confirmed" data-confirmed-row="${esc(messageRowId)}">Anzeigen</button>`:''}</p>`:''}${readOnly()?`<p class="pr-read-only" role="status">${esc(state.read_only_reason||'Diese Fassung ist während des laufenden Auftrags zum Anhören geöffnet. Änderungen und die Erstellung des Spielordners sind vorübergehend gesperrt.')}</p>`:''}<h2>Dialoge prüfen und korrigieren</h2><p>Beginne unter „Offen“ mit dem Filter „Warnungen“. Höre die Sprachausgabe an und vergleiche sie mit dem deutschen Untertitel. Wenn etwas nicht passt, ändere den Sprachausgabetext oder die Einstellungen und erzeuge eine neue Vorschau. Höre sie vollständig an und wähle „Vorschau als gut bestätigen“, wenn du sie für das Spiel verwenden möchtest. <a href="#review.workflow" data-help-open="review.workflow">So prüfst du die Dialoge</a></p><p>Alle Änderungen werden automatisch gespeichert. Wenn du den Patch später mit demselben Arbeitsordner öffnest und diese Fassung auswählst, werden deine Änderungen automatisch wieder geladen.</p><p class="muted">${state.rows.length} Dialoge. <a href="#review.pronunciation" data-help-open="review.pronunciation">Hilfe bei Aussprache und Stimmklang</a></p>${!state.render_enabled?`<p class="pr-experimental">Deutsches Neurendern ist in dieser Umgebung ausgeschaltet${state.render_disabled_reason?`: ${esc(state.render_disabled_reason)}`:'.'} Du kannst weiterhin Originaltöne und passende übernommene Vorschauen verarbeiten, Aufnahmen prüfen und eine Spielfassung erstellen. <a href="#review.audition" data-help-open="review.audition">Hilfe zum Start einer Vorschau</a></p>`:''}
      <nav class="pr-worklists" aria-label="Arbeitslisten">${[['todo','Offen'],['manual_confirmed','Manuell bestätigt'],['auto_clean','Automatisch unauffällig'],['auto_repaired','Automatisch repariert'],['all','Alle']].map(([key,label])=>`<button type="button" data-worklist="${key}" aria-pressed="${worklistFilter===key}" class="${worklistFilter===key?'active':''}">${label} · ${key==='all'?state.rows.length:worklistCounts[key]}</button>`).join('')}</nav>
      <section class="pr-toolbar" aria-label="Suche und Filter"><label>Dialog, Text oder Prüfergebnis suchen<input id="pr-query" type="search" placeholder="Dialognummer oder Text eingeben" value="${esc(query)}"></label><label>Status<select id="pr-status"><option value="all">Alle</option><option value="good" ${statusFilter==='good'?'selected':''}>Gut</option><option value="warnings" ${statusFilter==='warnings'?'selected':''}>Warnungen</option><option value="render" ${statusFilter==='render'?'selected':''}>Geplante Änderungen</option><option value="unaccepted" ${statusFilter==='unaccepted'?'selected':''}>Nicht übernommene Vorschauen</option></select></label><label>Fehler sortieren<select id="pr-sort"><option value="manifest" ${sortOrder==='manifest'?'selected':''}>Reihenfolge der Dialogvorgaben</option><option value="most" ${sortOrder==='most'?'selected':''}>Meiste Fehler zuerst</option><option value="least" ${sortOrder==='least'?'selected':''}>Wenigste Fehler zuerst</option></select></label><label>Pro Seite<select id="pr-pagesize">${[10,25,50,100].map(n=>`<option ${pageSize===n?'selected':''}>${n}</option>`).join('')}</select></label>${state.mode==='patch'?`<div class="pr-import-export"><button id="pr-export-recipe" ${exportBusy?'disabled':''}>Rezept speichern</button><label class="pr-check">Rezept laden<input id="pr-import" type="file" accept="application/json,.json"></label></div><p class="muted">Mit „Rezept speichern“ erhältst du eine Datei mit den Texten und Einstellungen deiner ausgewählten Aufnahmen. Du kannst sie später laden oder weitergeben; Aufnahmen enthält sie nicht. <a href="#review.recipe" data-help-open="review.recipe">Rezepte speichern und laden</a></p>`:''}<div class="pr-categories">${categories.map(c=>`<label><input type="checkbox" data-category="${esc(c.id)}" ${chosenCategories.has(c.id)?'checked':''}> ${esc(c.label)}</label>`).join('')}</div></section>
      ${state.recipe?`<p class="pr-recipe-hint">Rezept für <b>${esc(state.recipe.label)}</b> geladen. Verarbeite die gewünschten Dialoge und höre die Ergebnisse selbst an. Den Rendermodus kannst du pro Dialog ändern. <a href="#review.recipe" data-help-open="review.recipe">Mit einem Rezept weiterarbeiten</a></p>${(state.recipe.warnings||[]).map(w=>`<p class="pr-experimental">${esc(w)}</p>`).join('')}`:''}<section class="pr-batch" aria-label="Auswahl und nächster Lauf"><b>${selected.size} Dialoge ausgewählt</b><button id="pr-select-matches">Alle ${rows.length} Treffer auswählen</button><button id="pr-clear-selection" ${selected.size?'':'disabled'}>Auswahl löschen</button>${showSynthesisOptions?`<label>Rendermodus für die Auswahl<select id="pr-batch-backend" ${state.recipe?'disabled':''}>${(state.backends||[]).map(value=>`<option value="${esc(value)}" ${value===batchBackend?'selected':''}>${esc(value.toUpperCase())}${value==='cpu'?' · experimentell':''}</option>`).join('')}</select></label><label class="pr-check"><input id="pr-confirm-cpu" type="checkbox" ${cpuConfirmed?'checked':''}> CPU ausdrücklich experimentell wählen</label>`:'<span class="muted">Keine Spracherzeugung erforderlich</span>'}<span class="pr-batch-spacer"></span><button id="pr-start" class="primary" ${disabled?'disabled':''}>Ausgewählte Dialoge verarbeiten</button></section>${state.audition_queue_enabled===true?`<section class="pr-batch pr-audition-queue" aria-label="Vorschau-Warteliste"><div><b>Vorschau-Warteliste · ${auditionQueueIds.length}</b><p>Erzeuge mehrere Vorschauen nacheinander, um sie anschließend zu vergleichen.</p></div>${visibleAuditionQueueIds.map(id=>`<button data-queue-remove="${esc(id)}" ${jobRunning||busy?'disabled':''}>Dialog ${esc(id)} entfernen</button>`).join('')}${hiddenAuditionQueueCount?`<span class="muted">… und ${hiddenAuditionQueueCount} weitere</span>`:''}<button data-queue-clear ${!auditionQueueIds.length||jobRunning||busy?'disabled':''}>Leeren</button><button id="pr-audition-queue-start" class="primary" ${!auditionQueueIds.length||!state.render_enabled||jobRunning||busy?'disabled':''}>Vorschau-Warteliste rendern</button></section>`:''}<p class="pr-next-run-hint">Wähle die Dialoge im Dialogkopf aus und starte „Ausgewählte Dialoge verarbeiten“, um deine vorgemerkten Änderungen anzuwenden. Eine einzelne Vorschau startest du direkt am Dialog. <a href="#review.batch" data-help-open="review.batch">Dialoge auswählen und verarbeiten</a></p>${jobRunning?'<p class="pr-experimental">Ein Renderauftrag läuft bereits. Vorgemerkte Vorschauen warten auf den nächsten manuellen Lauf.</p>':''}${selected.size>1&&batchInfo.skipped?`<p class="pr-experimental">${eligibleActions.length} mit Auftrag oder passender Probe · ${batchInfo.skipped} ohne offenen Auftrag werden übersprungen.</p>`:''}${selected.size>1&&!eligibleActions.length?'<p class="pr-experimental">Kein ausgewählter Dialog hat einen offenen Auftrag oder eine passende angenommene Probe.</p>':''}${cpuSelected?'<p class="pr-experimental">CPU ungetestet: eine CPU-Bestätigung ist nur für ausgewählte Synthese-Dialoge erforderlich. Die CPU-Wahl am Dialog hat Vorrang vor dem Rendermodus der Auswahl.</p>':''}
      ${state.job?`<section class="pr-job" role="status">${esc(jobIntent)} · Auftrag: ${esc(state.job.status||'idle')}<span data-job-progress>${state.job.progress?.detail?` · ${esc(state.job.progress.detail)}`:''}</span><p data-memory-advisory ${state.job.memory_advisory?'':'hidden'}>${esc(memoryAdvisoryText())}</p>${state.job.id?` · ${esc(state.job.id)}`:''}${(state.job.dialog_ids||[]).length?` · ${(state.job.completed||[]).length}/${state.job.dialog_ids.length} Dialoge`:''}${reusedIds.length?` · Angenommene Probe wiederverwendet: ${esc(reusedIds.join(', '))}`:''}${state.job.errors&&Object.keys(state.job.errors).length?` · Fehler: ${esc(Object.entries(state.job.errors).map(([id,e])=>`${id}: ${e}`).join(' · '))}`:''}</section>`:''}<div class="pr-pager"><button id="pr-prev" ${page===0?'disabled':''}>Zurück</button><span>Seite ${page+1} von ${pages} · ${rows.length} Treffer</span><button id="pr-next" ${page+1>=pages?'disabled':''}>Weiter</button></div><section class="pr-list" aria-label="Dialogliste">${pinnedAuditionDetached?`<p class="pr-audition-pinned" role="status">Damit du den laufenden Versuch verfolgen kannst, bleibt Dialog ${esc(pinnedAuditionId)} hier sichtbar. Er gehört möglicherweise zu einer anderen Liste oder Seite; die Trefferzahlen bleiben unverändert.</p>`:''}${visibleRows.map(row=>rowMarkup(row)).join('')||'<p class="pr-empty">Keine passenden Dialoge.</p>'}</section><div class="pr-pager"><button id="pr-prev-bottom" ${page===0?'disabled':''}>Zurück</button><span>Seite ${page+1} von ${pages} · ${rows.length} Treffer</span><button id="pr-next-bottom" ${page+1>=pages?'disabled':''}>Weiter</button></div>`;
    let focused=focusId?root.querySelector(`#${CSS.escape(focusId)}`):null;
    if(!focused&&focusData){const attr=focusData[0].replace(/[A-Z]/g,c=>`-${c.toLowerCase()}`);focused=root.querySelector(`[data-${attr}="${CSS.escape(focusData[1])}"]`);}
    if(focused){focused.focus({preventScroll:true});if(selection)try{focused.setSelectionRange(...selection);}catch{}}
    if(pinnedViewport){const nextPinnedNode=root.querySelector(`[data-row="${CSS.escape(String(pinnedAuditionId))}"]`);if(nextPinnedNode)pinnedViewport.scrollTarget.scrollTop+=nextPinnedNode.getBoundingClientRect().top-pinnedViewport.top;}
    const header = document.querySelector('#pr-summary'); if(header) header.textContent=`${rows.length} Treffer · ${state.rows.length} Dialoge`;
    bindBuildControls();
    reportPreviewWarnings();
    const q=root.querySelector('#pr-query'); q.oninput=()=>{discardAuditionPinForNavigation();const value=q.value;query=value;clearTimeout(searchTimer);searchTimer=setTimeout(()=>{page=0;const pos=q.selectionStart;render();const next=root.querySelector('#pr-query');next?.focus();next?.setSelectionRange(pos,pos);},180);};
    const showConfirmed=root.querySelector('#pr-show-confirmed');if(showConfirmed)showConfirmed.onclick=()=>showConfirmedRow(showConfirmed.dataset.confirmedRow);
    root.querySelectorAll('[data-worklist]').forEach(button=>button.onclick=()=>{discardAuditionPinForNavigation();worklistFilter=button.dataset.worklist;page=0;render();});
    root.querySelector('#pr-status').onchange=e=>{discardAuditionPinForNavigation();statusFilter=e.target.value;page=0;render();};
    root.querySelector('#pr-sort').onchange=e=>{discardAuditionPinForNavigation();sortOrder=e.target.value;page=0;render();};
    const batchBackendNode=root.querySelector('#pr-batch-backend');if(batchBackendNode)batchBackendNode.onchange=e=>{batchBackend=e.target.value;cpuConfirmed=false;auditionCpuConfirmed.clear();render();};
    root.querySelector('#pr-pagesize').onchange=e=>{discardAuditionPinForNavigation();pageSize=Number(e.target.value);page=0;render();};
    root.querySelectorAll('[data-category]').forEach(node=>node.onchange=()=>{discardAuditionPinForNavigation();node.checked?chosenCategories.add(node.dataset.category):chosenCategories.delete(node.dataset.category);page=0;render();});
    ['#pr-prev','#pr-prev-bottom'].forEach(sel=>root.querySelector(sel).onclick=()=>{discardAuditionPinForNavigation();page--;render();});
    ['#pr-next','#pr-next-bottom'].forEach(sel=>root.querySelector(sel).onclick=()=>{discardAuditionPinForNavigation();page++;render();});
    root.querySelectorAll('[data-select]').forEach(node=>node.onchange=()=>{node.checked?selected.add(node.dataset.select):selected.delete(node.dataset.select);cpuConfirmed=false;render();});
    root.querySelector('#pr-select-matches').onclick=()=>{rows.forEach(row=>selected.add(String(row.dialog_id)));cpuConfirmed=false;render();};
    root.querySelector('#pr-clear-selection').onclick=()=>{selected.clear();cpuConfirmed=false;render();};
    for(const [attr,key] of [['tts','tts_text'],['note','note']])root.querySelectorAll(`[data-${attr}]`).forEach(node=>{
      node.oninput=()=>{
        const id=String(node.dataset[attr]),row=rowById(id);
        drafts.set(id,{...(drafts.get(id)||{}),[key]:node.value});
        // Keep the raw editor value while the server prepares the canonical TTS text.
        if(row&&(pendingUpdates.has(id)||rowSaveCounts.get(id)||!editorValueSaved(id,key,node.value,row[key])))stageRowUpdate(id,{[key]:node.value});
      };
      node.onblur=()=>flushRowUpdate(String(node.dataset[attr])).catch(()=>undefined);
    });
    root.querySelectorAll('[data-backend]').forEach(node=>node.onchange=()=>{const id=String(node.dataset.backend);auditionCpuConfirmed.delete(id);stageRowUpdate(id,{backend:node.value||null});});
    for(const [key,field,dataKey] of [['anchor','anchor','anchor'],['prewarm','prewarm','prewarm'],['generation-mode','generation_mode','generationMode']]) root.querySelectorAll(`[data-${key}]`).forEach(node=>node.onchange=()=>{const id=String(node.dataset[dataKey]);captureDrafts();const view=drafts.get(id);stageRowUpdate(id,field==='generation_mode'?{generation_mode:view.generation_mode}:{next_preset:view.next_preset});});
    const cpuConfirmation=root.querySelector('#pr-confirm-cpu');if(cpuConfirmation)cpuConfirmation.onchange=e=>{cpuConfirmed=e.target.checked;const start=root.querySelector('#pr-start');start.disabled=batchStartDisabled(eligibleActions.length,hasSynthesis,cpuSelected,e.target.checked);};
    root.querySelectorAll('[data-audition-cpu]').forEach(node=>node.onchange=e=>{const id=String(node.dataset.auditionCpu);if(e.target.checked)auditionCpuConfirmed.add(id);else auditionCpuConfirmed.delete(id);render();});
    root.querySelectorAll('[data-audition-queue]').forEach(button=>button.onclick=async()=>{const id=String(button.dataset.auditionQueue),operation=(state.audition_queue||[]).map(String).includes(id)?'remove':'add';try{await mutate('/audition-queue',{operation,dialog_id:id});schedulePoll();}catch{}});
    root.querySelectorAll('[data-queue-remove]').forEach(button=>button.onclick=async()=>{try{await mutate('/audition-queue',{operation:'remove',dialog_id:String(button.dataset.queueRemove)});schedulePoll();}catch{}});
    const queueClear=root.querySelector('[data-queue-clear]');if(queueClear)queueClear.onclick=async()=>{try{await mutate('/audition-queue',{operation:'clear'});schedulePoll();}catch{}};
    const queueStart=root.querySelector('#pr-audition-queue-start');if(queueStart)queueStart.onclick=async()=>{const ids=(state.audition_queue||[]).map(String);if(!ids.length)return;const availability=state.render_availability||{};if(!(availability.available??state.render_enabled)||availability.busy){setMessage(availability.reason||state.render_disabled_reason||'Renderer ist nicht verfügbar.',true);render();return;}try{busy=true;render();await flushAllRowUpdates();const queueRows=ids.map(rowById).filter(Boolean);if(queueRows.length!==ids.length)throw new Error('Die Warteliste enthält nicht verfügbare Dialoge.');const usesCpu=queueRows.some(row=>(draftFor(row).backend||batchBackend)==='cpu');if(usesCpu&&queueRows.some(row=>(draftFor(row).backend||batchBackend)==='cpu'&&!auditionCpuConfirmed.has(String(row.dialog_id))))throw new Error('CPU ist ungetestet. Bestätige sie je Dialog ausdrücklich, bevor die Warteliste startet.');auditionPinnedId=null;const payload={revision:state.revision,intent:'audition',dialog_ids:ids,audition_queue:true,backend:root.querySelector('#pr-batch-backend')?.value||batchBackend,...(usesCpu?{experimental_cpu:true}:{})};acceptMutation(await post('/start',payload));setMessage('Die Vorschau-Warteliste wurde gestartet. Ergebnisse bleiben Vorschauen.');render();schedulePoll();}catch(error){if(error.status===409)await refreshRows().catch(()=>undefined);setMessage(error.message,true);render();schedulePoll();}finally{busy=false;render();}};
    root.querySelectorAll('[data-audition]').forEach(button=>{const pin=event=>{auditionPinSuppressed=false;auditionPinnedId=String(button.dataset.audition);if(event.type==='pointerdown')event.preventDefault();};button.onpointerdown=pin;button.onfocus=pin;});
    root.querySelectorAll('[data-audition]').forEach(button=>button.onclick=async()=>{
      const id=String(button.dataset.audition),row=rowById(id),newSeed=button.dataset.newSeed==='true';
      auditionPinSuppressed=false;auditionPinnedId=id;
      if(!row||row.generation_mode==='copy_original'){setMessage('Originalton wird über den Originalplayer geprüft und benötigt keine Syntheseprobe.',true);render();return;}
      captureDrafts();
      const view=drafts.get(id)||draftFor(row),backend=root.querySelector('#pr-batch-backend')?.value||batchBackend||state.backends?.[0]||'gpu',usesCpu=(view.backend||backend)==='cpu';
      const available=state.render_availability?.available??(state.render_enabled===true),busyRenderer=Boolean(state.render_availability?.busy||state.job?.status==='running');
      if(!available){setMessage(state.render_availability?.reason||state.render_disabled_reason||'Für diesen Einstieg ist kein produktiver Renderexecutor konfiguriert.',true);render();return;}
      if(busyRenderer){setMessage('Ein Renderauftrag läuft bereits. Diese Vorschau wurde weder gestartet noch vorgemerkt.',true);render();return;}
      if(usesCpu&&!auditionCpuConfirmed.has(id)){setMessage('CPU ist ungetestet. Bestätige sie ausdrücklich, bevor ein neuer CPU-Vorhörversuch startet.',true);render();return;}
      uiDiagBegin();busy=true;auditionStartingId=id;render();
      try {
        await flushRowUpdate(id);
        await saveRowForAudition(id);
        const latestAvailability=state.render_availability||{};
        if(latestAvailability.busy||state.job?.status==='running')throw new Error('Ein Renderauftrag wurde inzwischen gestartet. Die Vorschau wurde nicht vorgemerkt.');
        if(!(latestAvailability.available??state.render_enabled))throw new Error(latestAvailability.reason||state.render_disabled_reason||'Renderer nicht verfügbar. Der gespeicherte Entwurf bleibt erhalten.');
        const currentRow=rowById(id),latestView=draftFor(currentRow),effectiveBackend=(latestView.backend||backend)==='cpu';
        if(effectiveBackend&&!auditionCpuConfirmed.has(id))throw new Error('CPU ist ungetestet. Bestätige sie ausdrücklich; es wurde kein Renderauftrag gestartet.');
      const payload={revision:state.revision,dialog_ids:[id],backend,intent:'audition',...(newSeed?{fresh_seed:true}:{}),...(effectiveBackend?{experimental_cpu:true}:{})};
        auditionContext={dialog_id:id,backend,preset:{...(latestView.next_preset||{}),seed:newSeed?'fresh':'keep'}};
        updateAuditionTerminal({intent:'audition',status:'running',active_dialog_id:id,dialog_ids:[id],progress:{phase:'preparing',detail:'Vorschauauftrag wird an den Renderer übergeben.'}});
        const body=await post('/start',payload);acceptMutation(body);await refreshScopedRows([id]);
        const reused=state.job?.reused?.map(String).includes(id);
        setMessage(reused?`Die passende angenommene Probe für Dialog ${id} kann direkt mit „Abspielen“ bei der ausgewählten Ausgabe angehört werden.`:`Einzelvorhören für Dialog ${id} wurde gestartet. Höre die neue Vorschau an und bestätige sie bei Gefallen als gut, um sie für das Spiel auszuwählen.`);render();schedulePoll();
      } catch(error) { uiDiagFinish('failed');if(error.status===409)await refreshRows().catch(()=>undefined);if(state.job?.intent!=='audition'||!(state.job.dialog_ids||[]).map(String).includes(id)){auditionPinnedId=null;window.LauncherConsole?.update('render',`Vorschau für Dialog ${id} nicht gestartet: ${error.message}`,[],{status:'failed'});}setMessage(error.message,true);render();schedulePoll(); }
      finally { busy=false;auditionStartingId=null;render(); }
    });
    root.querySelectorAll('[data-hear]').forEach(button=>button.onclick=()=>hearAudio({role:button.dataset.playerRole,src:button.dataset.playerSrc,label:button.dataset.playerLabel,key:button.dataset.playerKey,dialogId:button.dataset.playerDialog,outputId:button.dataset.playerOutput}));
    root.querySelectorAll('[data-restore-previous]').forEach(node=>node.onclick=async()=>{
      const id=String(node.dataset.restorePrevious),current=rowById(id)?.current;
      if(!rowById(id)?.previous_selection||busy)return;
      const expectedBinding=current?.binding;
      captureDrafts();busy=true;render();
      window.LauncherConsole?.update('review',`Dialog ${id}: Vorherige ausgewählte Fassung wird wiederhergestellt.`,[],{status:'running'});
      try {
        await flushAllRowUpdates();await saveCurrentDraft(id);
        const body=await post('/restore-previous',{revision:state.revision,dialog_id:id,expected_current_binding:expectedBinding});
        if(!acceptMutation(body,id))await refreshScopedRows([id],{skipStatus:true});
        drafts.delete(id);auditionPinnedId=null;
        const restored=rowById(id)?.current,seed=restored?.preset?.seed;
        const result=`Dialog ${id}: Vorherige Version wiederhergestellt${Number.isInteger(seed)?` · fester Seed ${seed}`:''}.`;
        setMessage(result);window.LauncherConsole?.update('review',result,[],{status:'completed'});schedulePoll();
      } catch(error) {
        const result=`Dialog ${id}: Vorherige Version nicht wiederhergestellt – ${error.message||String(error)}`;
        setMessage(result,true);window.LauncherConsole?.update('review',result,[],{status:'failed',error:true});
      } finally {busy=false;render({captureDrafts:false});}
    });
    root.querySelectorAll('[data-good]').forEach(node=>node.onclick=async()=>{const row=rowById(node.dataset.good);node.disabled=true;node.textContent='Wird bestätigt …';try{await flushRowUpdate(node.dataset.good);await mutate('/good',{dialog_id:node.dataset.good,good:node.dataset.goodValue==='true',binding:row?.current?.binding});if(node.dataset.goodValue==='true'){const current=rowById(node.dataset.good);confirmSuccess(node.dataset.good,reviewCandidate(current)?'Aktuelle Ausgabe als gut bestätigt. Eine andere Vorschau ist noch nicht übernommen.':'Aktuelle Ausgabe als gut bestätigt.');render();}}catch{}});
    root.querySelector('#pr-start').onclick=async()=>{const originalIds=[...selected],originalCount=originalIds.length;try{await Promise.all(originalIds.map(id=>flushRowUpdate(id)));}catch{return;}const selectedRows=originalIds.map(rowById).filter(Boolean),single=originalCount===1,entries=selectedRows.map(row=>({row,action:single?processAction(row):pendingAction(row)})),eligible=entries.filter(({row,action})=>single||action!=='none'||canReuseAccepted(row));if(!originalCount||!eligible.length){setMessage(single?'Wähle einen Dialog für die Verarbeitung.':'Kein ausgewählter Dialog hat einen offenen Auftrag oder eine passende angenommene Probe.',true);render();return;}const eligibleRows=eligible.map(({row})=>row),synthesisRows=eligible.filter(({row,action})=>action==='render'&&!canReuseAccepted(row)).map(({row})=>row),hasSynthesisRequest=eligible.some(({row,action})=>action==='render'||canReuseAccepted(row)),ids=eligibleRows.map(row=>String(row.dialog_id)),backend=root.querySelector('#pr-batch-backend')?.value||batchBackend,usesCpu=synthesisRows.some(row=>(draftFor(row).backend||backend)==='cpu');if(synthesisRows.length&&!state.render_enabled){setMessage('Deutsch neurendern ist in dieser Umgebung ausgeschaltet. Originalton und passende angenommene Proben bleiben verarbeitbar.',true);render();return;}if(usesCpu&&!root.querySelector('#pr-confirm-cpu')?.checked){setMessage('CPU ist experimentell und ungetestet. Bestätige die CPU-Wahl ausdrücklich, bevor du startest.',true);render();return;}auditionPinnedId=null;busy=true;render();try{const payload={revision:state.revision,dialog_ids:ids,intent:'process',...(hasSynthesisRequest?{backend,...(usesCpu?{experimental_cpu:true}:{})}:{})};const body=await post('/start',payload);acceptMutation(body);ids.forEach(id=>selected.delete(id));await refreshRows();const reused=state.job?.reused||[],completed=state.job?.status==='completed';setMessage(completed&&reused.length?`Der Gesamtlauf ist abgeschlossen; passende angenommene Probe${reused.length===1?'':'n'} wurde${reused.length===1?'':'n'} wiederverwendet.`:'Die ausgewählten Dialoge werden jetzt verarbeitet. Gewählter Originalton wird dabei direkt übernommen.');render();schedulePoll();}catch(error){if(error.status===409)await refreshRows().catch(()=>undefined);setMessage(error.message,true);render();schedulePoll();}finally{busy=false;render();}};
    const adoptOutput=async(node,ignoreWarnings=false)=>{
      const id=String(node.dataset.ignore||node.dataset.release),candidateId=node.dataset.candidate;
      const output=[...(rowById(id)?.candidates||[]),...(rowById(id)?.history||[])].find(value=>String(value.id)===String(candidateId));
      node.disabled=true;node.textContent='Wird übernommen …';
      const warningDetail=ignoreWarnings?` Warnungsgründe: ${previewWarningReasons(output).join(' · ')}`:'';
      window.LauncherConsole?.update('review',`Dialog ${id}: Fassung wird als gut bestätigt und ausgewählt.${warningDetail}`,[],{status:'running',warning:ignoreWarnings});
      try {
        await flushRowUpdate(id);
        await mutate('/release',{dialog_id:id,candidate_id:candidateId,...(ignoreWarnings?{ignore_warnings:true}:{})});
        const seed=rowById(id)?.current?.preset?.seed;
        const message=`Dialog ${id}: Fassung als gut bestätigt und ausgewählt${Number.isInteger(seed)?` · fester Seed ${seed}`:''}.`;
        confirmSuccess(id,message);render();
        window.LauncherConsole?.update('review',message,[],{status:'completed'});schedulePoll();
      } catch(error) {
        const message=`Dialog ${id}: Fassung nicht bestätigt – ${error.message||String(error)}`;
        setMessage(message,true);render();
        window.LauncherConsole?.update('review',message,[],{status:'failed',error:true});
      }
    };
    root.querySelectorAll('[data-release]').forEach(node=>node.onclick=()=>adoptOutput(node,true));
    root.querySelectorAll('[data-ignore]').forEach(node=>node.onclick=()=>adoptOutput(node,true));
    const recipeExport=root.querySelector('#pr-export-recipe');if(recipeExport)recipeExport.onclick=()=>saveRecipe();
    const imp=root.querySelector('#pr-import');if(imp)imp.onchange=async()=>{const file=imp.files?.[0];if(!file)return;try{await flushAllRowUpdates();const extension=JSON.parse(await file.text());const result=await mutate('/import',{extension});drafts.clear();if(!Array.isArray(result.rows))await refreshRows();setMessage(result.recipe?`Rezept für ${result.recipe.label} geladen. ${result.changed_dialogues} Dialoge zum Rendern vorgemerkt.`:`Bearbeitungsstand geladen. Revision ${result.revision}; passende Dialoge sind jetzt bearbeitbar.`);render({captureDrafts:false});}catch(error){setMessage(error.message,true);render();}finally{imp.value='';}};
  }

  async function refreshRows() {
    const status=await request('/status');if(!applyStatus(status))return false;
    const rows=await request('/rows');
    let applied=false;
    if(rows.revision>=state.revision){state.revision=rows.revision;setRows(rows.rows);applied=true;}
    else {const latest=await request('/rows');if(latest.revision>=state.revision){state.revision=latest.revision;setRows(latest.rows);applied=true;}}
    if(applied)hydratedResultSignature=jobResultSignature(state.job);
    render();
    return applied;
  }
  function applyStatus(status) {
    if(Number.isInteger(status.revision)&&status.revision<(state.revision||0))return false;
    if(Number.isInteger(status.revision))state.revision=status.revision;
    if(typeof status.read_only==='boolean'){state.read_only=status.read_only;state.read_only_reason=status.read_only_reason;}
    if(typeof status.audition_queue_enabled==='boolean')state.audition_queue_enabled=status.audition_queue_enabled;
    if(Array.isArray(status.audition_queue))state.audition_queue=status.audition_queue.map(String);
    state.job=status.job;updateAuditionTerminal(state.job);if(status.patch_build)state.patch_build=status.patch_build;if(state.patch_build?.job?.status==='completed'&&message==='Deine Spielfassung wird jetzt erstellt.')setMessage('');if(status.render_availability)state.render_availability=status.render_availability;return true;
  }
  function jobResultSignature(job) {
    const terminal=['completed','failed','error','cancelled'].includes(job?.status)?job.status:null;
    return JSON.stringify([job?.id,job?.dialog_ids,job?.completed,job?.errors,job?.reused,job?.active_dialog_id,terminal]);
  }
  function jobStateSignature(job) { return JSON.stringify([job?.id,job?.status,job?.intent,job?.dialog_ids,job?.completed,job?.errors,job?.reused,job?.active_dialog_id]); }
  function mergeScopedRows(rows) {
    if(rows.revision<(state.revision||0))return false;
    const updates=new Map(rows.rows.map(row=>[String(row.dialog_id),row]));
    state.rows=state.rows.map(row=>updates.has(String(row.dialog_id))?{...row,...updates.get(String(row.dialog_id))}:row);
    rowsById=new Map(state.rows.map(row=>[String(row.dialog_id),row]));state.revision=rows.revision;
    return true;
  }
  async function hydrateJobResults(job) {
    const signature=jobResultSignature(job);
    if(signature===hydratedResultSignature)return false;
    const ids=[...(job?.dialog_ids||[])];
    if(!ids.length){const refreshed=await refreshRows();if(!refreshed)return false;hydratedResultSignature=signature;return true;}
    const params=new URLSearchParams();ids.forEach(id=>params.append('dialog_id',String(id)));
    const rows=await request(`/rows?${params}`);
    if(!mergeScopedRows(rows))return false;
    hydratedResultSignature=signature;
    return true;
  }
  async function refreshScopedRows(ids,{skipStatus=false}={}) {
    if(!ids?.length)return;
    if(!skipStatus){const status=await request('/status');applyStatus(status);}
    const params=new URLSearchParams();ids.forEach(id=>params.append('dialog_id',String(id)));
    const rows=await request(`/rows?${params}`);
    if(!mergeScopedRows(rows)){const latest=await request('/rows');if(latest.revision>=state.revision){state.revision=latest.revision;setRows(latest.rows);}}
    render();
  }
  async function pollStatus() {
    const run=uiDiagCollecting()?uiDiagRun:null;
    if(run){run.polls++;run.pollInFlight++;run.pollMaxInFlight=Math.max(run.pollMaxInFlight,run.pollInFlight);}
    try { return await pollStatusImpl(); }
    finally { if(run)run.pollInFlight=Math.max(0,run.pollInFlight-1); }
  }
  async function pollStatusImpl() {
    const status=await request('/status');
    if(Number.isInteger(status.revision)&&status.revision<(state.revision||0)){
      const hydrated=await hydrateJobResults(state.job);if(hydrated)render();return;
    }
    const previous=JSON.stringify([jobStateSignature(state.job),state.render_availability,state.patch_build,state.read_only]),previousProgress=JSON.stringify([state.job?.status,state.job?.progress,state.job?.active_dialog_id,state.job?.memory_advisory,state.render_availability]);
    const revisionBefore=state.revision;if(!applyStatus(status))return;
    const changed=previous!==JSON.stringify([jobStateSignature(state.job),state.render_availability,state.patch_build,state.read_only]);
    const resultsChanged=jobResultSignature(state.job)!==hydratedResultSignature;
    let hydrated=false;
    if(resultsChanged)hydrated=await hydrateJobResults(state.job);
    else if(status.revision>revisionBefore&&previousProgress===JSON.stringify([state.job?.status,state.job?.progress,state.job?.active_dialog_id,state.job?.memory_advisory,state.render_availability])){await refreshRows();return;}
    if(changed||hydrated)render();
    else if(previousProgress!==JSON.stringify([state.job?.status,state.job?.progress,state.job?.active_dialog_id,state.job?.memory_advisory,state.render_availability]))refreshProgress();
  }
  function schedulePoll() { clearTimeout(timer); const hydrationPending=state?.job&&jobResultSignature(state.job)!==hydratedResultSignature; if(state?.job?.status==='running'||state?.patch_build?.job?.status==='running'||state?.patch_build?.busy||state?.render_availability?.busy||hydrationPending) timer=setTimeout(async()=>{try{await pollStatus();schedulePoll();}catch(error){setMessage(error.message,true);schedulePoll();}},1500); }
  async function mount(options={}) {
    root=typeof options.root==='string'?document.querySelector(options.root):options.root||document.querySelector('#patch-review');
    if(!root) throw new Error('Patchreview-Zielbereich fehlt.');
    window.LauncherReviewLoading=true;
    window.LauncherConsole?.update('review','Lade Dialoge …',[],{status:'running'});
    try {
    apiBase=options.apiBase||'/api/patch-review';
    const [bootstrap]=await Promise.all([request('/bootstrap')]);
    mountReviewAudio();
    state=bootstrap;setRows(bootstrap.rows);hydratedResultSignature=jobResultSignature(state.job);auditionPinnedId=restorableAuditionPin(state.job);updateAuditionTerminal(state.job);
    if(!batchBackend) batchBackend=state.backends?.[0]||'gpu';
    if(options.mode) state.mode=options.mode;
    const head=document.querySelector('#pr-mode');if(head)head.textContent=state.mode==='wizard'?'Wizardreview':'Patchreview';
    const finish=document.querySelector('#pr-finish-global');if(finish){finish.hidden=state.mode!=='patch';finish.onclick=()=>openCompletion();}
    const back=document.querySelector('#pr-back-render');
    if(back){
      back.hidden=!root.hasAttribute?.('data-launcher-review');
      back.onclick=async()=>{
        back.disabled=true;
        try{
          captureDrafts();if(!readOnly())await flushAllRowUpdates();
          for(const id of readOnly()?[]:[...drafts.keys()]){
            const row=rowById(id);
            if(row&&(hasDraftChanges(row)||draftFor(row).note!==(row.note||'')))await saveCurrentDraft(id);
          }
          if(!readOnly())await flushAllRowUpdates();
          const params=new URLSearchParams({page:'render'}),session=new URLSearchParams(location.search).get('session');
          if(session)params.set('session',session);
          clearReviewAudio();clearTimeout(timer);
          location.href='/?'+params.toString();
        }catch(error){setMessage(`Rückkehr nicht möglich: ${error.message||String(error)}`,true);render();}
        finally{back.disabled=false;}
      };
    }
    if(!started){started=true;window.addEventListener('pagehide',clearReviewAudio);}
    render();
    window.LauncherReviewLoading=false;
    window.LauncherConsole?.update('review','Dialoge geladen · Review bereit.',[],{status:'completed'});
    if(state.job?.status==='running'||state.patch_build?.job?.status==='running'||state.patch_build?.busy||state.render_availability?.busy)schedulePoll();
    return {refresh:refreshRows};
    } catch(error) {
      clearReviewAudio();
      window.LauncherReviewLoading=false;
      window.LauncherConsole?.update('review',`Dialogladen fehlgeschlagen: ${error.message}`,[],{status:'failed'});
      throw error;
    }
  }
  window.PatchReview={mount};
  if(document.querySelector('#patch-review') && !document.querySelector('#patch-review').hasAttribute('data-wizard-review')) mount().catch(error=>{const target=document.querySelector('#patch-review');if(target)target.innerHTML=`<p class="pr-error" role="alert">${esc(error.message)}</p>`;});
})();
