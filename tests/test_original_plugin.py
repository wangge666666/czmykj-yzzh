"""Original UI/engine integration. Synthetic files and mock licensing only."""
import copy
import hashlib
import io
import json
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from werkzeug.serving import make_server, WSGIRequestHandler

from hybrid_shared import HybridError
from workflow_core import resolve_ffmpeg
from yzzh_local.app import create_app
from yzzh_local.runtime import Runtime
from yzzh_local.original import PAGES, SOURCE, RECOVERY_PATHS, OriginalBridge, uncertain_temporary_upload
from yzzh_local.media import TEMP_DESTINATION
from yzzh_local.original_worker import CURRENT, NetworkGuard, RemoteImages, RemoteImageError, destination, redact, isolated_session_init


class Quiet(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


class OriginalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.auth = Mock(login_url='https://fixture.invalid/api/auth/login')
        self.account = {'user_id':71, 'username':'fixture', 'licensed':True, 'product_id':4,
                        'subscription_end':'2030-01-01', 'license_status':'active'}
        self.auth.login.return_value = 'fixture-not-real'
        self.auth.verify.side_effect = lambda token: copy.deepcopy(self.account)
        self.runtime = Runtime(self.root / 'data', account_client=self.auth)
        self.addCleanup(lambda: self.runtime.pool.shutdown(wait=True))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); self.port = sock.getsockname()[1]
        self.app = create_app(self.runtime, 'fixture-local-session', self.port)
        self.bridge = self.app.extensions['original_bridge']
        self.addCleanup(self.bridge.close)
        self.client = self.app.test_client()
        self.base = f'http://127.0.0.1:{self.port}'
        self.headers = {'X-Yzzh-Session':'fixture-local-session','X-Yzzh-Request':'1'}

    def login(self):
        self.runtime.login('fixture','fixture-password')
        self.headers.update({'X-Yzzh-Owner':str(self.account['user_id']), 'X-Yzzh-Context':self.runtime.session_revision})

    def call(self, path, method='GET', **kwargs):
        return self.client.open(path, method=method, base_url=self.base, headers=self.headers, **kwargs)

    def operation(self):
        self.login()
        _, revision = self.runtime.settings.load(71)
        self.bridge.operations['fixture-operation'] = {'owner':71, 'session':self.runtime.session_revision,
            'revision':revision,'path':'/api/generate','method':'POST','worker_key':'fixture-worker-key'}
        return {'operation':'fixture-operation'}

    def test_every_original_page_and_asset_is_preserved(self):
        for path, name in PAGES.items():
            response = self.call(path)
            self.assertEqual(response.status_code, 200, path)
            html = response.data.decode()
            original = (SOURCE / 'web' / name).read_text()
            self.assertIn('<script src="/_plugin/portal.js"></script>', html)
            import re
            restored = re.sub(r'<meta name="yzzh-owner"[^>]*><meta name="yzzh-context"[^>]*><meta name="yzzh-service-mode"[^>]*><script src="/_plugin/portal.js"></script>', '', html)
            self.assertEqual(restored, original)
        for file in (SOURCE / 'web').iterdir():
            if file.suffix in {'.css','.js'}:
                with self.call('/static/' + file.name) as response:
                    self.assertEqual(response.data, file.read_bytes())

    def test_original_api_auth_csrf_and_stale_tab_fail_closed(self):
        self.assertEqual(self.call('/api/config').status_code,401)
        self.login()
        self.headers['X-Yzzh-Context'] = 'stale-context'
        self.assertEqual(self.call('/api/config').status_code,409)
        self.headers['X-Yzzh-Context'] = self.runtime.session_revision
        self.headers.pop('X-Yzzh-Request')
        self.assertEqual(self.call('/api/long-video/analyze',method='POST',data={}).status_code,403)
        self.assertEqual(self.call('/_plugin/engine',method='POST',json={'operation':'unknown'}).status_code,403)
        self.assertFalse(self.bridge.workers)

    def test_network_requires_current_human_decision_and_durable_one_shot(self):
        op = self.operation()
        result = self.bridge.network({**op,'event':'request','summary':{'sha256':'fixture','destination':'fixture'}},'fixture-worker-key')
        network_id = result['id']
        take = {**op,'event':'take','id':network_id}
        self.assertEqual(self.bridge.network(take,'fixture-worker-key')['state'],'awaiting_approval')
        self.bridge.decide({'id':network_id,'owner':71,'session':self.runtime.session_revision,'approved':True})
        self.assertEqual(self.bridge.network(take,'fixture-worker-key')['state'],'send_once')
        self.assertEqual(self.bridge.network(take,'fixture-worker-key')['state'],'sending')
        with self.assertRaises(HybridError):
            self.bridge.network({**op,'event':'request','summary':{}},'fixture-worker-key')
        self.bridge.network({**op,'event':'finish','id':network_id,'ok':False},'fixture-worker-key')
        reloaded = OriginalBridge(self.runtime, self.port)
        self.addCleanup(reloaded.close)
        self.assertEqual(list(reloaded.unresolved(71))[0]['state'],'uncertain')

    def test_key_or_account_change_invalidates_pending(self):
        for change in ('key','account','expiry'):
            op = self.operation()
            key = self.bridge.network({**op,'event':'request','summary':{}},'fixture-worker-key')['id']
            if change == 'key':
                self.runtime.settings.save(71, {'ARK_API_KEY':'new-fixture-key'})
            elif change == 'account':
                self.runtime.logout()
            else:
                self.account['licensed'] = False
            with self.assertRaises(HybridError):
                self.bridge.decide({'id':key,'owner':71,'session':self.runtime.session_revision,'approved':True})
            self.account['licensed'] = True

    def test_upload_diagnosis_preserves_journal_and_blocks_both_request_and_take(self):
        op = self.operation()
        summary = {'destination': TEMP_DESTINATION, 'method': 'POST',
                   'endpoint': '/resources/internals/api.php', 'action': ''}
        upload = self.bridge.network({**op, 'event': 'request', 'summary': summary}, 'fixture-worker-key')['id']
        following = self.bridge.network({**op, 'event': 'request', 'summary': {}}, 'fixture-worker-key')['id']
        for key in (upload, following):
            self.bridge.decide({'id': key, 'owner': 71, 'session': self.runtime.session_revision, 'approved': True})
        self.bridge.network({**op, 'event': 'take', 'id': upload}, 'fixture-worker-key')
        self.bridge.network({**op, 'event': 'finish', 'id': upload, 'ok': False}, 'fixture-worker-key')
        journal = self.bridge.root / '71' / 'network' / (upload + '.json')
        before = journal.read_bytes()
        unresolved = self.bridge.approvals(71, self.runtime.session_revision)['unresolved']
        self.assertEqual(unresolved[0]['diagnosis'], 'temporary_upload_uncertain')
        self.assertEqual(unresolved[0]['state'], 'uncertain')
        self.assertEqual(journal.read_bytes(), before)
        for data in ({**op, 'event': 'request', 'summary': {}}, {**op, 'event': 'take', 'id': following}):
            with self.assertRaises(HybridError) as caught:
                self.bridge.network(data, 'fixture-worker-key')
            self.assertEqual(caught.exception.code, 'PREVIOUS_UPLOAD_UNCERTAIN')
        self.assertEqual(self.bridge.pending[following]['state'], 'approved')
        self.assertEqual(journal.read_bytes(), before)
        # Any additional unknown paid request retains the stronger generic guard.
        self.bridge._record({'id': 'fixture-paid', 'owner': 71, 'state': 'uncertain', 'summary': {'destination': '火山方舟'}})
        with self.assertRaises(HybridError) as caught:
            self.bridge.network({**op, 'event': 'request', 'summary': summary}, 'fixture-worker-key')
        self.assertEqual(caught.exception.code, 'PREVIOUS_REQUEST_UNCERTAIN_CHECK_PROVIDER')

    def test_upload_diagnosis_requires_exact_non_generating_request_metadata(self):
        known = {'state': 'uncertain', 'task_id': '', 'summary': {'destination': TEMP_DESTINATION,
                 'method': 'POST', 'endpoint': '/resources/internals/api.php', 'action': ''}}
        self.assertTrue(uncertain_temporary_upload(known))
        for field, value in (('destination', '火山方舟'), ('method', 'GET'),
                             ('endpoint', '/api/v3/contents/generations/tasks'), ('action', 'CreateAsset')):
            altered = copy.deepcopy(known); altered['summary'][field] = value
            self.assertFalse(uncertain_temporary_upload(altered), field)
            del altered['summary'][field]
            self.assertFalse(uncertain_temporary_upload(altered), 'missing ' + field)
        self.assertFalse(uncertain_temporary_upload({**known, 'task_id': 'fixture-generated-task'}))
        self.assertFalse(uncertain_temporary_upload({**known, 'state': 'sending'}))
        self.assertFalse(uncertain_temporary_upload({'state': 'uncertain'}))

    def test_expired_card_all_original_recovery_routes_remain_available(self):
        self.account['licensed'] = False
        self.login()
        with patch.object(self.bridge, '_worker', return_value={'key':'fixture'}) as worker:
            for path in RECOVERY_PATHS:
                self.bridge.operation(71,self.runtime.session_revision,'POST',path)
            self.assertEqual(worker.call_count,len(RECOVERY_PATHS))
            with self.assertRaises(HybridError):
                self.bridge.operation(71,self.runtime.session_revision,'POST','/api/wardrobe-swap/generate')

    def test_real_original_worker_local_split_and_account_isolation(self):
        import sys
        if sys.version_info < (3,10):
            self.skipTest('Original source requires Python 3.10+; run isolated Python 3.12 acceptance')
        server = make_server('127.0.0.1',self.port,self.app,threaded=True,request_handler=Quiet)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        self.login()
        source = self.root / 'synthetic.mp4'
        subprocess.run([str(resolve_ffmpeg()),'-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=24',
                        '-t','4','-c:v','libx264','-pix_fmt','yuv420p',str(source)],check=True)
        config = self.call('/api/config')
        self.assertEqual(config.status_code,200,config.data[:400])
        self.assertFalse(config.json['ark_ready'])
        worker = self.bridge.workers[71]
        self.assertEqual(requests.get(worker['url']+'/api/config',timeout=3).status_code,403)
        with source.open('rb') as stream:
            result = self.call('/api/long-video/analyze',method='POST',data={'reference_video':(stream,'synthetic.mp4'),'manual_cuts':'2'})
        self.assertEqual(result.status_code,202,result.data[:400])
        job = result.json
        deadline = time.monotonic()+25
        while time.monotonic()<deadline:
            result = self.call('/api/jobs/'+job['id'])
            job = result.json
            if job['status'] in {'succeeded','failed'}: break
            time.sleep(.2)
        self.assertEqual(job['status'],'succeeded',job.get('error'))
        self.assertEqual(len(job['shots']),2)
        media = self.call('/api/jobs/'+job['id']+'/shots/1/file/source')
        self.assertEqual(media.status_code,200,media.data[:150])
        artifact = self.root / 'shot.mp4'; artifact.write_bytes(media.data)
        subprocess.run([str(resolve_ffmpeg()),'-v','error','-i',str(artifact),'-f','null','-'],check=True)
        self.client.set_cookie('yzzh_session','fixture-local-session',domain='127.0.0.1')
        preview_url = '/_plugin/media/'+self.runtime.session_revision+'/api/plugin-remote/expired-fixture'
        expired_preview = self.client.get(preview_url, base_url=self.base)
        self.assertEqual(expired_preview.status_code, 410, expired_preview.data[:100])
        self.assertEqual(expired_preview.json['code'], 'PREVIEW_EXPIRED')
        self.assertEqual(expired_preview.headers['Cache-Control'], 'no-store')
        download_url = '/_plugin/media/'+self.runtime.session_revision+'/api/jobs/'+job['id']+'/shots/1/file/source?download=1'
        download = self.client.get(download_url,base_url=self.base)
        self.assertEqual(download.status_code,200,download.data[:100])
        self.assertEqual(download.data,media.data)
        self.assertFalse(self.bridge.pending)  # Real local FFmpeg path: zero cloud requests.
        old_headers = dict(self.headers)
        self.account['user_id']=72; self.login()
        self.assertEqual(self.call('/api/jobs/'+job['id']).status_code,404)
        stale = self.client.get('/api/jobs/'+job['id'],base_url=self.base,headers=old_headers)
        self.assertEqual(stale.status_code,409)
        self.assertEqual(self.client.get(download_url,base_url=self.base).status_code,409)
        self.assertTrue(list((self.root/'data'/'original'/'71'/'runs').glob('20*_web_long*')))


class GuardTests(unittest.TestCase):
    def test_uncertain_upload_callback_has_actionable_safe_message(self):
        guard = NetworkGuard({'callback': 'http://127.0.0.1/fixture-only', 'key': 'fixture-key'})
        response = Mock(status_code=409)
        response.json.return_value = {'error': 'PREVIOUS_UPLOAD_UNCERTAIN'}
        guard.control.post = Mock(return_value=response)
        token = CURRENT.set('fixture-operation')
        try:
            with self.assertRaises(RuntimeError) as caught:
                guard.callback('request', summary={})
        finally:
            CURRENT.reset(token)
        self.assertIn('临时素材上传的回执未确认', str(caught.exception))
        self.assertIn('确认面板', str(caught.exception))
        self.assertNotIn('fixture-key', str(caught.exception))
        self.assertNotIn('重复消费', str(caught.exception))

    def test_ambient_netrc_cannot_replace_this_accounts_key(self):
        with patch.object(requests.Session,'__init__',isolated_session_init(requests.Session.__init__)):
            with patch('requests.sessions.get_netrc_auth',return_value=('fixture-global','fixture-password')) as netrc:
                session=requests.Session()
                prepared=session.prepare_request(requests.Request('GET','https://ark.cn-beijing.volces.com/api/v3/tasks',headers={'Authorization':'Bearer fixture-account-key'}))
                self.assertEqual(prepared.headers['Authorization'],'Bearer fixture-account-key')
                netrc.assert_not_called()

    def guard(self, raw):
        return NetworkGuard({'callback':'http://127.0.0.1:1/unused', 'key':'fixture', 'values':{'TOS_BUCKET':'fixture-bucket'}},raw_send=raw)

    def test_freezes_bytes_waits_for_approval_and_sends_only_once(self):
        called=[]
        def raw(session, prepared, **kwargs):
            called.append(prepared.body.read())
            response=requests.Response(); response.status_code=200; response._content=b'{"id":"fixture-task"}'
            return response
        guard=self.guard(raw)
        events=[]
        def callback(event,**values):
            events.append((event,values)); self.assertFalse(called) if event in {'request','take'} else None
            if event=='request': return {'id':'fixture-network'}
            if event=='take': return {'state':'send_once'}
            return {}
        guard.callback=callback
        prepared=requests.Request('POST','https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks',json={'model':'fixture'}).prepare()
        original=prepared.body
        guard.send(requests.Session(),prepared)
        self.assertEqual(called,[original])
        self.assertEqual(events[0][1]['summary']['sha256'],hashlib.sha256(original).hexdigest())
        self.assertEqual(events[-1][1]['task_id'],'fixture-task')

    def test_denied_or_untrusted_request_never_sends(self):
        raw=Mock(); guard=self.guard(raw)
        guard.callback=Mock(side_effect=[{'id':'fixture'}, {'state':'rejected'}])
        with self.assertRaises(RuntimeError):
            guard.send(requests.Session(),requests.Request('POST','https://ark.cn-beijing.volces.com/api/v3/responses',json={}).prepare())
        for url in ('http://ark.cn-beijing.volces.com/x','https://evil.invalid/x','https://ark.cn-beijing.volces.com.evil.invalid/x'):
            with self.assertRaises(ValueError): destination(url,'fixture-bucket')
        with self.assertRaises(RuntimeError):
            guard.send(requests.Session(),requests.Request('PUT','https://other.tos-cn-beijing.volces.com/file',data=b'private').prepare())
        raw.assert_not_called()

    def test_lost_or_invalid_response_records_uncertain(self):
        raw=Mock(side_effect=requests.Timeout('fixture-timeout'))
        guard=self.guard(raw)
        guard.callback=Mock(side_effect=[{'id':'fixture'}, {'state':'send_once'}, {}])
        with self.assertRaises(requests.Timeout):
            guard.send(requests.Session(),requests.Request('POST','https://ark.cn-beijing.volces.com/api/v3/responses',json={}).prepare())
        self.assertFalse(guard.callback.call_args.kwargs['ok'])
        self.assertEqual(raw.call_count,1)
        self.assertNotIn('fixture-secret',redact('key=fixture-secret https://x.invalid/result?token=private', ['fixture-secret']))

    def test_malformed_success_stays_uncertain(self):
        for endpoint in ('contents/generations/tasks','images/generations','responses'):
            for body in (b'not-json',b'{}',b'{"data":[]}',b'{"data":[{}]}',b'{"output":[{}]}',b'{"choices":[{}]}'):
                response=requests.Response(); response.status_code=200; response._content=body
                raw=Mock(return_value=response); guard=self.guard(raw)
                guard.callback=Mock(side_effect=[{'id':'fixture'}, {'state':'send_once'}, {}])
                try:
                    guard.send(requests.Session(),requests.Request('POST','https://ark.cn-beijing.volces.com/api/v3/'+endpoint,json={}).prepare())
                except Exception:
                    pass
                self.assertFalse(guard.callback.call_args.kwargs['ok'], (endpoint,body))
                self.assertEqual(raw.call_count,1)

    def test_signed_character_thumbnail_is_private_validated_image_proxy(self):
        import cv2
        import numpy as np
        registry=RemoteImages('fixture-bucket')
        url='https://fixture-bucket.tos-cn-beijing.volces.com/image?signature=fixture-private'
        public=registry.register({'assets':[{'id':'fixture-asset','uri':'asset://fixture-asset','url':url}]})
        preview=public['assets'][0]['url']
        self.assertTrue(preview.startswith('/api/plugin-remote/'))
        self.assertNotIn('signature',json.dumps(public))
        _,image=cv2.imencode('.jpg',np.full((24,24,3),127,dtype=np.uint8))
        response=Mock(); response.status_code=200; response.iter_content.return_value=[image.tobytes()]
        with patch('requests.get') as get:
            get.return_value.__enter__.return_value=response
            data=registry.read(preview.rsplit('/',1)[1])
        self.assertEqual(get.call_args.args[0],url)
        self.assertEqual(cv2.imdecode(np.frombuffer(data,dtype=np.uint8),cv2.IMREAD_COLOR).shape,(24,24,3))
        self.assertEqual(registry.register({'uri':'asset://fixture','url':'https://untrusted.invalid/image'})['url'],'')

    def test_asset_mutation_unknown_response_does_not_unlock_retry(self):
        for action in ('CreateAsset','CreateAssetGroup','DeleteAsset','UpdateAssetGroup'):
            for body in (b'not-json',b'{}',b'{"Result":{}}'):
                response=requests.Response(); response.status_code=200; response._content=body
                guard=self.guard(Mock(return_value=response))
                guard.callback=Mock(side_effect=[{'id':'fixture'}, {'state':'send_once'}, {}])
                try:
                    guard.send(requests.Session(), requests.Request('POST','https://open.volcengineapi.com/?Action='+action,json={}).prepare())
                except ValueError:
                    pass
                self.assertFalse(guard.callback.call_args.kwargs['ok'],(action,body))


class RemoteImageErrorsTests(unittest.TestCase):
    def setUp(self):
        self.registry = RemoteImages('fixture-bucket')
        self.url = 'https://fixture-bucket.tos-cn-beijing.volces.com/image?signature=must-stay-private'
        public = self.registry.register({'uri': 'asset://fixture', 'url': self.url})
        self.key = public['url'].rsplit('/', 1)[1]

    def assert_public_error(self, callback, code, status):
        with self.assertRaises(RemoteImageError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.status, status)
        public = caught.exception.public()
        self.assertEqual(public['action'], 'refresh_character_library')
        self.assertNotIn('must-stay-private', json.dumps(public))
        self.assertNotIn('https://', json.dumps(public))

    def test_unknown_preview_key_is_expired_not_missing_resource(self):
        with patch('requests.get') as get:
            self.assert_public_error(lambda: self.registry.read('expired'), 'PREVIEW_EXPIRED', 410)
        get.assert_not_called()

    def test_network_failure_classification_never_returns_exception_text(self):
        for failure, code, status in (
            (requests.Timeout(self.url), 'PREVIEW_TIMEOUT', 504),
            (requests.ConnectionError(self.url), 'PREVIEW_NETWORK_ERROR', 502),
        ):
            with self.subTest(code=code), patch('requests.get', side_effect=failure):
                self.assert_public_error(lambda: self.registry.read(self.key), code, status)

    def test_upstream_failure_is_not_reported_as_a_local_404(self):
        for status in (301, 403, 404, 429, 500):
            response = Mock(status_code=status)
            with self.subTest(status=status), patch('requests.get') as get:
                get.return_value.__enter__.return_value = response
                self.assert_public_error(lambda: self.registry.read(self.key), 'PREVIEW_UPSTREAM_ERROR', 502)
                self.assertFalse(get.call_args.kwargs['allow_redirects'])
                response.iter_content.assert_not_called()

    def test_oversized_and_invalid_images_have_safe_errors(self):
        for body, code, status in (
            (b'x' * (20 * 1024 * 1024 + 1), 'PREVIEW_TOO_LARGE', 413),
            (b'not-an-image must-stay-private', 'PREVIEW_DECODE_ERROR', 422),
            (b'', 'PREVIEW_DECODE_ERROR', 422),
        ):
            response = Mock(status_code=200)
            response.iter_content.return_value = [body]
            with self.subTest(code=code, size=len(body)), patch('requests.get') as get:
                get.return_value.__enter__.return_value = response
                self.assert_public_error(lambda: self.registry.read(self.key), code, status)


if __name__=='__main__': unittest.main()
