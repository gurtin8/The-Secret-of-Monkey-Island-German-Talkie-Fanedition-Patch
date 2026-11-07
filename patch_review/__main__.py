"""Standalone manifest review; rendering is available unless explicitly disabled."""
import argparse,json,signal
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from .service import ReviewService
from .http import dispatch


def handler_for(service):
    class Handler(BaseHTTPRequestHandler):
        def run_request(self,method):
            payload=None
            if method=='POST':
                origin=self.headers.get('Origin');expected=f'http://127.0.0.1:{self.server.server_port}'
                if origin and origin!=expected:return self.respond(403,b'{"error":"Fremder Ursprung"}','application/json')
                try:
                    n=int(self.headers.get('Content-Length','0'))
                    if not 0<n<=32*1024*1024 or self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('Ungültige Anfrage')
                    payload=json.loads(self.rfile.read(n))
                except (ValueError,UnicodeError):return self.respond(400,'{"error":"Ungültiges JSON"}'.encode(),'application/json')
            result=dispatch(service,method,'/patch-review/index.html' if self.path in ('/','/index.html') else self.path,payload)
            return self.respond(*(result or (404,b'','text/plain')))
        def respond(self,status,body,content):
            self.send_response(status);self.send_header('Content-Type',content);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; media-src 'self'; object-src 'none'");self.end_headers();self.wfile.write(body)
        def do_GET(self):self.run_request('GET')
        def do_POST(self):self.run_request('POST')
        def log_message(self,*args):pass
    return Handler


def parse_args(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('manifest','asset-root','run-root','state-root'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--port',type=int,default=8770)
    p.add_argument('--enable-render',action='store_true',help='Legacy-Kompatibilitätsflag; Rendering ist standardmäßig verfügbar')
    p.add_argument('--read-only',action='store_true',help='Produktionsrenderer deaktivieren; vorhandene Belege bleiben sichtbar')
    p.add_argument('--ane-worker-config',type=Path)
    p.add_argument('--game-root',type=Path,help='Quellordner der deutschen Ultimate Talkie Edition 1.02')
    p.add_argument('--ffmpeg',type=Path,help='FFmpeg-Programm für die Spielausgabe')
    return p.parse_args(argv)


def main():
    a=parse_args()
    service=build_service(a)
    server=ThreadingHTTPServer(('127.0.0.1',a.port),handler_for(service));print(f'Patchreview: http://127.0.0.1:{server.server_port}/',flush=True);print('Zustand: '+str(service.path),flush=True)
    def stop_on_sigterm(signum,frame):raise SystemExit(128+signum)
    previous_sigterm=signal.signal(signal.SIGTERM,stop_on_sigterm)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        signal.signal(signal.SIGTERM,previous_sigterm)
        server.server_close()
        close=getattr(service.executor,'close',None)
        if close:close()


def build_service(args):
    """Configure the adapter without creating an executor, loading models, or starting a job."""
    service=ReviewService(args.manifest,args.asset_root,args.run_root,args.state_root,game_root=getattr(args,'game_root',None),ffmpeg=getattr(args,'ffmpeg',None))
    if not args.read_only and service.evidence.valid:
        from .render import ProductionAdapter
        service.executor=ProductionAdapter(args.asset_root,ane_config=json.loads(args.ane_worker_config.read_text()) if args.ane_worker_config else None)
    return service

if __name__=='__main__':main()
