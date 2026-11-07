/* Render log text as text nodes, never HTML; command IDs bind parallel results. */
(() => {
  function paint(log,text) {
    const lines=String(text).split('\n'),results=new Map();
    for(const line of lines){const m=line.match(/\[(OK|FEHLER) #(\d+)\]/);if(m)results.set(m[2],m[1]);}
    const nodes=lines.map((line,index)=>{
      const span=document.createElement('span'),command=line.match(/\[(RUN|OK|FEHLER) #(\d+)\]/);
      let kind='normal';
      if(command){const status=results.get(command[2])||command[1];kind=status==='OK'?'success':status==='FEHLER'?'error':'running';line=line.replace(command[0],'['+status+' #'+command[2]+']');}
      else if(/(?:^|\s)\$\s/.test(line))kind='running';
      else if(/\b(?:warning|warnung)\b/i.test(line))kind='warning';
      else if(/(?:\b(?:error|fehler):|\b(?:fehlgeschlagen|abgestürzt)\b)/i.test(line))kind='error';
      else if(/\b(?:geprüft|abgeschlossen|erfolgreich)\b/i.test(line))kind='success';
      span.className='terminal-line terminal-'+kind;
      if(command){
        const marker=line.match(/\[(RUN|OK|FEHLER) #\d+\]/),position=line.indexOf(marker[0]);
        span.textContent=line.slice(0,position);
        const badge=document.createElement('span');badge.className='terminal-badge';badge.textContent=marker[0];span.append(badge);
        const tail=document.createElement('span');tail.className='terminal-command';tail.textContent=line.slice(position+marker[0].length);span.append(tail);
        if(index<lines.length-1)span.append(document.createTextNode('\n'));
      }else span.textContent=line+(index<lines.length-1?'\n':'');return span;
    });
    log.replaceChildren(...nodes);
  }
  window.LauncherTerminal={paint};
})();
