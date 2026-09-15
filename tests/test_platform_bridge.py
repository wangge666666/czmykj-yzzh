"""Platform boundaries: synthetic accounts/files and fake HTTP streams only."""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from hybrid_shared import HybridError, digest, file_hash
from yzzh_local.app import create_app
from yzzh_local.original import OriginalBridge, PAGES
from yzzh_local.original_worker import NetworkGuard
from yzzh_local.platform_bridge import WorkerPlatformCallbacks
from yzzh_local.platform_service import PlatformService
from yzzh_local.runtime import Runtime


def capabilities():
    return {"mode": "platform", "product_id": 4, "ready": True,
            "capabilities": {key: True for key in ("video", "image", "analysis", "assets", "media")},
            "models": {"video": "fixture-video", "white": "fixture-white", "image": "fixture-image", "analysis": "fixture-analysis"},
            "projects": ["wardrobe", "virtual", "real"], "balance": 120.5, "licensed": True,
            "fields": {}, "configured": True, "channel": "primary", "asset_library": "canvas-shared-v1"}


class StreamResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.ok = 200 <= status < 300

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_content(self, _size):
        body = json.dumps(self.payload).encode()
        yield body[:7]
        yield body[7:]

    def json(self):
        return copy.deepcopy(self.payload)


class PlatformBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.no_network = patch("requests.sessions.Session.request", side_effect=AssertionError("No real network in platform tests"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)
        self.account = {"user_id": 71, "username": "fixture", "licensed": True, "product_id": 4,
                        "subscription_end": "2030-01-01", "license_status": "active"}
        self.auth = Mock(login_url="https://accounts.example.invalid/api/auth/login")
        self.auth.login.return_value = "synthetic-account-session"
        self.auth.verify.side_effect = lambda _token: copy.deepcopy(self.account)
        self.platform = Mock()
        self.public_capabilities = capabilities()
        self.platform.capabilities.side_effect = lambda *_args, **_kw: copy.deepcopy(self.public_capabilities)
        self.platform.operation.return_value = {"id": "cgt-fixture", "items": []}
        self.platform.upload.return_value = {"object_key": "synthetic/media.mp4", "signed_url": "https://media.example.invalid/synthetic.mp4"}
        self.runtime = Runtime(self.root / "data", account_client=self.auth, mode="platform", platform_client=self.platform)
        self.addCleanup(lambda: self.runtime.pool.shutdown(wait=True))
        self.runtime.login("fixture", "synthetic-password")
        for name in ("load", "public", "save", "clear"):
            setattr(self.runtime.settings, name, Mock(side_effect=AssertionError("Platform mode must not read or write BYOK settings")))
        self.bridge = OriginalBridge(self.runtime, 7871)
        self.addCleanup(self.bridge.close)
        self.worker_root = self.root / "synthetic-worker"
        self.worker_root.mkdir()
        self.worker_key = "synthetic-private-worker-binding"
        self.bridge.workers[71] = {"root": str(self.worker_root), "key": self.worker_key,
                                  "process": SimpleNamespace(poll=lambda: 0, stdout=None)}
        _, _, revision = self.bridge.context(71, self.runtime.session_revision)
        self.operation_id = "synthetic-operation"
        self.bridge.operations[self.operation_id] = {"owner": 71, "session": self.runtime.session_revision,
            "revision": revision, "path": "/api/wardrobe-swap/prepare", "method": "POST", "worker_key": self.worker_key}
        self.stage = "wardrobe-white-model"

    def request(self, command, payload, request_id=None, **overrides):
        data = {"operation": self.operation_id, "command": command, "payload": payload,
                "stage": self.stage, "id": request_id, **overrides}
        return self.bridge.platform_request(data, self.worker_key)

    def pending(self, command, payload, *, decision=None, consent=None, take=False):
        summary = {"destination": "米哟平台", "operation": command, "stage": self.stage,
                   "payload_sha256": digest(payload)}
        if command == "media.upload":
            source = Path(payload["path"])
            summary.update(bytes=source.stat().st_size, sha256=file_hash(source))
        key = self.bridge.network({"operation": self.operation_id, "event": "request", "summary": summary}, self.worker_key)["id"]
        if decision is not None:
            data = {"owner": 71, "session": self.runtime.session_revision, "id": key, "approved": decision}
            if consent is not None:
                data["compliance_confirmed"] = consent
            self.bridge.decide(data)
        if take:
            self.assertEqual(self.bridge.network({"operation": self.operation_id, "event": "take", "id": key}, self.worker_key),
                             {"state": "send_once"})
        return key

    def assert_no_platform_io(self):
        self.platform.operation.assert_not_called()
        self.platform.upload.assert_not_called()

    def test_reads_use_current_owner_session_without_creating_approval(self):
        for path, stage, project in (("/api/wardrobe-swap/prepare", "wardrobe-query", "wardrobe"),
                                     ("/api/long-video/query", "long-query", "virtual"),
                                     ("/api/real-long-video/query", "real-long-query", "real")):
            with self.subTest(project=project):
                self.bridge.operations[self.operation_id]["path"] = path
                self.request("video.list", {"page_size": 1}, stage=stage)
                token, envelope = self.platform.operation.call_args.args
                self.assertEqual(token, "synthetic-account-session")
                self.assertEqual(envelope["project"], project)
                self.assertEqual(envelope["stage"], stage)
                self.assertEqual(envelope["operation"], "video.list")
                self.assertEqual(envelope["payload"], {"page_size": 1})
                self.assertRegex(envelope["request_id"], r"^[a-f0-9]{64}$")
        self.assertEqual(self.bridge.pending, {})

    def test_invalid_worker_owner_session_revision_and_mode_block_reads_before_http(self):
        operation = self.bridge.operations[self.operation_id]
        original = dict(operation)
        for field, value in (("owner", 72), ("session", "stale-session"), ("revision", "stale-revision")):
            with self.subTest(field=field):
                operation[field] = value
                with self.assertRaises(HybridError):
                    self.request("video.get", {"task_id": "cgt-fixture"})
                operation.update(original)
        with self.assertRaises(HybridError):
            self.bridge.platform_request({"operation": self.operation_id}, "incorrect-worker")
        self.runtime.mode = "byok"
        with self.assertRaises(HybridError):
            self.request("video.list", {})
        self.runtime.mode = "platform"
        self.runtime.logout()
        with self.assertRaises(HybridError):
            self.request("video.list", {})
        self.assert_no_platform_io()

    def test_writes_require_approval_and_the_sending_transition(self):
        payload = {"model": "fixture-video", "duration": 6}
        for decision in (None, False, True):
            with self.subTest(decision=decision):
                key = self.pending("video.create", payload, decision=decision)
                with self.assertRaises(HybridError) as error:
                    self.request("video.create", payload, key)
                self.assertEqual(error.exception.code, "NETWORK_APPROVAL_REQUIRED")
        self.assert_no_platform_io()
        key = self.pending("video.create", payload, decision=True, take=True)
        self.assertEqual(self.request("video.create", payload, key)["id"], "cgt-fixture")
        self.platform.operation.assert_called_once()
        self.assertEqual(self.platform.operation.call_args.args[1]["request_id"], hashlib.sha256(key.encode()).hexdigest())

    def test_approved_write_cannot_change_payload_stage_command_or_owner(self):
        payload = {"model": "fixture-video", "duration": 6}
        key = self.pending("video.create", payload, decision=True, take=True)
        for command, body, stage in (("video.create", {**payload, "duration": 8}, self.stage),
                                     ("video.create", payload, "different-stage"),
                                     ("image.generate", payload, self.stage)):
            with self.subTest(command=command, stage=stage, body=body):
                with self.assertRaises(HybridError) as error:
                    self.request(command, body, key, stage=stage)
                self.assertEqual(error.exception.code, "APPROVED_REQUEST_CHANGED")
        self.runtime.account["user_id"] = 72
        with self.assertRaises(HybridError):
            self.request("video.create", payload, key)
        self.assert_no_platform_io()

    def test_upload_paths_outside_worker_and_symlink_escapes_are_blocked(self):
        outside = self.root / "outside.png"
        outside.write_bytes(b"synthetic image outside worker")
        link = self.worker_root / "escape.png"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                self.skipTest("This Windows account cannot create symbolic links")
            raise
        for source in (outside, link):
            with self.subTest(source=source.name):
                payload = {"path": str(source)}
                key = self.pending("media.upload", payload, decision=True, take=True)
                with self.assertRaises(HybridError) as error:
                    self.request("media.upload", payload, key)
                self.assertEqual(error.exception.code, "INVALID_PLATFORM_MEDIA")
                self.bridge.network({"operation": self.operation_id, "event": "finish", "id": key, "ok": True}, self.worker_key)
        self.assert_no_platform_io()

    def test_upload_changed_bytes_cannot_send_even_when_path_matches_approval(self):
        source = self.worker_root / "synthetic.mp4"
        source.write_bytes(b"approved synthetic bytes")
        payload = {"path": str(source)}
        key = self.pending("media.upload", payload, decision=True, take=True)
        source.write_bytes(b"different synthetic byte")
        with self.assertRaises(HybridError) as error:
            self.request("media.upload", payload, key)
        self.assertEqual(error.exception.code, "APPROVED_MEDIA_CHANGED")
        self.assert_no_platform_io()

    def test_upload_sends_an_approved_snapshot_and_cleans_only_that_snapshot(self):
        source = self.worker_root / "synthetic.mp4"
        source.write_bytes(b"approved synthetic bytes")
        payload = {"path": str(source)}
        key = self.pending("media.upload", payload, decision=True, take=True)
        snapshots = []
        def uploaded(token, path, request_id, project, stage):
            self.assertEqual(token, "synthetic-account-session")
            self.assertNotEqual(path, source)
            self.assertEqual(path.read_bytes(), b"approved synthetic bytes")
            self.assertEqual((project, stage), ("wardrobe", self.stage))
            self.assertEqual(request_id, hashlib.sha256(key.encode()).hexdigest())
            snapshots.append(path)
            return {"object_key": "synthetic/object", "signed_url": "https://media.example.invalid/synthetic.mp4"}
        self.platform.upload.side_effect = uploaded
        self.assertIn("signed_url", self.request("media.upload", payload, key))
        self.assertEqual(source.read_bytes(), b"approved synthetic bytes")
        self.assertFalse(snapshots[0].exists())
        self.platform.operation.assert_not_called()

    def test_asset_consent_must_come_from_the_actual_strict_decision(self):
        payload = {"GroupId": "group-fixture", "URL": "https://media.example.invalid/portrait.png"}
        key = self.pending("assets.CreateAsset", payload)
        for consent in (None, False, "true", 1):
            with self.subTest(consent=consent):
                with self.assertRaises(HybridError) as error:
                    self.bridge.decide({"owner": 71, "session": self.runtime.session_revision, "id": key,
                                        "approved": True, "compliance_confirmed": consent})
                self.assertEqual(error.exception.code, "ASSET_CONSENT_REQUIRED")
                self.assertEqual(self.bridge.pending[key]["state"], "awaiting_approval")
        self.bridge.decide({"owner": 71, "session": self.runtime.session_revision, "id": key,
                            "approved": True, "compliance_confirmed": True})
        self.bridge.network({"operation": self.operation_id, "event": "take", "id": key}, self.worker_key)
        self.request("assets.CreateAsset", payload, key)
        self.assertEqual(self.platform.operation.call_args.args[1]["payload"], {**payload, "compliance_confirmed": True})
        self.assertNotIn("compliance_confirmed", payload)

    def test_payload_cannot_supply_its_own_asset_consent(self):
        payload = {"compliance_confirmed": True, "URL": "https://media.example.invalid/portrait.png"}
        key = self.pending("assets.CreateAsset", payload)
        self.bridge.pending[key]["state"] = "sending"  # Corrupt/incomplete journal must still fail closed.
        with self.assertRaises(HybridError) as error:
            self.request("assets.CreateAsset", payload, key)
        self.assertEqual(error.exception.code, "ASSET_CONSENT_REQUIRED")
        self.assert_no_platform_io()

    def test_runtime_platform_metadata_and_blocked_settings_never_read_local_keys(self):
        public = self.runtime.provider_settings()
        self.assertEqual(public["mode"], "platform")
        self.assertEqual(public["owner_id"], 71)
        self.assertEqual(public["fields"], {})
        self.assertNotIn("synthetic-account-session", json.dumps(public))
        with self.assertRaises(HybridError) as error:
            self.runtime.save_settings({"ARK_API_KEY": "synthetic-never-saved"}, 71, self.runtime.session_revision)
        self.assertEqual(error.exception.code, "PLATFORM_SETTINGS_MANAGED_BY_ADMIN")
        self.assertEqual(self.runtime.health()["mode"], "platform")
        self.assertEqual(self.runtime.health()["projects"], ["wardrobe", "virtual", "real"])
        for name in ("load", "public", "save", "clear"):
            getattr(self.runtime.settings, name).assert_not_called()

    def test_legacy_agent_generation_is_blocked_before_loading_or_mutating_old_records(self):
        actions = (
            lambda: self.runtime.plan("synthetic-project", "synthetic-artifact", [], "fixture", ""),
            lambda: self.runtime.submit("synthetic-project"),
            lambda: self.runtime.poll("synthetic-project"),
            lambda: self.runtime._check_plan({"owner": 71}),
        )
        with patch.object(self.runtime.store, "get", side_effect=AssertionError("Must not load a legacy record")), \
             patch.object(self.runtime.store, "put", side_effect=AssertionError("Must not mutate a legacy record")), \
             patch.object(self.runtime, "provider_factory", side_effect=AssertionError("No BYOK provider allowed")):
            for action in actions:
                with self.assertRaises(HybridError) as error:
                    action()
                self.assertEqual(error.exception.code, "PLATFORM_USE_PROJECT_WORKFLOWS")
                self.assertEqual(error.exception.status, 409)
        self.runtime.settings.load.assert_not_called()
        self.assert_no_platform_io()

    def test_three_page_routes_and_browser_cannot_call_worker_platform_endpoint(self):
        app = create_app(self.runtime, "synthetic-local-cookie", 7871)
        self.addCleanup(app.extensions["original_bridge"].close)
        client = app.test_client()
        for path in ("/projects/wardrobe", "/projects/long-video", "/projects/real-long-video"):
            self.assertIn(path, PAGES)
            response = client.get(path, base_url="http://127.0.0.1:7871")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'/_plugin/portal.js', response.data)
            self.assertIn(b'<meta name="yzzh-service-mode" content="platform">', response.data)
        response = client.post("/_plugin/platform", base_url="http://127.0.0.1:7871", json={
            "operation": self.operation_id, "command": "video.list", "payload": {}, "stage": "main"},
            headers={"X-Yzzh-Session": "synthetic-local-cookie", "X-Yzzh-Request": "1"})
        self.assertEqual(response.status_code, 403)
        self.assert_no_platform_io()

    def test_worker_direct_supplier_posts_are_denied_in_platform_mode(self):
        raw = Mock(side_effect=AssertionError("No supplier transport may run"))
        guard = NetworkGuard({"values": {"__platform__": capabilities()}, "key": "synthetic-worker", "callback": "http://127.0.0.1:7871/_plugin/engine"}, raw_send=raw)
        guard.callback = Mock()
        for url in ("https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks", "https://open.volcengineapi.com/?Action=ListAssets"):
            with self.subTest(url=url):
                prepared = requests.Request("POST", url, json={"model": "fixture"}).prepare()
                with self.assertRaisesRegex(RuntimeError, "必须通过已确认的平台接口"):
                    guard.send(requests.Session(), prepared)
        raw.assert_not_called()
        guard.callback.assert_not_called()

    def test_cli_default_is_platform_and_byok_requires_explicit_selection(self):
        tree = ast.parse((Path(__file__).parents[1] / "yzzh_local/app.py").read_text(encoding="utf-8"))
        option = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"
                      and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "--mode")
        values = {item.arg: ast.literal_eval(item.value) for item in option.keywords}
        self.assertEqual(values["default"], "platform")
        self.assertEqual(values["choices"], ("platform", "byok"))

    def test_original_config_uses_platform_readiness_without_local_provider_keys(self):
        # Exercise the actual response adapter without starting a worker, model,
        # HTTP listener, or reading its private runtime configuration.
        path = Path(__file__).parents[1] / "yzzh_local/original_worker.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        function = copy.deepcopy(next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "safe_result"))
        function.decorator_list = []
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        for ready in (True, False):
            for media in (True, False, "true"):
                with self.subTest(ready=ready, media=media):
                    state = {**capabilities(), "ready": ready, "capabilities": {**capabilities()["capabilities"], "media": media}}
                    namespace = {"json": json, "request": SimpleNamespace(path="/api/config"), "media_mode": "platform",
                                 "platform_capabilities": state, "TEMP_NOTICE": "fixture", "secrets_to_hide": (),
                                 "redact": lambda value, _secrets: value,
                                 "remote_images": SimpleNamespace(register=lambda value: value),
                                 "web": SimpleNamespace(ark_assets_configured=lambda: ready,
                                                        TosMediaStore=SimpleNamespace(configured=lambda: False))}
                    exec(compile(module, str(path), "exec"), namespace)
                    result = {}
                    response = SimpleNamespace(is_json=True, get_json=lambda **_kw: {"ark_ready": False},
                                               set_data=lambda body: result.update(json.loads(body)))
                    self.assertIs(namespace["safe_result"](response), response)
                    self.assertEqual(result["ark_ready"], ready)
                    self.assertEqual(result["temporary_upload_ready"], ready and media is True)
                    self.assertEqual(result["plugin_service_mode"], "platform")


class WorkerPlatformCallbackTests(unittest.TestCase):
    def guard(self, state="send_once"):
        guard = SimpleNamespace(config={"callback": "http://127.0.0.1:7871/_plugin/engine", "key": "synthetic-worker"}, control=Mock())
        events = []
        def callback(event, **values):
            events.append((event, copy.deepcopy(values)))
            return {"id": "synthetic-network-id"} if event == "request" else {"state": state}
        guard.callback = callback
        guard.control.post.return_value = StreamResponse({"result": {"id": "cgt-fixture", "items": []}})
        return guard, events

    def test_worker_reads_request_only_binding_and_never_approval(self):
        guard, events = self.guard()
        callbacks = WorkerPlatformCallbacks(guard, SimpleNamespace(get=lambda: "synthetic-operation"))
        self.assertEqual(callbacks.rpc("video.list", {"page_size": 1})["items"], [])
        self.assertEqual([event for event, _ in events], ["read"])
        self.assertEqual(guard.control.post.call_args.kwargs["json"]["id"], None)

    def test_worker_mutation_records_exact_reviewed_payload_and_finishes_once(self):
        guard, events = self.guard()
        payload = {"model": "fixture", "duration": 6}
        callbacks = WorkerPlatformCallbacks(guard, SimpleNamespace(get=lambda: "synthetic-operation"))
        callbacks.rpc("video.create", payload)
        self.assertEqual([event for event, _ in events], ["request", "take", "finish"])
        summary = events[0][1]["summary"]
        self.assertEqual(summary["payload_sha256"], digest(payload))
        self.assertEqual(summary["operation"], "video.create")
        self.assertEqual(guard.control.post.call_args.kwargs["json"]["stage"], summary["stage"])
        self.assertEqual(events[-1][1], {"id": "synthetic-network-id", "ok": True, "task_id": "cgt-fixture"})
        guard.control.post.assert_called_once()

    def test_companion_timeout_is_uncertain_without_retry_or_private_error_text(self):
        guard, events = self.guard()
        guard.control.post.side_effect = requests.Timeout("synthetic-private-transport-detail")
        callbacks = WorkerPlatformCallbacks(guard, SimpleNamespace(get=lambda: "synthetic-operation"))
        with self.assertRaisesRegex(Exception, "平台回执尚未确认") as caught:
            callbacks.rpc("video.create", {"model": "fixture"})
        self.assertNotIn("synthetic-private", str(caught.exception))
        guard.control.post.assert_called_once()
        self.assertEqual(events[-1][1]["ok"], False)

    def test_worker_rejection_sends_no_platform_http(self):
        guard, events = self.guard(state="rejected")
        callbacks = WorkerPlatformCallbacks(guard, SimpleNamespace(get=lambda: "synthetic-operation"))
        with self.assertRaisesRegex(Exception, "未获批准"):
            callbacks.rpc("video.create", {"model": "fixture"})
        self.assertEqual([event for event, _ in events], ["request", "take"])
        guard.control.post.assert_not_called()


class PlatformServiceTests(unittest.TestCase):
    def test_media_upload_sends_filename_bytes_mime_and_account_binding_together(self):
        http = Mock()
        observed = {}
        def upload_request(method, url, **options):
            self.assertEqual((method, url), ("POST", "https://platform.example.invalid/api/yzzh/media"))
            name, stream, mime_type = options["files"]["file"]
            self.assertEqual((name, mime_type), ("synthetic.mp4", "video/mp4"))
            self.assertEqual(stream.read(), b"synthetic media bytes")
            self.assertEqual(options["data"], {"request_id": "a" * 64, "project": "real", "stage": "real-analysis"})
            self.assertEqual(options["headers"]["Authorization"], "Bearer synthetic-account-session")
            observed["stream"] = stream
            return StreamResponse({"result": {"object_key": "fixture/object", "signed_url": "https://media.example.invalid/fixture.mp4"}})
        http.request.side_effect = upload_request
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic.mp4"
            source.write_bytes(b"synthetic media bytes")
            result = PlatformService("https://platform.example.invalid", http=http).upload(
                "synthetic-account-session", source, "a" * 64, "real", "real-analysis")
            self.assertEqual(result["object_key"], "fixture/object")
        self.assertTrue(observed["stream"].closed)
        http.request.assert_called_once()

    def test_capabilities_use_account_auth_and_never_return_private_provider_fields(self):
        http = Mock()
        http.request.return_value = StreamResponse({**capabilities(), "ARK_API_KEY": "synthetic-provider-private"})
        service = PlatformService("https://platform.example.invalid", http=http)
        result = service.capabilities("synthetic-account-session", 71, "synthetic-revision")
        self.assertTrue(result["ready"])
        self.assertEqual(http.request.call_args.args, ("GET", "https://platform.example.invalid/api/yzzh/capabilities"))
        self.assertEqual(http.request.call_args.kwargs["headers"]["Authorization"], "Bearer synthetic-account-session")
        self.assertFalse(http.request.call_args.kwargs["allow_redirects"])
        self.assertFalse(http.trust_env)
        self.assertNotIn("synthetic-provider-private", json.dumps(result))
        result["capabilities"]["video"] = False
        self.assertTrue(service.capabilities("synthetic-account-session", 71, "synthetic-revision")["capabilities"]["video"])
        http.request.assert_called_once()

    def test_capability_flags_and_project_coverage_fail_closed(self):
        for field, value in (("ready", "true"), ("projects", ["wardrobe"]), ("capabilities", {"video": "true"}), ("models", {"video":"fixture"})):
            with self.subTest(field=field):
                http = Mock()
                http.request.return_value = StreamResponse({**capabilities(), field: value})
                self.assertFalse(PlatformService("https://platform.example.invalid", http=http).capabilities("synthetic-session", 71, "revision")["ready"])

    def test_posts_are_authenticated_once_without_retry_and_errors_expose_codes_only(self):
        http = Mock()
        http.request.side_effect = requests.ConnectionError("synthetic-private-transport-detail")
        service = PlatformService("https://platform.example.invalid", http=http)
        with self.assertRaises(HybridError) as error:
            service.operation("synthetic-session", {"request_id": "a" * 64})
        self.assertEqual(error.exception.code, "PLATFORM_CONNECTION_UNCERTAIN")
        self.assertNotIn("synthetic-private", str(error.exception))
        http.request.assert_called_once()
        http.request.side_effect = None
        http.request.return_value = StreamResponse({"code": "PROVIDER_REJECTED", "error": "synthetic-provider-key"}, 400)
        with self.assertRaises(HybridError) as error:
            service.operation("synthetic-session", {})
        self.assertEqual(error.exception.code, "PROVIDER_REJECTED")
        self.assertNotIn("synthetic-provider-key", str(error.exception))
        with self.assertRaises(HybridError) as error:
            service.operation("", {})
        self.assertEqual(error.exception.code, "LOGIN_REQUIRED")
        self.assertEqual(http.request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
