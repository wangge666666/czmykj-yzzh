"""Real offline stdio -> HTTP daemon -> FFmpeg -> persistent artifact smoke check."""
import argparse
import json
import os
import subprocess
import sys
import time
import socket
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workflow_core import resolve_ffmpeg, inspect_video


def start_account_fixture():
    from flask import Flask, jsonify, request
    from werkzeug.serving import make_server, WSGIRequestHandler
    app = Flask('synthetic-account-only')
    @app.post('/api/auth/login')
    def login():
        if request.json != {'username':'synthetic-fixture','password':'synthetic-fixture-password','role':'customer'}:
            return jsonify(success=False), 401
        return jsonify(success=True, token='synthetic-fixture-token')
    @app.post('/api/auth/verify-token')
    def verify():
        if request.json != {'token':'synthetic-fixture-token'}:
            return jsonify(valid=False), 401
        return jsonify(valid=True, userId=700001, username='合成验收账号', balance=0,
                       subscriptions=[{'productId':4,'status':'active','endTime':'2100-01-01'}])
    class Quiet(WSGIRequestHandler):
        def log(self, *args, **kwargs): pass
    server = make_server('127.0.0.1',0,app,request_handler=Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--evidence-dir', required=True)
    parser.add_argument('--bundle', help='Test the extracted customer launch.py instead of repository MCP')
    args = parser.parse_args()
    data_root = Path(args.data_dir).resolve()
    if data_root.exists():
        raise SystemExit('Use a NEW isolated data directory; existing customer state must not be used.')
    evidence = Path(args.evidence_dir).resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    fixture = evidence / 'synthetic-test-pattern.mp4'
    subprocess.run([str(resolve_ffmpeg()), '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=12',
                    '-t', '4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(fixture)], check=True)
    server, server_thread = start_account_fixture()
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0)); port = listener.getsockname()[1]
    runtime_root = Path(args.bundle).resolve() / 'runtime' if args.bundle else ROOT
    daemon_python = Path(args.bundle).resolve()/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python') if args.bundle else Path(sys.executable)
    daemon = subprocess.Popen([str(daemon_python),'-m','yzzh_local.app','--data-dir',str(data_root),
        '--port',str(port),'--development','--login-url',f'http://127.0.0.1:{server.server_port}/api/auth/login'],
        cwd=runtime_root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic()+20
    while not (data_root/'connection.json').exists():
        if daemon.poll() is not None or time.monotonic()>deadline:
            daemon.terminate(); server.shutdown(); server.server_close()
            raise RuntimeError('Isolated fixture daemon did not start')
        time.sleep(.1)
    # Only this NEW fixture's session is read. Never inspect a real user daemon.
    info=json.loads((data_root/'connection.json').read_text())
    import requests
    response=requests.post(f'http://127.0.0.1:{port}/api/login',
        headers={'X-Yzzh-Session':info['session'],'X-Yzzh-Request':'1'},
        json={'username':'synthetic-fixture','password':'synthetic-fixture-password'},timeout=5)
    assert response.status_code==200 and response.json()['licensed'], 'fixture login failed'
    command = [sys.executable, str(Path(args.bundle).resolve() / 'scripts' / 'launch.py')] if args.bundle else [sys.executable, '-m', 'yzzh_local.mcp']
    child = subprocess.Popen(command, cwd=Path(args.bundle).resolve() if args.bundle else ROOT,
        env={**os.environ, 'YZZH_DATA_DIR': str(data_root)},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    counter = 0
    def rpc(method, params=None):
        nonlocal counter
        counter += 1
        child.stdin.write(json.dumps({'jsonrpc':'2.0', 'id':counter, 'method':method, 'params':params or {}}) + '\n')
        child.stdin.flush()
        line = child.stdout.readline()
        if not line: raise RuntimeError('MCP exited before response')
        result = json.loads(line)
        if 'error' in result: raise RuntimeError(result['error'])
        return result['result']
    def tool(name, **arguments):
        result = rpc('tools/call', {'name':name, 'arguments':arguments})
        if result.get('isError'): raise RuntimeError(result)
        return json.loads(result['content'][0]['text'])
    try:
        initialized = rpc('initialize', {'protocolVersion':'2025-06-18', 'clientInfo':{'name':'offline-smoke','version':'1'}, 'capabilities':{}})
        child.stdin.write(json.dumps({'jsonrpc':'2.0','method':'notifications/initialized'}) + '\n'); child.stdin.flush()
        names = [x['name'] for x in rpc('tools/list')['tools']]
        project = tool('import_video', path=str(fixture))
        project_id = project['id']; source_id = next(iter(project['artifacts']))
        tool('process', project_id=project_id, artifact_id=source_id, operation='split')
        deadline = time.monotonic() + 30
        while True:
            state = tool('status', project_id=project_id)
            if state['state'] != 'processing': break
            if time.monotonic() > deadline: raise RuntimeError('Local processing timed out')
            time.sleep(.1)
        assert state['state'] == 'ready', state
        split_id = next(k for k,v in state['artifacts'].items() if v['kind'] == 'split')
        export = tool('export', project_id=project_id, artifact_id=split_id, destination=str(evidence / 'export.mp4'))
        subprocess.run([str(resolve_ffmpeg()), '-v', 'error', '-i', export['path'], '-f', 'null', '-'], check=True)
        original_job = tool('original_analyze', path=str(fixture), project='virtual', manual_cuts='2')
        original_id = original_job['id']
        deadline = time.monotonic() + 30
        while original_job['status'] not in {'succeeded','failed'}:
            if time.monotonic() > deadline: raise RuntimeError('Original workflow timed out')
            time.sleep(.1)
            original_job = tool('original_status', job_id=original_id)
        assert original_job['status'] == 'succeeded' and len(original_job['shots']) == 2, original_job.get('error')
        state = requests.get(f'http://127.0.0.1:{port}/api/state',headers={'X-Yzzh-Session':info['session']},timeout=5).json()
        media_headers={'X-Yzzh-Session':info['session'],'X-Yzzh-Owner':str(state['account']['user_id']),
                       'X-Yzzh-Context':state['settings']['session_revision']}
        media = requests.get(f'http://127.0.0.1:{port}/api/jobs/{original_id}/shots/1/file/source',headers=media_headers,timeout=10)
        assert media.status_code == 200
        original_export = evidence / 'original-shot.mp4'
        original_export.write_bytes(media.content)
        subprocess.run([str(resolve_ffmpeg()),'-v','error','-i',str(original_export),'-f','null','-'],check=True)
        report = {'protocol':initialized['protocolVersion'], 'tools':names, 'project_id':project_id,
                  'client': 'extracted-customer-bundle' if args.bundle else 'repository-source',
                  'state':'ready', 'export':export, 'duration':inspect_video(export['path']).duration,
                  'original_workflow':{'job_id':original_id,'status':original_job['status'],'shots':2,'decoded':True,
                                       'artifact':str(original_export)},
                  'version':initialized['serverInfo']['version'], 'mode':'byok',
                  'boundary':'Loopback fixture product-4 login + real synthetic local processing; no real account, provider, billing or installed host acceptance.'}
        (evidence / 'runtime-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(report, ensure_ascii=False))
    finally:
        child.stdin.close()
        child.wait(timeout=10)
        child.stdout.close(); child.stderr.close()
        daemon.terminate(); daemon.wait(timeout=10)
        server.shutdown(); server_thread.join(3); server.server_close()


if __name__ == '__main__':
    main()
