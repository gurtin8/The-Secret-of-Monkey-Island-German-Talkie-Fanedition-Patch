/* Page changes never start work. Only the native explicit download action may
   pass --start-setup; all other actions remain their existing POST controls. */
(() => {
  const pages = ['downloads','preparation','render','repair'];
  const state = {setupReady:false, setupBusy:false, gameReady:false, gameBusy:false, gameInterrupted:false, renderStarted:false, renderReady:false, renderBusy:false, repairBusy:false};
  let page = new URLSearchParams(location.search).get('page')==='render'?'render':'downloads';
  const visited = new Set([page]);
  let preparationRestored = false;
  const $ = id => document.getElementById(id);
  function paint() {
    window.LauncherConsole?.focus({downloads:'setup',preparation:'game',render:'render',repair:'repair'}[page]);
    const index=pages.indexOf(page), busy=state.setupBusy||state.gameBusy||state.renderBusy||state.repairBusy;
    document.querySelectorAll('[data-page]').forEach(e=>e.hidden=e.dataset.page!==page);
    document.querySelectorAll('[data-step]').forEach(e=>{if(e.dataset.step===page)e.setAttribute('aria-current','step');else e.removeAttribute('aria-current');});
    $('wizard-back').disabled=index===0||(page==='render'&&state.renderStarted);
    $('wizard-next').hidden=index>=2;
    const ready=[state.setupReady,state.gameReady,state.renderReady][index];
    const returning=index<2&&visited.has(pages[index+1]);
    const preparationAvailable = index===0 && (state.gameBusy || state.gameInterrupted);
    $('wizard-next').disabled=!returning&&!preparationAvailable&&(!ready||busy);
    $('wizard-next').textContent=['Weiter zur Patch-Vorbereitung','Weiter zu „Sprachausgabe rendern“','Weiter zur Review'][index]||'Weiter';
    window.FassungControls?.page(page);
    $('wizard-next-hint').textContent=preparationAvailable?'In der Patch-Vorbereitung findest du die geöffneten Fenster und den nächsten Schritt.':returning?'Du kannst zur bereits besuchten Seite zurückkehren; der laufende Auftrag arbeitet dabei weiter.':busy?'Der laufende Auftrag wird hier weiter angezeigt.':ready?'Dieser Schritt ist bereit. Du kannst weitergehen.':['Warte, bis die Downloads und die anschließende Einrichtung abgeschlossen sind.','Folge den Patchdialogen, bis die Konsole den Abschluss der Vorbereitung meldet.','Wähle die Stimmen und den Rendermodus und starte die Spracherzeugung. Sobald alle Aufnahmen geprüft sind, geht es zur Review.','Die automatische Reparatur ist optional. In der Review hörst du die Aufnahmen an und stellst danach deine spielbare Fassung zusammen.'][index];
  }
  function show(next) {if(!pages.includes(next))return;if(next==='render'&&(state.gameBusy||state.gameInterrupted))next='preparation';page=next;visited.add(next);paint();window.scrollTo(0,0);$(page==='repair'?'repair-title':page+'-title')?.focus({preventScroll:true});}
  $('wizard-back').onclick=()=>{if(!$('wizard-back').disabled)show(pages[pages.indexOf(page)-1]);};
  $('wizard-next').onclick=()=>{if(!$('wizard-next').disabled)show(pages[pages.indexOf(page)+1]);};
  window.LauncherWizard={update(value){Object.assign(state,value);if(!preparationRestored&&(state.gameBusy||state.gameInterrupted)){preparationRestored=true;show('preparation');}else paint();},show};
  window.LauncherConsents={packages:false,wine:false,game:false};
  fetch('/api/launcher/consents',{headers}).then(async r=>{if(!r.ok)throw Error('Native Bestätigungen nicht verfügbar');return r.json();}).then(v=>{
    window.LauncherConsents=v.consents;
    const complete=['packages','wine','game'].every(key=>v.consents[key]===true);
    $('consents-status').textContent=complete?'Die Bestätigungen aus dem Startschritt wurden übernommen.':'Gehe im Patch zum Startschritt zurück und bestätige die erforderlichen Angaben vor dem Download.';
    $('acquire').disabled=!v.consents.packages||setupBusy;
    window.updateLauncherLocks?.();
  }).catch(e=>{$('consents-status').textContent=e.message;window.LauncherConsole?.update('setup',e.message,[],{status:'failed'});});
  paint();
})();
