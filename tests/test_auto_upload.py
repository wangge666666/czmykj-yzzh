"""No external network: synthetic transport, UI and original-workflow contracts."""
import io
import json
import tempfile
import types
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from hybrid_shared import HybridError
from yzzh_local.media import TemporaryMediaStore, TEMP_UPLOAD_URL, TEMP_NOTICE, upload_mode, validate_temporary_url
from yzzh_local.original_media import install_media, library_status
from yzzh_local.original_worker import NetworkGuard, isolated_session_init
from yzzh_local.provider import ArkProvider
from yzzh_local.settings import Settings

URL = "https://litter.catbox.moe/fixture123.mp4"


def response(body, status=200, **headers):
    result = requests.Response()
    result.status_code = status
    result._content = body
    result._content_consumed = True
    result.headers.update(headers)
    return result


class AutoUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / "private-client-name.mp4"
        self.video.write_bytes(b"synthetic-mp4-fixture" * 10)
        self.data = self.video.read_bytes()

    def test_key_only_settings_and_legacy_tos_are_distinct(self):
        settings = Settings(self.root)
        state = settings.save(1, {"ARK_API_KEY": "fixture-key", "ARK_MODEL": "fixture-model"})
        self.assertTrue(state["configured"])
        self.assertEqual(state["upload_mode"], "temporary")
        self.assertIn("Litterbox", state["upload"]["storage"])
        self.assertNotIn("fixture-key", json.dumps(state))
        state = settings.save(1, {"MEDIA_UPLOAD_MODE": "tos"})
        self.assertFalse(state["configured"])
        self.assertIn("TOS_BUCKET", state["missing"])
        state = settings.save(1, {"MEDIA_UPLOAD_MODE": "temporary"})
        self.assertTrue(state["configured"])
        self.assertEqual(upload_mode({"TOS_BUCKET": "fixture-bucket", "TOS_ACCESS_KEY": "x", "TOS_SECRET_KEY": "y"}), "tos")
        self.assertFalse(settings.clear(1)["configured"])

    def test_upload_uses_random_filename_no_keys_and_verifies_bytes(self):
        session = Mock()
        session.post.return_value = response(URL.encode())
        session.get.return_value = response(self.data, **{"Content-Length": str(len(self.data))})
        store = TemporaryMediaStore(session)
        result = store.upload_video(self.video)
        self.assertEqual(result.signed_url, URL)
        self.assertFalse(session.trust_env)
        args, kw = session.post.call_args
        self.assertEqual(args, (TEMP_UPLOAD_URL,))
        self.assertFalse(kw["allow_redirects"])
        self.assertNotIn("headers", kw)
        self.assertNotIn("private-client", kw["files"]["fileToUpload"][0])
        self.assertEqual(kw["data"], {"reqtype": "fileupload", "time": "72h"})
        self.assertEqual(session.get.call_args.args[0], URL)
        self.assertFalse(store.delete(result.object_key))

    def test_host_cookie_is_not_replayed_by_guarded_upload_or_verification(self):
        """Use requests' actual cookie handling; never contact the real host."""
        sent = []
        data = self.data

        class FixtureAdapter(requests.adapters.BaseAdapter):
            def send(self, prepared, **kwargs):
                sent.append((prepared.method, dict(prepared.headers)))
                body = URL.encode() if prepared.method == "POST" else data
                result = response(body, **{"Content-Length": str(len(body))})
                result.request, result.url = prepared, prepared.url
                message = Message()
                if prepared.method == "POST":
                    message.add_header("Set-Cookie", "fixture=synthetic; Domain=.catbox.moe; Path=/; Secure")
                result.raw = types.SimpleNamespace(_original_response=types.SimpleNamespace(msg=message))
                return result

            def close(self):
                pass

        # Match the worker's privacy isolation, while keeping the genuine
        # Session.send implementation that extracts response cookies.
        with patch.object(requests.Session, "__init__", isolated_session_init(requests.Session.__init__)), \
                patch("requests.sessions.get_netrc_auth", side_effect=AssertionError("No ambient authentication")):
            session = requests.Session()
            session.mount("https://", FixtureAdapter())
            guard = NetworkGuard({"callback": "http://127.0.0.1:1", "key": "fixture",
                                  "values": {"MEDIA_UPLOAD_MODE": "temporary"}})
            events = []

            def callback(event, **values):
                events.append(event)
                if event == "request":
                    return {"id": "fixture"}
                if event == "take":
                    return {"state": "send_once"}
                return {}

            guard.callback = callback
            session.send = lambda prepared, **kwargs: guard.send(session, prepared, **kwargs)
            store = TemporaryMediaStore(session)
            try:
                # A later upload must remain anonymous too, even if the first
                # upload response tried to set a shared parent-domain cookie.
                for _ in range(2):
                    self.assertEqual(store.upload_video(self.video).signed_url, URL)
            finally:
                session.close()
                guard.control.close()

        self.assertEqual([method for method, _ in sent], ["POST", "GET", "POST", "GET"])
        for _, headers in sent:
            self.assertFalse({key.lower() for key in headers} & {"authorization", "cookie", "x-api-key"})
        self.assertEqual(events, ["request", "take", "finish", "read"] * 2)

    def test_bad_urls_bodies_and_redirects_do_not_start_fetch(self):
        for url in ["http://litter.catbox.moe/a.mp4", "https://evil.invalid/a.mp4", "https://litter.catbox.moe.evil.invalid/a.mp4",
                    "https://user@litter.catbox.moe/a.mp4", URL + "?key=fixture", "https://litter.catbox.moe/../upload", "https://litter.catbox.moe/%2e%2e.mp4", "https://litter.catbox.moe:443/a.mp4"]:
            with self.assertRaises(HybridError):
                validate_temporary_url(url)
        for payload, status in [(b"<html>not media</html>", 200), (b"x" * 5000, 200), (URL.encode(), 302), (b"fixture-secret", 500)]:
            session = Mock(); session.post.return_value = response(payload, status)
            with self.assertRaises(HybridError) as error:
                TemporaryMediaStore(session).upload_video(self.video)
            self.assertNotIn("fixture-secret", str(error.exception))
            session.get.assert_not_called()

    def test_invalid_file_and_mismatched_link_fail_before_generation(self):
        session = Mock(); store = TemporaryMediaStore(session)
        with self.assertRaises(HybridError): store.upload_file(self.video, maximum_bytes=1)
        session.post.assert_not_called()
        session.post.return_value = response(URL.encode())
        session.get.return_value = response(b"x" * len(self.data), **{"Content-Length": str(len(self.data))})
        with self.assertRaises(HybridError): store.upload_video(self.video)

    def test_original_upload_cannot_bypass_human_approval_or_send_credentials(self):
        raw = Mock(return_value=response(URL.encode()))
        guard = NetworkGuard({"callback": "http://127.0.0.1:1", "key": "fixture", "values": {"MEDIA_UPLOAD_MODE": "temporary"}}, raw_send=raw)
        guard.callback = Mock(side_effect=[{"id": "fixture"}, {"state": "rejected"}])
        with self.assertRaises(RuntimeError):
            guard.send(requests.Session(), requests.Request("POST", TEMP_UPLOAD_URL, data=b"fixture").prepare())
        raw.assert_not_called()
        for headers in ({"Authorization": "Bearer fixture-key"}, {"Cookie": "fixture=token"}, {"X-API-Key": "fixture-key"}):
            with self.assertRaises(RuntimeError):
                guard.send(requests.Session(), requests.Request("POST", TEMP_UPLOAD_URL, headers=headers, data=b"fixture").prepare())
        raw.assert_not_called()
        guard.callback = Mock(side_effect=[{"id": "fixture"}, {"state": "send_once"}, {}])
        result = guard.send(requests.Session(), requests.Request("POST", TEMP_UPLOAD_URL, data=b"fixture").prepare())
        self.assertEqual(raw.call_count, 1)
        self.assertEqual(result.content, URL.encode())
        self.assertTrue(guard.callback.call_args.kwargs["ok"])
        summary = guard.callback.call_args_list[0].kwargs["summary"]
        self.assertEqual(summary["privacy"], TEMP_NOTICE)

    def test_selected_tos_never_sends_to_temporary_host(self):
        raw = Mock()
        guard = NetworkGuard({"callback": "http://127.0.0.1:1", "key": "fixture", "values": {"MEDIA_UPLOAD_MODE": "tos"}}, raw_send=raw)
        with self.assertRaises(RuntimeError):
            guard.send(requests.Session(), requests.Request("POST", TEMP_UPLOAD_URL, data=b"fixture").prepare())
        raw.assert_not_called()

    def test_original_person_and_video_use_the_same_adapter(self):
        core = types.SimpleNamespace(TosMediaStore=Mock(), WorkflowError=RuntimeError)
        web = types.SimpleNamespace(ArkCharacterUploadSource=lambda **kw: kw, SeedanceVideoReferenceSource=lambda **kw: kw)
        uploaded = types.SimpleNamespace(signed_url=URL, object_key="fixture")
        with patch("yzzh_local.original_media.TemporaryMediaStore") as cls:
            cls.return_value.upload_file.return_value = uploaded
            cls.return_value.upload_video.return_value = uploaded
            install_media(web, core, {"MEDIA_UPLOAD_MODE": "temporary"})
            self.assertEqual(web.prepare_ark_character_upload_source(self.video)["url"], URL)
            self.assertEqual(web.prepare_seedance_stable_video_reference(self.video)["url"], URL)
            self.assertFalse(web.TosMediaStore.configured())
            cls.return_value.upload_file.assert_called_once()
            cls.return_value.upload_video.assert_called_once()
            cls.return_value.upload_file.side_effect = HybridError("TEMP_UPLOAD_UNAVAILABLE")
            with self.assertRaisesRegex(RuntimeError, "素材上传服务暂时不可用"):
                web.prepare_ark_character_upload_source(self.video)

    def test_original_library_does_not_claim_tunnel_or_immediate_delete(self):
        state = library_status({"configured": True, "message": "同步失败；TOS 尚未开通，上传新人物时将自动使用项目一次性加密通道"}, "temporary")
        self.assertIn("同步失败", state["message"])
        self.assertIn("Litterbox", state["message"])
        self.assertNotIn("一次性加密", state["message"])
        self.assertEqual(state["storage_mode"], "temporary")

    def test_agent_path_inlines_images_and_uploads_only_video_without_tos(self):
        values = {"ARK_API_KEY": "fixture-key", "ARK_MODEL": "fixture-model", "MEDIA_UPLOAD_MODE": "temporary"}
        provider = ArkProvider(values)
        manifest = {"model": "fixture-model", "resolution": "720p", "ratio": "9:16", "duration": 5, "prompt": "fixture",
                    "assets": [{"slot": "v", "kind": "video"}, {"slot": "i", "kind": "image"}]}
        image = self.root / "fixture.png"; image.write_bytes(b"synthetic-image")
        events = []
        with patch("workflow_core.validate_seedance_reference_video"), patch("yzzh_local.provider.TemporaryMediaStore") as cls, patch.object(provider, "_request", return_value={"id": "fixture-task"}) as post:
            cls.return_value.upload_video.return_value.signed_url = URL
            provider.submit(manifest, {"v": self.video, "i": image}, lambda code, data: events.append(code))
            payload = post.call_args.args[2]
            self.assertEqual(payload["content"][1]["video_url"]["url"], URL)
            self.assertTrue(payload["content"][2]["image_url"]["url"].startswith("data:image/png;base64,"))
            self.assertEqual(events, ["UPLOAD_PLANNED", "PROVIDER_POST_STARTED"])
            cls.return_value.upload_video.assert_called_once()

    def test_ui_keeps_storage_optional_and_discloses_upload(self):
        portal = (Path(__file__).parents[1] / "yzzh_local/web/portal.js").read_text(encoding="utf-8")
        self.assertIn("素材由插件自动上传", portal)
        self.assertIn("高级设置 · 模型与上传方式", portal)
        self.assertIn('class="tos-fields" hidden', portal)
        self.assertIn("确认上传本次素材", portal)
        self.assertIn("插件不能提前删除", portal)
        self.assertIn("火山素材库连接 · 虚拟人像入库时使用", portal)
        self.assertIn("不需要真人认证或存储桶", portal)
        self.assertIn("写实虚拟人像", portal)
        self.assertNotIn("请填写方舟 Key、视频模型和 TOS", portal)


if __name__ == "__main__":
    unittest.main()
