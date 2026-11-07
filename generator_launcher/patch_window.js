/* Live German patch guidance. Native activation uses the exact hosted process. */
(() => {
  const $ = id => document.getElementById(id);
  let current = null, sentKey = '', shownStage = '';
  const bridge = () => window.webkit?.messageHandlers?.launcher;
  const status = (text,failed=false) => window.LauncherConsole?.update('patch-window',text,[],{status:failed?'failed':'ready'});
  function resizeTarget(){
    const field=$('patch-target-path');if(!field?.style||!field.clientWidth)return;
    field.style.height='auto';field.style.height=Math.max(90,field.scrollHeight)+'px';
  }
  window.LauncherPatchTargetResize=resizeTarget;
  window.addEventListener('resize',resizeTarget);
  if(typeof ResizeObserver!=='undefined'){let width=-1;new ResizeObserver(entries=>{const next=entries[0].contentRect.width;if(next!==width){width=next;resizeTarget();}}).observe($('patch-target-path'));}
  function target(text) {
    const field = $('patch-target-path');
    if (field.value !== text) field.value = text;
    resizeTarget();
  }
  function send(extra = {}) {
    if (!current) return;
    bridge()?.postMessage({action:'patchWindow', active:true, ...current, ...extra});
  }
  async function copy() {
    if (!current?.patch_target_wine) return;
    if (bridge()) { send({copy_only:true}); return; }
    try {
      const field = $('patch-target-path');
      field.focus(); field.select();
      if (!document.execCommand('copy')) throw Error('copy unavailable');
      status('Windows-Zielordner in die Zwischenablage kopiert.');
    } catch { status('Automatisches Kopieren nicht verfügbar. Markiere den angezeigten Windows-Zielordner und kopiere ihn.',true); }
  }
  window.LauncherPatchWindowStatus = value => {
    if (!current || value.stage_id !== current.stage_id) return;
    const message=value.message || 'Patchfenster im separaten Wine-Programm bedienen.';
    const copyHint=value.clipboard_copied?'':current.patch_target_wine?' Den angezeigten Windows-Zielordner mit dem Kopierknopf kopieren.':' Wine-Zielpfad nicht verfügbar. Prüfe die Laufwerkszuordnung im Wine-Profil.';
    status(message+copyHint,value.state==='unavailable');
  };
  window.LauncherPatchWindow = {
    sync(value) {
      const active = value.phase === 'german_patch' && value.status === 'running';
      $('patch-assistant').hidden = !active;
      if (!active) {
        if (sentKey) bridge()?.postMessage({action:'patchWindow', active:false});
        if(current)window.LauncherConsole?.update('patch-window',null,[],{status:'idle'});
        current = null; sentKey = ''; return;
      }
      const descriptor = value.patch_interaction;
      if (!descriptor) {
        if (sentKey) bridge()?.postMessage({action:'patchWindow', active:false});
        current = null; sentKey = '';
        target('Der konkrete Zielordner wird beim Patchstart angezeigt …');
        $('patch-focus').disabled = true; $('patch-copy').disabled = true;
        status('Deutscher Patch wird gestartet …'); return;
      }
      current = descriptor;
      target(descriptor.patch_target_wine || 'Windows-Zielordner nicht verfügbar. Prüfe die Wine-Laufwerkszuordnung.');
      $('patch-copy').disabled = !descriptor.patch_target_wine;
      $('patch-focus').disabled = !bridge();
      const key = JSON.stringify(descriptor);
      if (key !== sentKey) {
        sentKey = key;
        if (!descriptor.patch_target_wine)status('Wine-Zielpfad nicht verfügbar. Prüfe die Laufwerkszuordnung im Wine-Profil.',true);
        else if(bridge())status('Windows-Zielordner wird in die Zwischenablage kopiert …');
        else status('Deutschen Patch im separaten Wine-Fenster öffnen. Den Zielordner mit dem Kopierknopf kopieren.');
        if (bridge()) send();
      }
      if (shownStage !== descriptor.stage_id) {
        shownStage = descriptor.stage_id;
        $('patch-target-path').scrollIntoView({block:'center'});
      }
    }
  };
  $('patch-focus').onclick = () => send({retry:true});
  $('patch-copy').onclick = copy;
  $('patch-target-path').addEventListener('keydown', event => {
    if (event.metaKey && !event.ctrlKey && !event.altKey && event.key.toLowerCase() === 'c'
        && current?.patch_target_wine) {
      event.preventDefault();
      copy();
    }
  });
  window.addEventListener('pagehide', () => {
    if (sentKey) bridge()?.postMessage({action:'patchWindow', active:false});
  });
})();
