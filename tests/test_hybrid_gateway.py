from __future__ import annotations
import copy
import hashlib
import io
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

from hybrid_shared import HybridError, validate_manifest
from yzzh_cloud.account import BillingQuote, MiyoIntegrationError, VerifiedAccount
from yzzh_cloud.app import create_app

VIDEO = b'\x00\x00\x00\x20ftypisom' + b'0' * 64
IMAGE = b'\x89PNG\r\n\x1a\n' + b'0' * 64
MODEL = 'doubao-seedance-2-0-test'


def account(owner=7):
    return VerifiedAccount(owner, 9, 1800000000000, 'fixture', 'customer', Decimal('10'), 4,
                           'miyo_fashion', 'monthly', '2030-01-01', 30)


def manifest():
    return dict(prompt='只替换服装，保留原视频动作', model=MODEL, resolution='720p', ratio='9:16', duration=5,
                assets=[dict(slot=f'asset_{i}', kind=k, sha256=hashlib.sha256(b).hexdigest(), size=len(b))
                        for i, (k, b) in enumerate([('video', VIDEO), ('image', IMAGE)])])


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.accounts = Mock()
        self.accounts.verify_token.side_effect = lambda token: account(8 if token == 'other' else 7)
        self.accounts.quote_paid_stage.return_value = BillingQuote('seedance-video', 'tokens', Decimal('0.042'))
        self.accounts.charge_once.return_value.public.return_value = {'charged': True, 'cost': 0.084}
        self.provider = Mock()
        self.provider.submit.return_value = 'provider-private-id'
        self.provider.query.return_value = {'status': 'succeeded', 'usage': {'total_tokens': 2000},
                                            'content': {'video_url': 'https://private.example/output?signature=redacted-fixture'}}
        self.provider.download.side_effect = lambda task, path: Path(path).write_bytes(VIDEO)
        self.now = 1000
        self.app = create_app(self.temp.name, accounts=self.accounts, provider=self.provider, models=[MODEL], now=lambda: self.now)
        self.client = self.app.test_client()
        self.headers = {'Authorization': 'Bearer fixture'}

    def quote(self, data=None):
        return self.client.post('/v1/quotes', headers=self.headers, json=data or manifest())

    def submit(self, key, *, video=VIDEO, headers=None):
        return self.client.post(f'/v1/tasks/{key}/submit', headers=headers or self.headers,
                               data={'asset_0': (io.BytesIO(video), 'video.mp4'), 'asset_1': (io.BytesIO(IMAGE), 'image.png')})

    def test_missing_auth_and_readiness_boundary(self):
        self.assertEqual(self.client.get('/health').status_code, 200)
        self.assertEqual(self.client.get('/v1/account').status_code, 401)
        self.accounts.verify_token.assert_not_called()

    def test_signed_account_error_is_not_bypassed(self):
        self.accounts.verify_token.side_effect = MiyoIntegrationError('private error', code='TOKEN_EXPIRED', status_code=401)
        response = self.quote()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json, {'error': 'TOKEN_EXPIRED'})
        self.accounts.quote_paid_stage.assert_not_called()

    def test_central_quote_uses_verified_identity(self):
        result = self.quote().json
        self.assertEqual(result['quote']['unit_price'], '0.042')
        self.assertEqual(self.accounts.quote_paid_stage.call_args.args[0], 7)
        self.assertNotIn('owner', result)

    def test_user_price_usage_url_injection_rejected(self):
        for key, value in [('owner',8), ('unit_price',0), ('usage',{'total_tokens':0}), ('url','http://127.0.0.1/')]:
            bad = manifest(); bad[key] = value
            self.assertEqual(self.quote(bad).status_code, 400)
        bad = manifest(); bad['model'] = 'unapproved-model'
        self.assertEqual(self.quote(bad).status_code, 400)
        self.provider.submit.assert_not_called()

    def test_other_account_cannot_submit_query_or_download(self):
        key = self.quote().json['id']
        other = {'Authorization': 'Bearer other'}
        self.assertEqual(self.submit(key, headers=other).status_code, 404)
        for suffix in ('', '/output'):
            self.assertEqual(self.client.get(f'/v1/tasks/{key}{suffix}', headers=other).status_code, 404)
        self.provider.submit.assert_not_called()

    def test_expired_quote_and_changed_price_block_before_upload(self):
        key = self.quote().json['id']; self.now += 901
        self.assertEqual(self.submit(key).status_code, 409)
        self.now = 1000
        self.accounts.quote_paid_stage.return_value = BillingQuote('seedance-video', 'tokens', Decimal('0.1'))
        self.assertEqual(self.submit(key).status_code, 409)
        self.provider.submit.assert_not_called()

    def test_changed_material_blocks_paid_call(self):
        key = self.quote().json['id']
        self.assertEqual(self.submit(key, video=b'changed').json['state'], 'failed')
        self.provider.submit.assert_not_called()

    def test_duplicate_submission_only_creates_one_paid_task(self):
        key = self.quote().json['id']
        self.assertEqual(self.submit(key).json['state'], 'running')
        self.submit(key)
        self.assertEqual(self.provider.submit.call_count, 1)

    def test_unknown_submit_never_retries_even_after_restart(self):
        self.provider.submit.side_effect = TimeoutError('private-secret-detail')
        key = self.quote().json['id']
        self.assertEqual(self.submit(key).json['state'], 'submit_uncertain')
        second = create_app(self.temp.name, accounts=self.accounts, provider=self.provider, models=[MODEL]).test_client()
        response = second.post(f'/v1/tasks/{key}/submit', headers=self.headers)
        self.assertEqual(response.json['state'], 'submit_uncertain')
        self.assertEqual(self.provider.submit.call_count, 1)
        self.assertNotIn('private-secret', response.get_data(as_text=True))

    def test_success_is_stored_before_charge_and_response_is_filtered(self):
        key = self.quote().json['id']; self.submit(key)
        self.accounts.charge_once.side_effect = lambda **kw: (
            self.assertTrue((Path(self.temp.name) / key / 'output.mp4').is_file()) or Mock(public=lambda: {'charged': True}))
        response = self.client.get(f'/v1/tasks/{key}', headers=self.headers)
        self.assertEqual(response.json['state'], 'succeeded')
        self.assertNotIn('provider-private', response.get_data(as_text=True))
        self.assertNotIn('signature', response.get_data(as_text=True))
        self.client.get(f'/v1/tasks/{key}', headers=self.headers)
        self.assertEqual(self.accounts.charge_once.call_count, 1)
        with self.client.get(f'/v1/tasks/{key}/output', headers=self.headers) as output:
            self.assertEqual(output.data, VIDEO)

    def test_concurrent_submit_claims_only_once(self):
        key = self.quote().json['id']
        entered, release = threading.Event(), threading.Event()
        def submit_provider(*args):
            entered.set(); release.wait(3)
            return 'one-task'
        self.provider.submit.side_effect = submit_provider
        first = threading.Thread(target=lambda: self.submit(key))
        first.start()
        self.assertTrue(entered.wait(2))
        duplicate = self.app.test_client().post(f'/v1/tasks/{key}/submit', headers=self.headers)
        self.assertEqual(duplicate.json['state'], 'submitting')
        release.set(); first.join(3)
        self.assertFalse(first.is_alive())
        self.assertEqual(self.provider.submit.call_count, 1)

    def test_billing_failure_keeps_output_and_only_reconciles(self):
        key = self.quote().json['id']; self.submit(key)
        self.accounts.charge_once.side_effect = MiyoIntegrationError('private', code='BILLING_FAIL', status_code=503)
        result = self.client.get(f'/v1/tasks/{key}', headers=self.headers)
        self.assertEqual(result.json['state'], 'reconciliation_required')
        self.assertTrue((Path(self.temp.name) / key / 'output.mp4').is_file())
        self.assertEqual(self.client.get(f'/v1/tasks/{key}/output', headers=self.headers).status_code, 409)
        self.accounts.charge_once.side_effect = None
        self.assertEqual(self.client.get(f'/v1/tasks/{key}', headers=self.headers).json['state'], 'succeeded')
        self.assertEqual(self.provider.submit.call_count, 1)
        self.assertEqual(self.provider.download.call_count, 1)

    def test_query_network_failure_can_be_retried_without_resubmit(self):
        key = self.quote().json['id']; self.submit(key)
        self.provider.query.side_effect = TimeoutError('raw-provider-secret')
        response = self.client.get(f'/v1/tasks/{key}', headers=self.headers)
        self.assertEqual(response.json['state'], 'running')
        self.assertNotIn('raw-provider', response.get_data(as_text=True))
        self.assertEqual(self.provider.submit.call_count, 1)


if __name__ == '__main__':
    unittest.main()
