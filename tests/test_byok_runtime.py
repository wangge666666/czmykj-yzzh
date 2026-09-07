"""BYOK tests use synthetic media and fixture credentials, never paid services."""
import copy
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np
from flask import Flask, jsonify, request
from werkzeug.serving import make_server, WSGIRequestHandler

from hybrid_shared import HybridError, file_hash
from workflow_core import resolve_ffmpeg, inspect_video
from yzzh_local.account import AccountClient
from yzzh_local.app import create_app
from yzzh_local.runtime import Runtime
from yzzh_local.settings import Settings, FIELDS
from yzzh_local.provider import ArkProvider
from yzzh_local.mcp import Protocol, TOOLS

VALUES = {"ARK_API_KEY":"fixture-ark-not-real", "ARK_MODEL":"fixture-model", "TOS_ACCESS_KEY":"fixture-ak",
          "TOS_SECRET_KEY":"fixture-sk", "TOS_BUCKET":"fixture-bucket"}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video, self.image = self.root / 'synthetic.mp4', self.root / 'synthetic.png'
        subprocess.run([str(resolve_ffmpeg()), '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=96x64:rate=10',
                        '-t', '4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(self.video)], check=True)
        cv2.imwrite(str(self.image), np.full((24,24,3), 180, dtype=np.uint8))
        self.auth = Mock(login_url='https://accounts.example.test/api/auth/login')
        self.auth.login.return_value = 'fixture-login-token'
        self.account = {'user_id':7, 'username':'fixture-customer', 'licensed':True, 'product_id':4,
                        'subscription_end':'2030-01-01', 'license_status':'active'}
        self.auth.verify.side_effect = lambda token: copy.deepcopy(self.account)
        self.provider = Mock()
        def submit(manifest, files, event):
            event('UPLOAD_PLANNED', {'bucket':'fixture-bucket', 'key':'yzzh-plugin/fixture.mp4'})
            event('PROVIDER_POST_STARTED', {})
            return 'fixture-provider-task'
        self.provider.submit.side_effect = submit
        self.provider.query.return_value = {'id':'fixture-provider-task', 'status':'succeeded',
                                           'content':{'video_url':'https://private.invalid/?signed=fixture'}}
        self.provider.download.side_effect = lambda task, path: shutil.copyfile(self.video, path)
        self.factory = Mock(return_value=self.provider)
        self.runtime = Runtime(self.root / 'data', account_client=self.auth, provider_factory=self.factory)
        self.addCleanup(lambda: self.runtime.pool.shutdown(wait=True))
        self.runtime.login('fixture','fixture-password')
        self.save_settings(VALUES)

    def save_settings(self, values):
        return self.runtime.save_settings(values, self.runtime.account['user_id'], self.runtime.session_revision)

    def plan(self, approve=True):
        project = self.runtime.import_video(str(self.video))
        key, artifact = project['id'], next(iter(project['artifacts']))
        result = self.runtime.plan(key, artifact, [str(self.image)], '合成素材测试', '')
        if approve: self.runtime.approve(key, result['plan']['hash'], True)
        return key, artifact

    def submit(self, key):
        self.runtime.submit(key)
        self.runtime.pool.submit(lambda: None).result(timeout=10)

    def test_end_to_end_direct_provider_output_decodes_no_central_gateway(self):
        key, _ = self.plan()
        self.submit(key)
        result = self.runtime.poll(key)
        self.assertEqual(result['state'], 'succeeded')
        artifact = next(k for k,v in result['artifacts'].items() if v['kind']=='output')
        exported = self.runtime.export(key, artifact, str(self.root / 'export.mp4'))
        self.assertEqual(file_hash(exported['path']), file_hash(self.video))
        subprocess.run([str(resolve_ffmpeg()), '-v', 'error', '-i', exported['path'], '-f', 'null', '-'], check=True)
        self.runtime.poll(key)
        self.assertEqual(self.provider.submit.call_count, 1)
        self.assertEqual(self.provider.download.call_count, 1)
        self.assertFalse(hasattr(self.runtime, 'cloud'))
        self.assertEqual(result['plan']['cost']['payer'], 'customer_provider_account')
        for secret in ('fixture-ark-not-real', 'fixture-login-token', 'signed=fixture'):
            self.assertNotIn(secret, json.dumps(result))
            self.assertNotIn(secret, self.runtime.store.path.read_bytes().decode(errors='ignore'))

    def test_no_approval_and_rejected_approval_prevent_submit(self):
        key, _ = self.plan(False)
        with self.assertRaises(HybridError): self.runtime.submit(key)
        plan_hash = self.runtime.status(key)['plan']['hash']
        self.runtime.approve(key, plan_hash, False)
        with self.assertRaises(HybridError): self.runtime.submit(key)
        self.provider.submit.assert_not_called()

    def test_changed_key_file_or_login_invalidates_approval(self):
        for change in ('key','file','login'):
            with self.subTest(change=change):
                key, artifact = self.plan()
                if change=='key': self.save_settings({'ARK_API_KEY':'fixture-new-key'})
                elif change=='file':
                    path = self.runtime.artifact_path(key, artifact)
                    path.write_bytes(path.read_bytes() + b'fixture-change')
                else: self.runtime.login('fixture', 'fixture-password')
                with self.assertRaises(HybridError): self.runtime.submit(key)
        self.provider.submit.assert_not_called()

    def test_expiry_stops_new_work_but_allows_accepted_task_recovery(self):
        key, artifact = self.plan()
        self.submit(key)
        self.account.update(licensed=False, license_status='inactive')
        for action in (lambda:self.runtime.import_video(str(self.video)),
                       lambda:self.runtime.process(key, artifact, 'split')):
            with self.assertRaises(HybridError): action()
        self.assertEqual(self.runtime.poll(key)['state'], 'succeeded')
        self.assertEqual(self.provider.submit.call_count, 1)

    def test_license_outage_no_new_tasks_preserves_existing_data(self):
        key, _ = self.plan()
        self.auth.verify.side_effect = HybridError('LICENSE_SERVICE_UNAVAILABLE',503)
        with self.assertRaises(HybridError): self.runtime.submit(key)
        self.assertEqual(self.runtime.status(key)['state'], 'approved')
        self.assertEqual(self.runtime.account['license_status'], 'unavailable')
        self.provider.submit.assert_not_called()

    def test_submit_timeout_is_persistent_and_cannot_replan_or_resubmit(self):
        key, artifact = self.plan()
        def timeout(manifest, files, event):
            event('PROVIDER_POST_STARTED', {})
            raise RuntimeError('fixture raw key should not escape')
        self.provider.submit.side_effect = timeout
        self.submit(key)
        self.assertEqual(self.runtime.status(key)['state'], 'submit_uncertain')
        for action in (lambda:self.runtime.submit(key), lambda:self.runtime.poll(key),
                       lambda:self.runtime.plan(key,artifact,[str(self.image)],'again','')):
            with self.assertRaises(HybridError): action()
        self.assertEqual(self.provider.submit.call_count,1)
        self.assertNotIn('raw key',json.dumps(self.runtime.status(key)))

    def test_upload_failure_is_not_false_paid_submission(self):
        key, _ = self.plan()
        self.provider.submit.side_effect = RuntimeError('fixture pre-POST failure')
        self.submit(key)
        self.assertEqual(self.runtime.status(key)['state'], 'needs_replan')

    def test_download_failure_keeps_task_and_recovers_without_new_post(self):
        key, _ = self.plan()
        self.submit(key)
        self.provider.download.side_effect = RuntimeError('fixture signed-url')
        self.assertEqual(self.runtime.poll(key)['state'], 'download_pending')
        with self.assertRaises(HybridError): self.runtime.plan(key,'unused',[],'new','')
        self.provider.download.side_effect = lambda task,path:shutil.copyfile(self.video,path)
        self.assertEqual(self.runtime.poll(key)['state'], 'succeeded')
        self.assertEqual(self.provider.submit.call_count,1)

    def test_restart_requires_login_preserves_task_id_and_allows_recovery(self):
        key, _ = self.plan(); self.submit(key)
        restored = Runtime(self.root/'data', account_client=self.auth, provider_factory=self.factory)
        self.addCleanup(lambda:restored.pool.shutdown(wait=True))
        with self.assertRaises(HybridError): restored.status(key)
        restored.login('fixture','fixture-password')
        self.account['licensed'] = False
        self.assertEqual(restored.poll(key)['state'],'succeeded')
        self.assertEqual(self.provider.submit.call_count,1)

    def test_settings_are_account_scoped_private_and_never_returned_to_mcp(self):
        key, _ = self.plan()
        public = self.runtime.provider_settings()
        self.assertEqual(self.runtime.settings.path(7).stat().st_mode & 0o777, 0o600)
        for secret in ('fixture-ark-not-real','fixture-ak','fixture-sk'):
            self.assertNotIn(secret,json.dumps(public))
        self.account['user_id'] = 8
        self.runtime.login('second','fixture-password')
        self.assertFalse(self.runtime.provider_settings()['configured'])
        self.assertEqual(self.runtime.list_projects(),[])
        with self.assertRaises(HybridError):self.runtime.poll(key)
        names = {n for n,_,_ in TOOLS}
        self.assertTrue({'login','approve','save_settings','settings'}.isdisjoint(names))

    def test_queued_worker_rechecks_account_settings_and_time_card(self):
        for change in ('account','settings','expiry'):
            with self.subTest(change=change):
                self.account.update(user_id=7,licensed=True)
                self.runtime.login('fixture','fixture-password')
                key,_=self.plan()
                with patch.object(self.runtime.pool,'submit'):
                    self.runtime.submit(key)
                if change=='account':
                    self.account['user_id']=8; self.runtime.login('second','fixture-password')
                elif change=='settings':self.save_settings({'ARK_API_KEY':'fixture-rotated'})
                else:self.account['licensed']=False
                self.runtime._submit_worker(key)
                self.assertEqual(self.runtime.store.get(key)['state'],'needs_replan')
        self.provider.submit.assert_not_called()

    def test_license_rechecked_between_upload_and_paid_post(self):
        key,_=self.plan()
        def upload(manifest, files, event):
            event('UPLOAD_PLANNED',{'bucket':'fixture','key':'fixture'})
            self.account['licensed']=False
            event('PROVIDER_POST_STARTED',{})
            self.fail('must not reach paid POST')
        self.provider.submit.side_effect=upload
        self.submit(key)
        record=self.runtime.store.get(key)
        self.assertEqual(record['state'],'needs_replan')
        self.assertFalse(record['provider']['post_started'])

    def test_real_split_and_restore_artifacts(self):
        project=self.runtime.import_video(str(self.video))
        self.runtime.process(project['id'],next(iter(project['artifacts'])),'split')
        self.runtime.pool.submit(lambda:None).result(timeout=20)
        state=self.runtime.status(project['id'])
        self.assertEqual(state['state'],'ready')
        output=next(k for k,v in state['artifacts'].items() if v['kind']=='split')
        self.assertGreater(inspect_video(self.runtime.artifact_path(project['id'],output)).duration,0)

    def test_settings_ui_security_and_no_credentials_echo(self):
        client=create_app(self.runtime,'fixture-session',7871).test_client()
        base='http://127.0.0.1:7871'
        headers={'X-Yzzh-Session':'fixture-session','X-Yzzh-Request':'1'}
        self.assertEqual(client.post('/api/settings',base_url=base,json={'values':VALUES}).status_code,401)
        self.assertEqual(client.post('/api/settings',base_url=base,headers={**headers,'Origin':'https://evil.test'},json={'values':VALUES}).status_code,403)
        context={'expected_owner':7,'expected_session':self.runtime.session_revision}
        saved=client.post('/api/settings',base_url=base,headers=headers,json={**context,'values':VALUES})
        self.assertEqual(saved.status_code,200)
        state=client.get('/api/state',base_url=base,headers=headers)
        self.assertEqual(state.status_code,200)
        self.assertNotIn(VALUES['ARK_API_KEY'], state.get_data(as_text=True))
        self.assertNotIn(VALUES['ARK_API_KEY'], saved.get_data(as_text=True))
        self.assertEqual(client.post('/api/call',base_url=base,headers=headers,json={'name':'save_settings','arguments':{'changes':VALUES}}).status_code,400)

    def test_stale_settings_form_cannot_save_or_clear_another_account(self):
        stale_session=self.runtime.session_revision
        self.account['user_id']=8
        self.runtime.login('second','fixture-password')
        self.save_settings({**VALUES,'ARK_API_KEY':'fixture-B-key'})
        for clear in (False,True):
            with self.assertRaises(HybridError) as error:
                self.runtime.save_settings(VALUES,7,stale_session,clear=clear)
            self.assertEqual(error.exception.code,'SETTINGS_ACCOUNT_CHANGED_REFRESH')
            self.assertEqual(self.runtime.settings.load(8)[0]['ARK_API_KEY'],'fixture-B-key')


class AccountAndSettingsTests(unittest.TestCase):
    def test_provider_upload_payload_and_durable_post_boundary(self):
        events=[]
        sdk=Mock()
        sdk.TosClientV2.return_value.pre_signed_url.return_value.signed_url='https://fixture.tos-cn-beijing.volces.com/asset?signature=fixture'
        provider=ArkProvider(VALUES)
        manifest={'model':'fixture-model','resolution':'720p','ratio':'9:16','duration':5,'prompt':'fixture',
                  'assets':[{'slot':'asset_0','kind':'video'},{'slot':'asset_1','kind':'image'}]}
        def post(method,path,payload):
            self.assertEqual(events[-1],'PROVIDER_POST_STARTED')
            self.assertEqual(method,'POST')
            self.assertEqual(path,'/contents/generations/tasks')
            self.assertEqual([x['role'] for x in payload['content'][1:]],['reference_video','reference_image'])
            self.assertEqual(payload['model'],'fixture-model')
            return {'id':'fixture-task'}
        with patch.dict('sys.modules',{'tos':sdk}), patch.object(provider,'preflight'), patch.object(provider,'_request',side_effect=post) as send:
            result=provider.submit(manifest,{'asset_0':'/fixture/reference.mp4','asset_1':'/fixture/image.png'},lambda code,data:events.append(code))
            self.assertEqual(result,'fixture-task')
            self.assertEqual(events,['UPLOAD_PLANNED','UPLOAD_PLANNED','PROVIDER_POST_STARTED'])
            self.assertEqual(send.call_count,1)
            self.assertEqual(sdk.TosClientV2.return_value.put_object_from_file.call_count,2)

    def test_provider_preflight_rejects_invalid_video_and_model_before_network(self):
        provider=ArkProvider(VALUES)
        manifest={'model':'wrong-model','assets':[]}
        with self.assertRaises(HybridError):provider.preflight(manifest,{})
        manifest={'model':'fixture-model','assets':[{'slot':'asset_0','kind':'video'}]}
        with patch('workflow_core.validate_seedance_reference_video',side_effect=ValueError('fixture-invalid-fps')):
            with self.assertRaises(HybridError) as error:provider.preflight(manifest,{'asset_0':'/fixture.mp4'})
            self.assertEqual(error.exception.code,'REFERENCE_VIDEO_FORMAT_UNSUPPORTED')

    def test_server_entitlement_is_authoritative_zero_balance_and_stacked_card(self):
        client=AccountClient()
        data={'valid':True,'userId':7,'username':'fixture','balance':0,'subscriptions':[
            {'productId':4,'status':'active','startTime':'2099-01-01','endTime':'2100-01-01'}]}
        with patch.object(client,'_post',return_value=data):
            self.assertTrue(client.verify('fixture')['licensed'])
            data['subscriptions'][0]['productId']=5
            self.assertFalse(client.verify('fixture')['licensed'])
            data['valid']='true'
            with self.assertRaises(HybridError):client.verify('fixture')

    def test_settings_external_edit_rotation_and_invalid_fields(self):
        with tempfile.TemporaryDirectory() as root:
            settings=Settings(root)
            settings.save(7,VALUES); _,first=settings.load(7)
            path=settings.path(7)
            path.write_text(path.read_text().replace('fixture-ark-not-real','fixture-edited'))
            _,second=settings.load(7)
            self.assertNotEqual(first,second)
            settings.clear(7)
            self.assertFalse(settings.public(7)['configured'])
            for changes in ({'AUTH_TOKEN_SECRET':'never-accepted'}, {'ARK_API_KEY':'x\nTOS_BUCKET=bad'}, {'TOS_BUCKET':'https://evil.test'}):
                with self.assertRaises(HybridError):settings.save(7,changes)

    def test_real_http_login_and_verify_contract_no_other_routes(self):
        app=Flask('fixture-account'); calls=[]
        @app.post('/api/auth/login')
        def login():
            calls.append(request.path)
            assert request.json=={'username':'fixture','password':'fixture-password','role':'customer'}
            return jsonify(success=True,token='fixture-token')
        @app.post('/api/auth/verify-token')
        def verify():
            calls.append(request.path)
            assert request.json=={'token':'fixture-token'}
            return jsonify(valid=True,userId=7,username='fixture',balance=0,subscriptions=[{'productId':4,'status':'active','endTime':'2100-01-01'}])
        class Quiet(WSGIRequestHandler):
            def log(self,*args,**kwargs):pass
        server=make_server('127.0.0.1',0,app,request_handler=Quiet)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            client=AccountClient(f'http://127.0.0.1:{server.server_port}/api/auth/login',development=True)
            self.assertTrue(client.verify(client.login('fixture','fixture-password'))['licensed'])
            self.assertEqual(calls,['/api/auth/login','/api/auth/verify-token'])
        finally:server.shutdown();thread.join(3);server.server_close()

    def test_provider_output_hosts_and_raw_errors_are_rejected(self):
        provider=ArkProvider(VALUES)
        with patch('yzzh_local.provider.requests.get') as get:
            for url in ('http://127.0.0.1/a','https://evil.test/a','https://tos-cn-beijing.volces.com.evil.test/a','https://user:pass@x.tos-cn-beijing.volces.com/a'):
                with self.assertRaises(HybridError):provider.download({'content':{'video_url':url}},'/unused')
            get.assert_not_called()
        import requests
        with patch('yzzh_local.provider.requests.request',side_effect=requests.Timeout('raw-fixture-key')) as send:
            with self.assertRaises(HybridError) as error:provider._request('POST','/contents/generations/tasks',{})
            self.assertNotIn('raw-fixture-key',str(error.exception))
            self.assertEqual(send.call_count,1)


if __name__=='__main__':unittest.main()
