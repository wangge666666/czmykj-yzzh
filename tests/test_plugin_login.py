"""Role-aware login contract. Fixture accounts only; no real credentials."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask, jsonify, request
from werkzeug.serving import WSGIRequestHandler, make_server

from hybrid_shared import HybridError
from yzzh_local.account import AccountClient, LOGIN_ROLES
from yzzh_local.app import create_app
from yzzh_local.runtime import Runtime


class AccountResponseTests(unittest.TestCase):
    def response(self, status, body):
        response = Mock(status_code=status)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [body if isinstance(body, bytes) else json.dumps(body).encode()]
        return response

    def test_allowed_role_sent_exactly_once_and_default_stays_customer(self):
        for role in sorted(LOGIN_ROLES):
            with self.subTest(role=role), patch('yzzh_local.account.requests.post', return_value=self.response(200, {'success':True, 'token':'fixture-token'})) as post:
                self.assertEqual(AccountClient().login('fixture','fixture-password',role), 'fixture-token')
                self.assertEqual(post.call_args.kwargs['json']['role'], role)
                self.assertEqual(post.call_count, 1)
                self.assertFalse(post.call_args.kwargs['allow_redirects'])
        client = AccountClient()
        with patch.object(client, '_post', return_value={'success':True, 'token':'fixture-token'}) as post:
            client.login('fixture','fixture-password')
            self.assertEqual(post.call_args.args[1]['role'], 'customer')

    def test_invalid_roles_make_no_account_request(self):
        with patch('yzzh_local.account.requests.post') as post:
            for role in ('', '员工', 'super_admin', 'STAFF', None, [], {}, True):
                with self.subTest(role=role), self.assertRaises(HybridError) as error:
                    AccountClient().login('fixture','fixture-password',role)
                self.assertEqual(error.exception.code, 'INVALID_LOGIN_ROLE')
            post.assert_not_called()

    def test_known_rejections_safe_on_200_401_403(self):
        for status in (200, 401, 403):
            for message, code, expected_status in (
                ('账号或密码错误','LOGIN_CREDENTIALS_INVALID',401),
                ('身份无效或未审批','LOGIN_ROLE_NOT_APPROVED',403),
            ):
                with self.subTest(status=status,code=code), patch('yzzh_local.account.requests.post', return_value=self.response(status, {'success':False,'error':message})) as post:
                    with self.assertRaises(HybridError) as error:
                        AccountClient().login('fixture','fixture-password','staff')
                    self.assertEqual((error.exception.code,error.exception.status),(code,expected_status))
                    self.assertEqual(post.call_count,1)  # Never retry using another role.

    def test_unknown_errors_and_non_success_http_never_leak_or_login(self):
        for status in (200,401,403):
            for error_value in ('fixture-secret https://private.invalid/?token=fixture', {'password':'fixture-secret'}, ['fixture-secret']):
                with patch('yzzh_local.account.requests.post', return_value=self.response(status, {'success':False,'error':error_value})):
                    with self.assertRaises(HybridError) as error:
                        AccountClient().login('fixture','fixture-password','staff')
                    self.assertEqual(error.exception.code, 'LOGIN_REJECTED')
                    self.assertNotIn('fixture-secret',str(error.exception))
        for status in (401,403):
            with patch('yzzh_local.account.requests.post', return_value=self.response(status, {'success':True,'token':'must-not-accept'})):
                with self.assertRaises(HybridError):
                    AccountClient().login('fixture','fixture-password','staff')

    def test_invalid_bounded_response_and_service_errors(self):
        for status, body, code in (
            (200,b'not-json','LICENSE_INVALID_RESPONSE'),
            (401,b'not-json','LOGIN_REJECTED'),
            (200,b'x' * (128*1024+1),'LICENSE_INVALID_RESPONSE'),
            (200,{'success':True},'LICENSE_INVALID_RESPONSE'),
            (503,{'error':'fixture-secret'},'LICENSE_SERVICE_UNAVAILABLE'),
        ):
            with self.subTest(status=status,code=code), patch('yzzh_local.account.requests.post', return_value=self.response(status,body)):
                with self.assertRaises(HybridError) as error:
                    AccountClient().login('fixture','fixture-password','staff')
                self.assertEqual(error.exception.code,code)

    def test_verify_rejection_is_not_misreported_as_password_error(self):
        with patch('yzzh_local.account.requests.post', return_value=self.response(401,{'error':'账号或密码错误'})):
            with self.assertRaises(HybridError) as error:
                AccountClient().verify('fixture-token')
            self.assertEqual(error.exception.code,'LOGIN_REJECTED')


class LoginHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.account_calls = []
        self.licensed, self.valid = True, True
        fixture = Flask('role-login-fixture')

        @fixture.post('/api/auth/login')
        def login():
            payload = request.get_json()
            self.account_calls.append(('login', payload['role']))
            if payload['password'] != 'fixture-password':
                return jsonify(success=False,error='账号或密码错误')
            if payload['role'] != 'staff':
                return jsonify(success=False,error='身份无效或未审批')
            return jsonify(success=True,token='fixture-token')

        @fixture.post('/api/auth/verify-token')
        def verify():
            self.account_calls.append(('verify',None))
            self.assertEqual(request.get_json(), {'token':'fixture-token'})
            return jsonify(valid=self.valid,userId=71,username='fixture-staff',balance=0,
                           subscriptions=[{'productId':4,'status':'active','endTime':'2100-01-01'}] if self.licensed else [])

        class Quiet(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        self.server = make_server('127.0.0.1',0,fixture,request_handler=Quiet)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(lambda: self.thread.join(3))
        self.addCleanup(self.server.shutdown)
        self.runtime = Runtime(Path(self.temporary.name)/'data',
                               f'http://127.0.0.1:{self.server.server_port}/api/auth/login',development=True)
        self.addCleanup(lambda: self.runtime.pool.shutdown(wait=True))
        self.app = create_app(self.runtime,'fixture-session',17879)
        self.addCleanup(self.app.extensions['original_bridge'].close)
        self.client = self.app.test_client()
        self.headers = {'X-Yzzh-Session':'fixture-session','X-Yzzh-Request':'1'}

    def login(self, **changes):
        data = {'username':'fixture','password':'fixture-password','role':'staff',**changes}
        return self.client.post('/api/login',base_url='http://127.0.0.1:17879',headers=self.headers,json=data)

    def test_staff_end_to_end_and_role_change_rotates_session(self):
        self.assertTrue(self.login().json['licensed'])
        self.assertEqual(self.account_calls,[('login','staff'),('verify',None)])
        previous = self.runtime.session_revision
        response = self.login(role='customer')
        self.assertEqual((response.status_code,response.json['error']),(403,'LOGIN_ROLE_NOT_APPROVED'))
        self.assertNotEqual(previous,self.runtime.session_revision)
        self.assertIsNone(self.runtime.account)
        self.assertEqual(self.runtime.token,'')

    def test_staff_without_card_still_cannot_start_new_work(self):
        self.licensed = False
        response = self.login()
        self.assertEqual(response.status_code,200)
        self.assertFalse(response.json['licensed'])
        with self.assertRaises(HybridError) as error:
            self.runtime.import_video('/fixture/nonexistent.mp4')
        self.assertEqual(error.exception.code,'PRODUCT_4_LICENSE_REQUIRED')

    def test_failed_password_and_verification_never_keep_session(self):
        self.assertEqual(self.login(password='fixture-wrong').json['error'],'LOGIN_CREDENTIALS_INVALID')
        self.assertIsNone(self.runtime.account)
        self.valid = False
        self.assertEqual(self.login().json['error'],'LOGIN_REJECTED')
        self.assertIsNone(self.runtime.account)
        self.assertEqual(self.runtime.token,'')

    def test_http_boundary_rejects_bad_roles_payloads_and_csrf(self):
        for role in ('super_admin',None,{},True):
            self.assertEqual(self.login(role=role).status_code,400)
        self.assertEqual(self.login(licensed=True).status_code,400)
        self.assertEqual(self.client.post('/api/login',base_url='http://127.0.0.1:17879',headers=self.headers,json=[]).status_code,400)
        self.headers.pop('X-Yzzh-Request')
        self.assertEqual(self.login().status_code,403)
        self.assertEqual(self.account_calls,[])

    def test_both_ui_entries_require_explicit_identity_and_safe_errors(self):
        root = Path(__file__).resolve().parents[1]/'yzzh_local'/'web'
        for file in ('index.html','portal.js'):
            text = (root/file).read_text()
            self.assertIn('name="role"',text)
            self.assertIn('selected disabled',text)
            for role in LOGIN_ROLES:
                self.assertIn(f'value="{role}"',text)
        for file in ('app.js','portal.js'):
            text = (root/file).read_text()
            for code in ('LOGIN_CREDENTIALS_INVALID','LOGIN_ROLE_NOT_APPROVED','INVALID_LOGIN_ROLE','LOGIN_REJECTED'):
                self.assertIn(code,text)


if __name__ == '__main__':
    unittest.main()
