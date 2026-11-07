"""Same endpoint dispatcher for standalone and Wizard hosts."""
import json,mimetypes
from pathlib import Path
from urllib.parse import urlparse,parse_qs
PREFIX='/api/patch-review'
ASSETS=Path(__file__).resolve().parent


def dispatch(service,method,path,payload=None):
    parsed=urlparse(path);route=parsed.path
    if method=='GET' and route.startswith('/wiki/'):
        name=route.removeprefix('/wiki/')
        if name not in ('index.html','data.js','help.js','help.css','wiki.js','wiki.css','app-help.js','help-index.json',
                        *(f'status-{kind}.svg' for kind in ('manual','clean','repaired','open','warning','invalid','active'))):return 404,b'', 'text/plain'
        target=ASSETS.parent/'docs/wiki/site'/name
        return 200,target.read_bytes(),mimetypes.guess_type(name)[0] or 'text/plain'
    if method=='GET' and route.startswith('/patch-review/'):
        name=route.removeprefix('/patch-review/')
        if name not in ('index.html','app.js','styles.css'):return 404,b'', 'text/plain'
        target=ASSETS/name
        return 200,target.read_bytes(),mimetypes.guess_type(name)[0] or 'text/plain'
    if not route.startswith(PREFIX+'/'):return None
    try:
        action=route[len(PREFIX)+1:]
        if service is None:raise ValueError('Reviewquelle ist nicht konfiguriert')
        if method=='GET':
            if action=='bootstrap':body=service.bootstrap()
            elif action=='rows':
                q=parse_qs(parsed.query)
                ids=q.get('dialog_id')
                body=service.rows_payload(ids)
            elif action=='status':body=service.status()
            elif action=='build-status':body=service.patch_builder().status(output_root=parse_qs(parsed.query).get('output_root',[None])[0])
            elif action=='build-report':body=service.patch_builder().report()
            elif action=='export':body=service.export()
            elif action=='recipe-export':body=service.export_recipe()
            elif action=='audio':
                q=parse_qs(parsed.query);p=service.evidence.audio(q.get('key',[''])[0],q.get('sha256',[''])[0]);return 200,p.read_bytes(),mimetypes.guess_type(p.name)[0] or 'audio/wav'
            else:return 404,b'', 'text/plain'
        elif method=='POST':
            if getattr(service,'launcher_readonly',lambda:False)():raise RuntimeError('Eine andere Fassung wird verarbeitet. Diese Review ist zum Anhören geöffnet; Änderungen und Export sind erst danach möglich.')
            if not isinstance(payload,dict):raise ValueError('JSON-Objekt erforderlich')
            if action in ('update','mark','good','release','release-original','restore-previous'):body=service.mutate(action,payload)
            elif action=='audition-queue':body=service.mutate_audition_queue(payload)
            elif action=='import':body=service.import_extension(payload)
            elif action=='recipe-export':body=service.save_recipe(payload)
            elif action=='export':body=service.save_extension(payload)
            elif action=='todo-diagnostic':body=service.save_todo_diagnostic(payload)
            elif action=='start':body=service.start(payload)
            elif action=='build':body=service.patch_builder().start(payload)
            else:return 404,b'', 'text/plain'
        else:return 405,b'', 'text/plain'
        return (202 if action in ('start','build') else 200),json.dumps(body,ensure_ascii=False).encode(),'application/json; charset=utf-8'
    except RuntimeError as exc:return 409,json.dumps(dict(error=str(exc))).encode(),'application/json'
    except FileNotFoundError as exc:return 404,json.dumps(dict(error=str(exc))).encode(),'application/json'
    except OSError as exc:return 500,json.dumps(dict(error='Dateizugriff bei Patchreview fehlgeschlagen: '+str(exc))).encode(),'application/json'
    except (ValueError,KeyError,TypeError) as exc:return 400,json.dumps(dict(error=str(exc))).encode(),'application/json'
