(() => {
  'use strict';
  const data = window.MI1_WIKI, help = window.MI1Help;
  const $ = id => document.getElementById(id), article = $('article'), search = $('search'), category = $('category');
  const el = (tag, text, cls) => {const e = document.createElement(tag); if (text) e.textContent = text; if (cls) e.className = cls; return e;};
  const normalize = text => text.toLocaleLowerCase('de').normalize('NFD').replace(/\p{Diacritic}/gu, '');
  const searchable = new Map(data.topics.map(t => [t.id, normalize([t.title, t.summary, ...t.body, ...(t.steps || []),
    ...(t.sections || []).flatMap(s => [s.title, ...s.body, ...(s.steps || [])]), ...(t.messages || []), t.id].map(help.plainText).join(' '))]));
  const orderedTopics = [...data.topics].sort((a,b) => (a.navigation_order ?? 1000) - (b.navigation_order ?? 1000) || a.title.localeCompare(b.title,'de'));
  let limit = 40;
  if (window.matchMedia('(max-width:760px)').matches) $('wiki-navigation').open = false;
  $('version').textContent = `Hilfe zur Patch-Version ${data.app_version}`;
  [...new Set(data.topics.map(t => t.category))].sort((a,b) => a.localeCompare(b,'de')).forEach(c => {const opt=el('option',c); opt.value=c; category.append(opt);});
  function link(id, title, container) {const a=el('a',title); a.href='#'+encodeURIComponent(id); if (location.hash.slice(1)===id) a.setAttribute('aria-current','page'); container.append(a);}
  function listing() {
    const words=normalize(search.value.trim()).split(/\s+/).filter(Boolean);
    const contains = text => words.every(w=>text.includes(w));
    const showAll = $('all-topics').checked || words.length || category.value;
    const topics=orderedTopics.filter(t=>(showAll || Number.isFinite(t.navigation_order)) && (!category.value || t.category===category.value) && contains(searchable.get(t.id)));
    const messages=words.length && (!category.value || category.value==='Diagnose') ? data.messages.filter(m=>contains(normalize(m.message))) : [];
    $('topics').replaceChildren(); $('messages').replaceChildren();
    topics.forEach(t=>link(t.id,t.title,$('topics')));
    if (messages.length) {
      $('messages').append(el('h2','Meldungen'));
      messages.slice(0,limit).forEach(m=>link(m.id,m.message,$('messages')));
      if(messages.length>limit) {const more=el('button',`Weitere Meldungen (${messages.length-limit})`); more.type='button'; more.onclick=()=>{limit+=40;listing();};$('messages').append(more);}
    }
    $('result-count').textContent=`${topics.length} ${showAll ? 'Themen' : 'Anleitungen'}${messages.length ? ` und ${messages.length} Meldungen` : ''}${!topics.length&&!messages.length?' · Suche verkürzen oder Bereich wechseln.':''}`;
  }
  function route() {
    let id; try{id=decodeURIComponent(location.hash.slice(1)) || 'wiki.start';}catch(_){id='';}
    const message=data.messages.find(m=>m.id===id), topic=help.get(message?message.topic:id);
    article.replaceChildren();
    if(!topic) {article.append(el('h1','Hilfeseite nicht gefunden'),el('p','Dieser Verweis führt zu keiner Seite in deiner Wiki-Fassung. Über die Suche findest du die gewünschte Funktion; auf der Startseite kannst du auch wieder beim vollständigen Ablauf einsteigen.'));link('wiki.start','Zur Startseite',article);document.title='Hilfe nicht gefunden · MI German Talkie Patch';listing();return;}
    const label=el('p',topic.category,'eyebrow'); article.append(label,el('h1',message?'Meldung verstehen':topic.title));
    document.title=(message?'Meldung verstehen':topic.title)+' · MI German Talkie Patch';
    if(message) {
      article.append(el('pre',message.message,'message-text'));
      article.append(el('p',message.explanation_level==='specific'?'Hier findest du die Erklärung zu dieser Meldung und den passenden nächsten Schritt.':'Zu dieser Art von Meldung findest du hier Hinweise für die Fehlersuche. Lies dazu auch die konkrete Meldung im Protokoll, um den passenden Schritt für deinen Fall zu wählen.','notice'));
      article.append(el('h2',topic.title));
    }
    const toolbar=el('div',null,'article-tools'), button=el('button','? Erklärung öffnen');button.type='button';button.dataset.helpOpen=topic.id; toolbar.append(button);
    const direct=el('a','Link zu dieser Seite');direct.href='#'+encodeURIComponent(id);toolbar.append(direct);article.append(toolbar);
    const text=el('div');help.render(text,topic);article.append(text);
    if(message) {const details=el('details'), summary=el('summary','Technische Fundstellen');details.append(summary);message.locations.forEach(l=>details.append(el('p',`${l.source}:${l.line}`)));article.append(details);}
    listing();
  }
  search.addEventListener('input',()=>{limit=40;listing();});category.addEventListener('change',()=>{limit=40;listing();});
  $('all-topics').addEventListener('change',listing);
  window.addEventListener('hashchange',()=>{route();article.focus({preventScroll:true});window.scrollTo(0,0);});
  help.install();route();
})();
