/* Own-code context-help adapter. No host writes or network requests. */
(() => {
  'use strict';
  const data = window.MI1_WIKI;
  if (!data || data.format !== 'mi1-help/1') throw new Error('MI1-Hilfedaten fehlen oder haben ein unbekanntes Format.');
  const topics = new Map(data.topics.map(t => [t.id, t]));
  let dialog, title, body, search, results, returnFocus, settings = {};
  const element = (tag, text, cls) => { const e = document.createElement(tag); if (text) e.textContent = text; if (cls) e.className = cls; return e; };
  const get = id => topics.get(id) || null;
  const inlineLink = () => /\[([^\]\n]+)\]\(([a-z][a-z0-9.-]+)\)/g;
  const statusNames={manual:'Manuell bestätigt',clean:'Automatisch unauffällig',repaired:'Automatisch repariert',open:'Offen',warning:'Warnung',invalid:'Fehler',active:'Wird erzeugt'};
  const statusToken=/\{\{review-status:(manual|clean|repaired|open|warning|invalid|active)\}\}/g;
  const assetURL=new URL('.',document.currentScript.src);
  const plainText = text => text.replace(inlineLink(), '$1').replace(statusToken,(_,kind)=>statusNames[kind]);
  function appendText(node,text) {
    let offset=0;
    for(const match of text.matchAll(statusToken)) {
      node.append(document.createTextNode(text.slice(offset,match.index)));
      const icon=element('img',null,'mi1-review-status-icon'+(match[1]==='active'?' mi1-review-status-active':''));
      icon.src=new URL(`status-${match[1]}.svg`,assetURL).href;
      icon.alt=statusNames[match[1]];icon.width=24;icon.height=24;
      node.append(icon);offset=match.index+match[0].length;
    }
    node.append(document.createTextNode(text.slice(offset)));
  }
  function paragraph(tag, text, cls, linkHandler) {
    const node = element(tag, null, cls);
    let offset = 0;
    for (const match of text.matchAll(inlineLink())) {
      appendText(node,text.slice(offset, match.index));
      if (get(match[2])) {
        const link = element('a', match[1]);
        link.href = '#' + encodeURIComponent(match[2]);
        if (linkHandler) link.addEventListener('click', event => { event.preventDefault(); linkHandler(match[2]); });
        node.append(link);
      } else node.append(document.createTextNode(match[1]));
      offset = match.index + match[0].length;
    }
    appendText(node,text.slice(offset));
    return node;
  }
  function surface(source) {
    if (/generator_launcher\/native\//.test(source || '')) return 'native-start';
    if (/generator_launcher\//.test(source || '')) return 'launcher';
    if (/patch_review\//.test(source || '')) return 'review';
    return source;
  }
  function resolve(target, source = settings.source) {
    if (!(target instanceof Element)) return null;
    const explicit = target.closest('[data-help-id], [data-help-open]');
    if (explicit) return explicit.dataset.helpOpen || explicit.dataset.helpId;
    const candidates = [];
    for (const binding of data.bindings) {
      if (surface(binding.source) !== surface(source)) continue;
      try {
        const match = target.closest(binding.selector);
        if (!match) continue;
        let distance = 0, current = target;
        while (current && current !== match) { distance++; current = current.parentElement; }
        const specificity = (binding.selector.match(/#/g) || []).length * 100 + (binding.selector.match(/[.\[:]/g) || []).length * 10 + binding.selector.length / 1000;
        candidates.push({topic: binding.topic, distance, specificity});
      } catch (_) { /* Invalid future selector leaves the normal action intact. */ }
    }
    candidates.sort((a, b) => a.distance - b.distance || b.specificity - a.specificity);
    return candidates[0]?.topic || null;
  }
  function content(container, topic, linkHandler) {
    container.replaceChildren();
    const prose = (tag, text, cls) => paragraph(tag, text, cls, linkHandler);
    container.append(prose('p', topic.summary, 'mi1-help-summary'));
    topic.body.forEach(p => container.append(prose('p', p)));
    (topic.sections || []).forEach(section => {
      container.append(element('h3', section.title));
      section.body.forEach(p => container.append(prose('p', p)));
      if (section.steps?.length) {
        const list = element('ol');
        section.steps.forEach(s => list.append(prose('li', s)));
        container.append(list);
      }
    });
    if (topic.steps?.length) {
      container.append(element('h3', 'Vorgehen'));
      const list = element('ol'); topic.steps.forEach(s => list.append(prose('li', s))); container.append(list);
    }
    if (topic.messages?.length) {
      const details = element('details');
      details.append(element('summary', 'Meldungen zu diesem Thema nachschlagen'));
      const list = element('ul'); topic.messages.forEach(m => list.append(element('li', m)));
      details.append(list); container.append(details);
    }
    if (topic.related?.length) {
      container.append(element('h3', 'Weiterführende Hilfe'));
      const links = element('ul');
      topic.related.forEach(id => {
        const t = get(id); if (!t) return;
        const li = element('li'), a = element('a', t.title);
        a.href = '#' + encodeURIComponent(id);
        if (linkHandler) a.addEventListener('click', e => { e.preventDefault(); linkHandler(id); });
        li.append(a); links.append(li);
      }); container.append(links);
    }
    if (topic.links?.length) {
      container.append(element('h3', 'Originalquellen und Bedingungen'));
      const list = element('ul');
      topic.links.forEach(link => {
        if (!/^https?:\/\//.test(link.url)) return;
        const li = element('li'), a = element('a', link.title);
        a.href = link.url; a.target = '_blank'; a.rel = 'noopener';
        li.append(a); list.append(li);
      });
      container.append(list);
    }
  }
  function ensureDialog() {
    if (dialog) return;
    dialog = element('dialog', null, 'mi1-help-dialog'); dialog.setAttribute('aria-labelledby', 'mi1-help-title');
    title = element('h2'); title.id = 'mi1-help-title'; body = element('div', null, 'mi1-help-body');
    const toolbar = element('div', null, 'mi1-help-toolbar');
    const close = element('button', 'Schließen'); close.type = 'button'; close.autofocus = true; close.addEventListener('click', () => dialog.close());
    toolbar.append(title, close);
    const finder = element('div', null, 'mi1-help-finder');
    search = element('input'); search.type = 'search'; search.placeholder = 'Wiki oder Fehlermeldung durchsuchen'; search.setAttribute('aria-label', 'Wiki durchsuchen');
    results = element('div', null, 'mi1-help-results'); results.setAttribute('aria-live', 'polite');
    search.addEventListener('input', () => {
      results.replaceChildren(); const query = search.value.trim().toLocaleLowerCase('de'); if (!query) return;
      const matches = data.topics.filter(t => [t.title,t.summary,...t.body,...(t.steps || []),
        ...(t.sections || []).flatMap(s => [s.title,...s.body,...(s.steps || [])])].map(plainText).join(' ').toLocaleLowerCase('de').includes(query));
      const messageMatches = data.messages.filter(m => m.message.toLocaleLowerCase('de').includes(query));
      const entries = [...matches.map(t => ({id:t.id,title:t.title})), ...messageMatches.map(m => ({id:m.topic,title:m.message}))];
      entries.slice(0,20).forEach(t => {const b=element('button',t.title);b.type='button';b.addEventListener('click',()=>open(t.id));results.append(b);});
      if (!entries.length) results.append(element('p','Keine Treffer. Suche verkürzen.'));
      else if (entries.length > 20) results.append(element('p',`${entries.length} Treffer – Suche eingrenzen.`));
    });
    finder.append(search, results); dialog.append(toolbar, finder, body); document.body.append(dialog);
    // A search input can consume Escape to clear its query; the help gesture closes the dialog.
    dialog.addEventListener('keydown', event => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopPropagation();
      dialog.close();
    }, true);
    dialog.addEventListener('close', () => { if (returnFocus?.isConnected) returnFocus.focus(); });
  }
  function open(id) {
    const topic = get(id); if (!topic) return false;
    ensureDialog(); title.textContent = topic.title; content(body, topic, open);
    search.value = ''; results.replaceChildren(); body.scrollTop = 0;
    if (settings.wikiURL) {
      const a = element('a', 'In der vollständigen Wiki lesen'); a.href = settings.wikiURL.split('#')[0] + '#' + encodeURIComponent(id); a.target = '_blank'; a.rel = 'noopener'; body.append(a);
    }
    if (!dialog.open) { returnFocus = document.activeElement; dialog.showModal(); }
    return true;
  }
  function findMessage(text) {
    if (typeof text !== 'string' || !text.trim()) return [];
    return data.messages.filter(m => {
      const parts = m.message.split(/\{[^{}]*\}/).map(p => p.trim()).filter(Boolean);
      if (!parts.length) return false;
      let offset = 0;
      for (const part of parts) { const index = text.indexOf(part, offset); if (index < 0) return false; offset = index + part.length; }
      return true;
    }).map(m => ({id: m.id, topic: m.topic, message: m.message, explanation_level: m.explanation_level}));
  }
  let installed = false;
  function install(options = {}) {
    settings = {...settings, ...options}; if (installed) return; installed = true;
    document.addEventListener('pointerdown', event => {
      if (!event.shiftKey || !(event.target instanceof Element) || !event.target.closest(':disabled')) return;
      const id = resolve(event.target); if (!get(id)) return;
      event.preventDefault(); event.stopImmediatePropagation(); open(id);
    }, true);
    document.addEventListener('click', event => {
      const direct = event.target instanceof Element && event.target.closest('[data-help-open]');
      if (!event.shiftKey && !direct) return;
      const id = resolve(event.target); if (!get(id)) return;
      event.preventDefault(); event.stopImmediatePropagation(); open(id);
    }, true);
    document.addEventListener('keydown', event => {
      if (event.key !== 'F1' && event.key !== '?') return;
      if (event.altKey || event.ctrlKey || event.metaKey || dialog?.open) return;
      const target = document.activeElement;
      const editable = target?.closest?.('input, textarea, select, [contenteditable="true"], [contenteditable=""]');
      if (event.key === '?' && editable) return;
      const id = resolve(target) || settings.defaultTopic; if (!get(id)) return;
      event.preventDefault(); event.stopImmediatePropagation(); open(id);
    }, true);
  }
  window.MI1Help = {get, resolve, open, install, findMessage, render: content, plainText};
})();
