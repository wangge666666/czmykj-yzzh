from __future__ import annotations
import copy
import json
import subprocess
import tempfile
import time
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from hybrid_shared import HybridError, digest, file_hash
from workflow_core import inspect_video, resolve_ffmpeg
from yzzh_local.app import create_app
from yzzh_local.mcp import Protocol
from yzzh_cloud.legacy_client import Runtime  # historical gateway regression only


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / 'fixture.mp4'
        subprocess.run([str(resolve_ffmpeg()), '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=96x64:rate=10', '-t', '4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(self.video)], check=True)
        self.runtime = Runtime(self.root / 'data')
        self.addCleanup(lambda: self.runtime.pool.shutdown(wait=True))

    def project(self):
        result = self.runtime.import_video(str(self.video))
        return result['id'], next(iter(result['artifacts']))

    def test_real_video_import_split_decode_and_restore(self):
        key, artifact = self.project()
        self.runtime.process(key, artifact, 'split')
        self.runtime.pool.shutdown(wait=True)
        state = self.runtime.status(key)
        self.assertEqual(state['state'], 'ready')
        outputs = [k for k,v in state['artifacts'].items() if v['kind'] == 'split']
        self.assertGreater(len(outputs), 0)
        for output in outputs:
            path = self.runtime.artifact_path(key, output)
            self.assertGreater(inspect_video(path).duration, 0)
            subprocess.run([str(resolve_ffmpeg()), '-v', 'error', '-i', str(path), '-f', 'null', '-'], check=True)
        restored = Runtime(self.root / 'data')
        self.addCleanup(lambda: restored.pool.shutdown(wait=True))
        self.assertEqual(restored.status(key)['artifacts'], state['artifacts'])

    def test_artifact_traversal_and_existing_export_rejected(self):
        key, artifact = self.project()
        with self.assertRaises(HybridError):
            self.runtime.artifact_path('../', artifact)
        with self.assertRaises(FileExistsError):
            self.runtime.export(key, artifact, str(self.video))

    def test_export_copies_without_overwriting(self):
        key, artifact = self.project()
        target = self.root / 'export.mp4'
        result = self.runtime.export(key, artifact, str(target))
        self.assertEqual(result['sha256'], file_hash(self.video))
        self.assertTrue(inspect_video(target).duration > 0)

    def test_restart_marks_local_interruption_not_done(self):
        key, _ = self.project()
        record = self.runtime.store.get(key); record['state'] = 'processing'; self.runtime.store.put(record, 'FIXTURE')
        restored = Runtime(self.root / 'data'); self.addCleanup(lambda: restored.pool.shutdown(wait=True))
        self.assertEqual(restored.status(key)['state'], 'interrupted')

    def test_client_never_submits_without_approval(self):
        key, _ = self.project()
        with patch.object(self.runtime, 'cloud') as cloud:
            with self.assertRaises(HybridError):
                self.runtime.submit(key)
            cloud.assert_not_called()

    def test_stale_and_rejected_approval_block(self):
        key, _ = self.project()
        record = self.runtime.store.get(key)
        record.update(state='awaiting_approval', plan={'hash':'current', 'approved':False})
        self.runtime.store.put(record, 'FIXTURE')
        with self.assertRaises(HybridError):
            self.runtime.approve(key, 'old', True)
        self.runtime.approve(key, 'current', False)
        with self.assertRaises(HybridError):
            self.runtime.submit(key)

    def test_file_change_after_approval_prevents_upload(self):
        key, _ = self.project()
        record = self.runtime.store.get(key)
        record.update(state='approved', plan={'approved':True, 'files':[str(self.video)],
                     'manifest':{'assets':[{'sha256':'0'*64}]}})
        self.runtime.store.put(record, 'FIXTURE')
        with patch.object(self.runtime, 'cloud') as cloud:
            with self.assertRaises(HybridError):
                self.runtime.submit(key)
            cloud.assert_not_called()

    def test_account_switch_blocks_foreign_project(self):
        self.runtime.account = {'user_id':7}
        key, _ = self.project()
        self.runtime.account = {'user_id':8}
        with self.assertRaises(HybridError):
            self.runtime.status(key)
        self.assertEqual(self.runtime.list_projects(), [])

    def test_import_keeps_owner_when_login_races_media_copy(self):
        import shutil
        self.runtime.account = {'user_id':7}
        entered, release, switched = threading.Event(), threading.Event(), threading.Event()
        original = shutil.copyfile
        result, errors = [], []
        def copy(*args, **kwargs):
            entered.set(); release.wait(3)
            return original(*args, **kwargs)
        def importer():
            try: result.append(self.runtime.import_video(str(self.video)))
            except Exception as error: errors.append(error)
        def login(*args):
            self.runtime.account = {'user_id':8}; switched.set()
        with patch('yzzh_cloud.legacy_client.shutil.copyfile', side_effect=copy), patch.object(self.runtime, '_login', side_effect=login):
            first = threading.Thread(target=importer); first.start(); self.assertTrue(entered.wait(2))
            second = threading.Thread(target=lambda: self.runtime.login('B','test')); second.start()
            self.assertFalse(switched.wait(.05)); release.set(); first.join(3); second.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(len(result), 1)
        self.assertEqual(self.runtime.store.get(result[0]['id'])['owner'], 7)
        self.assertEqual(self.runtime.list_projects(), [])
        with self.assertRaises(HybridError): self.runtime.status(result[0]['id'])

    def test_reconciliation_cannot_be_overwritten_or_processed(self):
        key, artifact = self.project()
        record = self.runtime.store.get(key)
        record.update(state='reconciliation_required', plan={'quote':{'id':'old-cloud-task'}})
        self.runtime.store.put(record, 'FIXTURE')
        for callback in [lambda: self.runtime.process(key, artifact, 'split'),
                         lambda: self.runtime.plan(key, artifact, [], 'prompt', 'model')]:
            with self.assertRaises(HybridError) as caught:
                callback()
            self.assertEqual(caught.exception.code, 'PROJECT_BUSY')
        self.assertEqual(self.runtime.store.get(key)['plan']['quote']['id'], 'old-cloud-task')
        restored = Runtime(self.root / 'data'); self.addCleanup(lambda: restored.pool.shutdown(wait=True))
        self.assertEqual(restored.store.get(key)['plan']['quote']['id'], 'old-cloud-task')

    def test_quote_rejection_is_replan_not_unknown_submission(self):
        self.runtime.account = {'user_id':7}
        key, _ = self.project()
        record = self.runtime.store.get(key)
        record.update(state='submitting', plan={'files':[], 'manifest':{'assets':[]}, 'quote':{'id':'a'*32}})
        self.runtime.store.put(record, 'FIXTURE')
        with patch.object(self.runtime, 'cloud', side_effect=HybridError('CLOUD_SUBMIT_REJECTED', 409)):
            self.runtime._submit_worker(key)
        self.assertEqual(self.runtime.status(key)['state'], 'needs_replan')

    def test_queued_submit_after_account_switch_never_uploads(self):
        self.runtime.account = {'user_id':7}
        key, _ = self.project()
        record = self.runtime.store.get(key)
        record.update(state='submitting', plan={'files':[], 'quote':{'id':'a'*32}})
        self.runtime.store.put(record, 'FIXTURE')
        self.runtime.account = {'user_id':8}
        with patch.object(self.runtime, 'cloud') as cloud:
            self.runtime._submit_worker(key)
            cloud.assert_not_called()
        record = self.runtime.store.get(key)
        self.assertEqual(record['state'], 'needs_replan')
        self.assertEqual(record['owner'], 7)

    def test_uncertain_quoted_task_remains_explicit_not_fake_running(self):
        key, _ = self.project()
        record = self.runtime.store.get(key)
        record.update(state='submit_uncertain', plan={'quote':{'id':'a'*32}})
        self.runtime.store.put(record, 'FIXTURE')
        with patch.object(self.runtime, 'cloud', return_value=Mock(json=lambda: {'state':'quoted'})):
            self.assertEqual(self.runtime.poll(key)['state'], 'submit_uncertain')

    def test_login_cannot_change_owner_while_plan_is_quoting(self):
        import cv2
        import numpy as np
        image = self.root / 'private-reference.png'
        cv2.imwrite(str(image), np.zeros((20,20,3), dtype=np.uint8))
        self.runtime.account = {'user_id':7}
        key, artifact = self.project()
        entered, release, switched = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def quote(*args, **kwargs):
            entered.set(); release.wait(3)
            return Mock(json=lambda: {'id':'a'*32, 'quote':{'unit_price':'0.1'}})
        def plan():
            try: self.runtime.plan(key, artifact, [str(image)], 'prompt', 'model')
            except Exception as error: errors.append(error)
        def login(*args):
            self.runtime.account = {'user_id':8}; switched.set()
        with patch.object(self.runtime, 'cloud', side_effect=quote), patch.object(self.runtime, '_login', side_effect=login):
            first = threading.Thread(target=plan); first.start(); self.assertTrue(entered.wait(2))
            second = threading.Thread(target=lambda: self.runtime.login('B','test')); second.start()
            self.assertFalse(switched.wait(.05)); release.set(); first.join(3); second.join(3)
        self.assertEqual(errors, [])
        record = self.runtime.store.get(key)
        self.assertEqual(record['owner'], 7)
        with self.assertRaises(HybridError): self.runtime.artifact_path(key, artifact)
        self.runtime.account = {'user_id':7}
        refs = self.runtime.public(record)['plan']['references']
        self.assertEqual(refs[1]['name'], image.name)
        self.assertEqual(file_hash(self.runtime.artifact_path(key, refs[1]['artifact_id'])), file_hash(image))

    def test_cloud_endpoint_does_not_allow_insecure_external_hosts(self):
        for endpoint in ['http://example.com', 'https://user:pass@example.com', 'https://example.com?secret=a']:
            with self.assertRaises(HybridError):
                Runtime(self.root / 'reject', endpoint)

    def test_loopback_ui_blocks_cross_origin_missing_auth_and_approval_tool(self):
        client = create_app(self.runtime, 'fixture-session', 7871).test_client()
        base = 'http://127.0.0.1:7871'
        headers = {'X-Yzzh-Session':'fixture-session', 'X-Yzzh-Request':'1'}
        self.assertEqual(client.get('/api/state', base_url=base).status_code, 401)
        self.assertEqual(client.get('/api/state', base_url='http://evil.test:7871', headers=headers).status_code, 403)
        self.assertEqual(client.get('/api/state', base_url=base, headers={**headers, 'Origin':'https://evil.test'}).status_code, 403)
        self.assertEqual(client.post('/api/call', base_url=base, headers=headers, json={'name':'approve'}).status_code, 400)
        response = client.get('/api/state', base_url=base, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])


class ProtocolTests(unittest.TestCase):
    def test_initialize_list_and_call_without_private_tokens(self):
        call = Mock(return_value={'ready':True})
        protocol = Protocol(call)
        result = protocol.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}})
        self.assertEqual(result['result']['protocolVersion'], '2025-06-18')
        self.assertIsNone(protocol.handle({'jsonrpc':'2.0','method':'notifications/initialized'}))
        result = protocol.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})
        names = [x['name'] for x in result['result']['tools']]
        self.assertIn('process', names)
        self.assertNotIn('approve', names)
        self.assertNotIn('login', names)
        result = protocol.handle({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'health','arguments':{}}})
        self.assertFalse(result['result']['isError'])

    def test_protocol_rejects_unknown_tool_and_extra_arguments(self):
        protocol = Protocol(Mock())
        protocol.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{}})
        for name, args in [('approve', {}), ('health', {'token':'do-not-accept'})]:
            result = protocol.handle({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':name,'arguments':args}})
            self.assertEqual(result['error']['code'], -32602)
        protocol.call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
