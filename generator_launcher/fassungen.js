/* Fassung selection is independent of the running worker and never starts work. */
(() => {
  const $ = id => document.getElementById(id);
  const labels = {idle:'Noch nicht begonnen',running:'Wird gerendert',pausing:'Wird angehalten',paused:'Pausiert',failed:'Unterbrochen',interrupted:'Unterbrochen',completed_with_errors:'Nacharbeit erforderlich',completed:'Gerendert'};
  let snapshot = {}, repair = {}, page = new URLSearchParams(location.search).get('page')==='render'?'render':'downloads', pending = false, dirty = false, draft = false, lastSelected = null;
  let constraints = {canStart:false,blocked:false}, loaded = false;
  const reviewLocked = () => window.LauncherReviewActivating === true || window.LauncherNavigating === true;
  const date = value => {
    if (!value) return 'Zeit nicht überliefert';
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? 'Zeit nicht überliefert' : new Intl.DateTimeFormat('de-DE',{dateStyle:'medium',timeStyle:'short'}).format(d);
  };
  const category = item => item?.category || ({slow:'GPU',medium:'ANE',fast:'ANE+GPU',cpu:'CPU'}[item?.mode] || 'GPU');
  const selected = () => draft ? null : snapshot.selected_fassung || (snapshot.render_lists || []).find(item => item.id === snapshot.selected_list_id) || null;
  function itemLabel(item) {
    const complete = item.status === 'completed';
    return category(item) + ' · ' + (labels[item.status] || 'Unterbrochen') + (complete ? '' : ` · ${item.completed || 0}/${item.total || 0}`) + ' · ' + date(complete ? item.completed_at : item.started_at);
  }
  function message(value, failed = false) {
    $('fassung-message').textContent = value || '';
    $('fassung-message').hidden = !value;
    window.LauncherConsole?.update('render',value,[],{status:failed?'failed':'ready'});
  }
  function paint() {
    const item = selected(), complete = item?.status === 'completed';
    const running = snapshot.running_list_id;
    const live = ['running','pausing','paused'].includes(snapshot.status);
    const ownPaused = live && snapshot.status === 'paused' && item?.id === running;
    const active = live && snapshot.status !== 'paused';
    const start = $('render-start'), pause = $('render-pause'), review = $('review-open');
    const onRender = page === 'render', onRepair = page === 'repair';
    $('render-list').disabled = !loaded || pending || constraints.blocked || reviewLocked();
    if (!loaded) {
      for (const button of [start,pause,review]) { button.hidden = true; button.disabled = true; }
      start.classList.toggle('primary',false);
      $('fassung-details').textContent = 'Fassungen werden geladen …';
      $('repair-options').hidden = true;
      return;
    }
    start.hidden = !onRender || active;
    start.textContent = 'Rendering starten';
    start.classList.toggle('primary',!complete);
    start.classList.toggle('secondary-action',complete);
    start.disabled = pending || reviewLocked() || constraints.blocked || !constraints.canStart || (live && !ownPaused);
    pause.hidden = !onRender || !active;
    pause.disabled = pending || reviewLocked() || snapshot.status !== 'running';
    pause.textContent = snapshot.status === 'pausing' ? 'Wird angehalten …' : 'Anhalten';
    review.hidden = !(onRender && complete) && !onRepair;
    review.textContent = 'Zur Review';
    review.disabled = pending || constraints.blocked || !complete || snapshot.selected_review_ready === false || window.LauncherReviewActivating === true;
    const detail = $('fassung-details');
    detail.replaceChildren();
    const title = document.createElement('strong');
    title.textContent = item ? category(item) + ' · ' + (labels[item.status] || item.status) : 'Neue Fassung';
    detail.append(title);
    if (item) {
      const timing = document.createElement('span');
      timing.textContent = 'Begonnen: ' + date(item.started_at) + (complete ? ' · Abgeschlossen: ' + date(item.completed_at) : ` · Stand: ${item.completed || 0}/${item.total || 0} Dialoge`);
      detail.append(timing);
    }
    $('fassung-progress').textContent = live ? (labels[snapshot.status] || snapshot.status) + ` · ${snapshot.completed || 0}/${snapshot.total || 0} Dialoge` + (running && item?.id !== running ? ' · Eine andere Fassung wird verarbeitet.' : '') : '';
    const changed = item && ($('render-mode').value !== item.mode || $('render-ultra-fast').checked !== (item.ultra_fast === true));
    $('fassung-mode-note').hidden = !changed;
    $('fassung-mode-note').textContent = complete ? 'Die geänderten Einstellungen gelten erst für eine weitere oder ausdrücklich ersetzte Fassung.' : 'Die geänderten Einstellungen gelten beim nächsten Start für die noch fehlenden Aufnahmen. Fertige passende Aufnahmen bleiben erhalten.';
    $('repair-options').hidden = !onRender || !complete || live;
    $('repair-options').disabled = reviewLocked();
  }
  function update(value, repairStatus = repair) {
    loaded = true;
    snapshot = value; repair = repairStatus;
    const current = value.selected_list_id || value.list_id || 'legacy';
    const picker = $('render-list'), lists = value.render_lists || [];
    const signature = JSON.stringify(lists);
    if (picker.dataset.signature !== signature) {
      picker.replaceChildren();
      const fresh = document.createElement('option'); fresh.value = '__new__'; fresh.textContent = 'Neue Fassung'; picker.append(fresh);
      for (const item of lists) { const option = document.createElement('option'); option.value = item.id; option.textContent = itemLabel(item); picker.append(option); }
      picker.dataset.signature = signature;
    }
    picker.value = draft ? '__new__' : lists.some(item=>item.id===current) ? current : '__new__';
    if (current !== lastSelected || !dirty) {
      const item = selected();
      if (item?.mode && !draft) { $('render-mode').value = item.mode; $('render-ultra-fast').checked = item.ultra_fast === true; }
      if (current !== lastSelected) dirty = false;
    }
    lastSelected = current;
    paint();
  }
  async function submit(policy = 'mixed') {
    if (!loaded || pending || reviewLocked()) return;
    const item = selected();
    pending = true; paint();
    try {
      const body = {mode:$('render-mode').value,ultra_fast:$('render-ultra-fast').checked && !$('render-ultra-fast').disabled,list_policy:policy};
      if (policy === 'mixed' && item) body.list_id = item.id;
      if (policy === 'replace' && item) body.replace_list_id = item.id;
      const result = await post('/api/render/start',body);
      if (result.requires_action === 'completed_fassung') { pending = false; update(result); showRestart(); return; }
      draft = false; dirty = false; update(result);
      message('Die gewählte Fassung wird verarbeitet. Vorhandene passende Aufnahmen werden weiterverwendet.');
      await window.refreshLauncherFlow?.();
    } catch (error) { message(error.message,true); }
    finally { pending = false; paint(); }
  }
  function showRestart() {
    const item = selected();
    $('fassung-restart-description').textContent = 'Diese Fassung wurde bereits vollständig gerendert (' + category(item) + ', ' + date(item?.completed_at) + '). Möchtest du sie ersetzen oder eine weitere Fassung erstellen?';
    $('fassung-restart-dialog').showModal();
  }
  $('render-list').onchange = async () => {
    if (!loaded || pending || reviewLocked()) return;
    dirty = false;
    if ($('render-list').value === '__new__') { draft = true; paint(); return; }
    draft = false; pending = true; paint();
    try { update(await post('/api/render/select',{list_id:$('render-list').value})); await window.refreshLauncherFlow?.(); }
    catch (error) { message(error.message,true); }
    finally { pending = false; paint(); }
  };
  $('fassung-new').onclick = () => { $('fassung-restart-dialog').close(); submit('new'); };
  $('fassung-replace').onclick = () => { $('fassung-restart-dialog').close(); submit('replace'); };
  $('fassung-cancel').onclick = () => $('fassung-restart-dialog').close();
  $('repair-options').onclick = () => window.LauncherWizard.show('repair');
  window.FassungControls = {
    update, paint(value) { if (value) constraints = {...constraints,...value}; paint(); },
    page(value) { page = value; paint(); },
    modeChanged() { dirty = true; paint(); },
    start() { if (!loaded || pending || reviewLocked()) return; if (selected()?.status === 'completed') showRestart(); else submit(selected()?'mixed':'new'); }
  };
  paint();
})();
