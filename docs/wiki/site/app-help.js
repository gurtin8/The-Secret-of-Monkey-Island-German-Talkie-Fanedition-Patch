/* One help hint in the existing console toolbar; no duplicate page controls. */
(() => {
  const scriptURL = document.currentScript?.src;
  const wikiURL = scriptURL ? new URL('index.html', scriptURL).href : new URL('/wiki/index.html', location.href).href;
  const start = () => {
    window.MI1Help.install({source: document.body.dataset.helpSource, defaultTopic: 'wiki.start', wikiURL});
    const exportButton = document.querySelector('#export-log, #launcher-log-export');
    if (!exportButton || document.querySelector('.mi1-help-entry')) return;
    const hint = document.createElement('span'); hint.className = 'mi1-help-entry';
    const text = document.createElement('span'); text.textContent = 'Hilfe: Shift+Klick · ? / F1';
    text.title = 'Shift+Klick auf ein Feld oder eine Taste öffnet Hilfe. Fokussierte Taste: ? oder F1. In Texteingaben bleibt ? normale Eingabe.';
    const link = document.createElement('a'); link.href = '#wiki.start'; link.dataset.helpOpen = 'wiki.start';
    link.className = 'mi1-help-link'; link.textContent = '? Hilfe / Wiki';
    hint.append(text, link); exportButton.before(hint);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true});
  else start();
})();
