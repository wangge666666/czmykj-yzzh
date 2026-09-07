"""Two-origin browser fixture for the real portal launcher and local SSO code.

No real login, native scheme registration/wake, provider settings or media. Keep
the visible 'synthetic fixture' label; this is not production login acceptance.
"""
import json
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server, WSGIRequestHandler
from yzzh_local.runtime import Runtime
from yzzh_local.app import create_app


def main():
    portal_root = ROOT.parent / 'CZMIYOU_kehudengru'
    compiled = subprocess.run(['node','-e',
        'const fs=require("fs"),ts=require("typescript");process.stdout.write(ts.transpileModule(fs.readFileSync("src/lib/yzzh-launcher.ts","utf8"),{compilerOptions:{module:ts.ModuleKind.ES2022,target:ts.ScriptTarget.ES2022}}).outputText);'],
        cwd=portal_root, check=True, text=True, capture_output=True).stdout
    portal = Flask('synthetic-portal-handoff')
    counts = {'verify':0}

    @portal.get('/')
    def index():
        return '''<!doctype html><html lang="zh-CN"><head><title>合成账号 · 统一入口验证</title>
        <script type="module" src="/fixture.js"></script></head><body>
        <h1>合成账号：统一入口验证（非真实授权）</h1><p>本机已启动；此测试不注册或唤醒操作系统协议，不调用模型。</p>
        <button id="open">打开衣装智换 · 测试已运行分支</button><p id="state">等待点击</p></body></html>'''

    @portal.get('/launcher.js')
    def module():
        return Response(compiled, mimetype='text/javascript')

    @portal.get('/fixture.js')
    def fixture_js():
        return Response('''import {launchYzzh} from '/launcher.js';
        document.getElementById('open').onclick=()=>launchYzzh('synthetic-portal-only',
          result=>document.getElementById('state').textContent=result,17876,()=>{});''', mimetype='text/javascript')

    @portal.post('/api/auth/verify-token')
    def verify():
        counts['verify'] += 1
        if request.get_json() != {'token':'synthetic-portal-only'}:
            return jsonify(valid=False),401
        return jsonify(valid=True,userId=700050,username='合成统一登录账号（非真实授权）',role='customer',balance=0,
            subscriptions=[{'productId':4,'status':'active','endTime':'2100-01-01'}])

    class Quiet(WSGIRequestHandler):
        def log(self, *args, **kwargs):
            pass

    with tempfile.TemporaryDirectory(prefix='yzzh-portal-fixture-') as directory:
        runtime = Runtime(Path(directory), 'http://127.0.0.1:17875/api/auth/login', development=True)
        local = create_app(runtime, 'synthetic-local-session-only', 17876)
        @portal.get('/report')
        def report():
            return jsonify(verify_calls=counts['verify'],logged_in=bool(runtime.account),
                licensed=bool(runtime.account and runtime.account['licensed']),
                version=runtime.health()['version'],real_account=False,paid_generation=False,native_wake=False)
        servers = [make_server('127.0.0.1',17875,portal,threaded=True,request_handler=Quiet),
                   make_server('127.0.0.1',17876,local,threaded=True,request_handler=Quiet)]
        threads = [threading.Thread(target=server.serve_forever,daemon=True) for server in servers]
        for thread in threads: thread.start()
        print('Synthetic portal: http://127.0.0.1:17875; local: 17876',flush=True)
        try:
            threads[0].join()
        except KeyboardInterrupt:
            pass
        finally:
            for server in servers: server.shutdown(); server.server_close()
            local.extensions['original_bridge'].close()
            runtime.pool.shutdown(wait=True)


if __name__ == '__main__':
    main()
