"""Synthetic SSO only: no real portal, credentials, native registration or fees."""
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hybrid_shared import HybridError
from yzzh_local.app import create_app
from yzzh_local.runtime import Runtime


class PortalLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.auth = Mock(login_url='https://portal.fixture.invalid/api/auth/login')
        self.auth.verify.return_value = {'user_id':71,'username':'fixture','product_id':4,'licensed':True}
        self.runtime = Runtime(Path(self.temp.name), account_client=self.auth)
        self.addCleanup(lambda:self.runtime.pool.shutdown(wait=True))
        self.app = create_app(self.runtime, 'private-local-session-fixture', 17879)
        self.addCleanup(self.app.extensions['original_bridge'].close)
        self.client = self.app.test_client()
        self.base = 'http://127.0.0.1:17879'
        self.headers = {'Origin':self.base, 'X-Yzzh-Request':'1'}

    def nonce(self):
        page = self.client.get('/_plugin/launch', base_url=self.base)
        self.assertEqual(page.status_code,200)
        self.assertIn(b'https://portal.fixture.invalid',page.data)
        self.assertNotIn(b'private-local-session-fixture',page.data)
        return re.search(r'name="yzzh-launch-nonce" content="([^"]+)"',page.data.decode())[1]

    def accept(self, nonce, **changes):
        return self.client.post('/_plugin/launch/accept', base_url=self.base,
            headers=self.headers,json={'nonce':nonce,'token':'fixture-central-token',**changes})

    def test_single_use_handoff_sets_cookie_verifies_central_and_rotates_context(self):
        self.assertEqual(self.client.get('/api/state',base_url=self.base).status_code,401)
        old = self.runtime.session_revision
        nonce = self.nonce()
        result = self.accept(nonce)
        self.assertEqual(result.json,{'logged_in':True,'licensed':True})
        self.auth.login.assert_not_called()
        self.auth.verify.assert_called_once_with('fixture-central-token')
        self.assertNotEqual(old,self.runtime.session_revision)
        self.assertEqual(self.client.get('/api/state',base_url=self.base).status_code,200)
        self.assertNotIn('fixture-central-token',json.dumps(result.json))
        self.assertEqual(self.accept(nonce).status_code,403)
        self.assertEqual(self.auth.verify.call_count,1)

    def test_wrong_origin_no_header_missing_or_forged_nonce_reject_before_verify(self):
        nonce = self.nonce()
        for headers in ({}, {'Origin':'https://evil.invalid','X-Yzzh-Request':'1'}, {'Origin':self.base}):
            response = self.client.post('/_plugin/launch/accept',base_url=self.base,headers=headers,
                                       json={'nonce':nonce,'token':'fixture'})
            self.assertEqual(response.status_code,403)
        self.assertEqual(self.accept('x'*43).status_code,403)
        self.assertEqual(self.accept(nonce,user_id=999).status_code,400)
        self.auth.verify.assert_not_called()

    def test_expired_or_rejected_handoff_never_replaces_current_account(self):
        self.runtime.login_token('existing-fixture')
        self.auth.verify.reset_mock()
        nonce = self.nonce()
        with patch('yzzh_local.launcher.time.monotonic',return_value=10**20):
            self.assertEqual(self.accept(nonce).status_code,403)
        self.auth.verify.assert_not_called()
        nonce = self.nonce()
        self.auth.verify.side_effect = HybridError('LOGIN_REJECTED',401)
        self.assertEqual(self.accept(nonce).status_code,401)
        self.assertEqual(self.runtime.token,'existing-fixture')
        self.assertEqual(self.accept(nonce).status_code,403)

    def test_expired_card_keeps_login_but_no_new_product_work(self):
        self.auth.verify.return_value['licensed'] = False
        self.assertFalse(self.accept(self.nonce()).json['licensed'])
        with self.assertRaises(HybridError) as error:
            self.runtime._verify()
        self.assertEqual(error.exception.code,'PRODUCT_4_LICENSE_REQUIRED')

    def test_browser_contract_checks_origin_window_state_no_token_urls(self):
        source = (Path(__file__).parents[1]/'yzzh_local/web/launch.js').read_bytes().decode('utf-8')
        response = self.client.get('/_plugin/launch.js',base_url=self.base)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data.decode(),source)
        self.assertIn('javascript',response.content_type)
        response.close()
        for fragment in ('event.origin !== loginOrigin','event.source !== parent','event.data.state !== state',
                         "location.replace('/')",'window.opener = null'):
            self.assertIn(fragment,source)
        self.assertNotIn('localStorage',source)
        self.assertNotIn('?token=',source)

    def test_native_script_ignores_url_and_safely_quotes_bundle_paths(self):
        root = Path(__file__).parents[1]/'plugins/czmiyou-yzzh'
        spec = importlib.util.spec_from_file_location('fixture_register',root/'scripts/register_launcher.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        script = module.applescript(Path('/tmp/fixture quote" and space'))
        self.assertIn('on open location ignoredURL',script)
        self.assertNotIn('& ignoredURL',script)
        self.assertIn('\\"',script)
        source = (root/'scripts/start_local.py').read_text(encoding="utf-8")
        self.assertNotIn('sys.argv',source)
        self.assertNotIn('shell=True',source)

    def test_native_registration_rejects_incomplete_bundle_before_os_write(self):
        root = Path(__file__).parents[1]/'plugins/czmiyou-yzzh'
        spec = importlib.util.spec_from_file_location('fixture_register_guard',root/'scripts/register_launcher.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with patch.object(module.subprocess,'run') as run:
            with self.assertRaisesRegex(SystemExit,'Complete bundle setup first'):
                module.register(Path(self.temp.name))
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
