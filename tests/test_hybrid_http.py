"""Real loopback HTTP/multipart test; identity, provider and charges are fixtures."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import cv2
import numpy as np
from werkzeug.serving import WSGIRequestHandler, make_server

from hybrid_shared import file_hash
from workflow_core import inspect_video, resolve_ffmpeg
from yzzh_cloud.account import BillingQuote, MiyoIntegrationError, VerifiedAccount
from yzzh_cloud.app import create_app as cloud_app
from yzzh_local.app import create_app as local_app
from yzzh_cloud.legacy_client import Runtime  # historical gateway regression only


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


class HttpWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / 'synthetic.mp4'
        self.image = self.root / 'synthetic.png'
        subprocess.run([str(resolve_ffmpeg()), '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=96x64:rate=10', '-t', '4', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(self.video)], check=True)
        cv2.imwrite(str(self.image), np.full((24, 24, 3), 180, dtype=np.uint8))
        self.accounts = Mock()
        self.accounts.verify_token.return_value = VerifiedAccount(
            7, 9, 1800000000000, 'http-fixture', 'customer', Decimal('10'), 4,
            'miyo_fashion', 'monthly', '2030-01-01', 30)
        self.accounts.quote_paid_stage.return_value = BillingQuote('seedance-video', 'tokens', Decimal('0.042'))
        self.accounts.charge_once.return_value.public.return_value = {'charged': True, 'cost': 0.084}
        self.provider = Mock()
        self.provider.submit.return_value = 'fixture-provider-id'
        self.provider.query.return_value = {'status': 'succeeded', 'usage': {'total_tokens':2000}}
        self.provider.download.side_effect = lambda task, path: shutil.copyfile(self.video, path)
        self.now = 1000
        self.server = make_server('127.0.0.1', 0, cloud_app(self.root / 'server', accounts=self.accounts,
            provider=self.provider, models=['fixture-seedance'], now=lambda: self.now), request_handler=QuietHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.stop_server)
        self.runtime = Runtime(self.root / 'client', f'http://127.0.0.1:{self.server.server_port}', development=True)
        self.runtime.token = 'isolated-fixture-not-real-token'
        self.runtime.account = self.runtime.cloud('GET', '/v1/account').json()
        self.addCleanup(lambda: self.runtime.pool.shutdown(wait=True))

    def stop_server(self):
        self.server.shutdown()
        self.server_thread.join(3)
        self.server.server_close()

    def plan_and_fixture_approve(self):
        project = self.runtime.import_video(str(self.video))
        key, artifact = project['id'], next(iter(project['artifacts']))
        plan = self.runtime.plan(key, artifact, [str(self.image)], '合成媒体端到端测试，不调用供应商', 'fixture-seedance')
        # Automated fixture approval is NOT real user approval or paid acceptance.
        client = local_app(self.runtime, 'fixture-session', 7871).test_client()
        response = client.post('/api/approve', base_url='http://127.0.0.1:7871',
            headers={'X-Yzzh-Session':'fixture-session', 'X-Yzzh-Request':'1'},
            json={'project_id':key, 'plan_hash':plan['plan']['hash'], 'approved':True})
        self.assertEqual(response.status_code, 200)
        return key

    def test_http_multipart_generation_reconciliation_download_and_export(self):
        key = self.plan_and_fixture_approve()
        self.runtime.submit(key)
        self.runtime.pool.submit(lambda: None).result(timeout=10)
        self.assertEqual(self.runtime.status(key)['state'], 'cloud_running')
        sent_manifest, sent_files = self.provider.submit.call_args.args
        for asset in sent_manifest['assets']:
            self.assertEqual(file_hash(sent_files[asset['slot']]), asset['sha256'])
        self.accounts.charge_once.side_effect = MiyoIntegrationError('fixture failure', code='BILLING_FAIL', status_code=503)
        self.assertEqual(self.runtime.poll(key)['state'], 'reconciliation_required')
        self.accounts.charge_once.side_effect = None
        result = self.runtime.poll(key)
        self.assertEqual(result['state'], 'succeeded')
        output = next(k for k, v in result['artifacts'].items() if v['kind'] == 'output')
        exported = self.runtime.export(key, output, str(self.root / 'export.mp4'))
        self.assertEqual(exported['sha256'], file_hash(self.video))
        self.assertEqual(inspect_video(Path(exported['path'])).duration, 4)
        subprocess.run([str(resolve_ffmpeg()), '-v', 'error', '-i', exported['path'], '-f', 'null', '-'], check=True)
        self.assertEqual(self.provider.submit.call_count, 1)
        self.assertEqual(self.provider.download.call_count, 1)
        self.assertEqual(self.accounts.charge_once.call_args.kwargs['user_id'], 7)

    def test_real_http_expired_quote_requires_new_approval_without_paid_post(self):
        key = self.plan_and_fixture_approve()
        self.now += 901
        self.runtime.submit(key)
        self.runtime.pool.submit(lambda: None).result(timeout=10)
        self.assertEqual(self.runtime.status(key)['state'], 'needs_replan')
        self.provider.submit.assert_not_called()
        self.accounts.charge_once.assert_not_called()


if __name__ == '__main__':
    unittest.main()
