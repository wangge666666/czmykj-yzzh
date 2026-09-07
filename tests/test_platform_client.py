from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from performance_analysis import ArkPerformanceAnalyzer
from workflow_core import ArkAPIError, ArkConnectionError, WorkflowError
from yzzh_local.platform import (
    PlatformAssetsClient, PlatformPerformanceAnalyzer, PlatformTransport,
    PlatformVideoClient, install_platform,
)


def capabilities(**overrides):
    return {"mode": "platform", "product_id": 4, "ready": True,
            "projects": ["wardrobe", "virtual", "real"],
            "capabilities": {"video": True, "image": True, "analysis": True, "assets": True, "media": True},
            "models": {"analysis": "fixture-analysis"}, **overrides}


class PlatformClientTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.result = {}
        self.rpc = Mock(side_effect=self._record)
        self.upload = Mock(return_value={"object_key": "synthetic/account/material.mp4",
                                        "signed_url": "https://media.example.invalid/reference.mp4?signature=synthetic"})
        self.transport = PlatformTransport(self.rpc, self.upload)
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("No network in adapter tests"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def _record(self, operation, payload):
        self.calls.append((operation, copy.deepcopy(payload)))
        return copy.deepcopy(self.result)

    def test_video_operations_preserve_parameters_and_native_result_parsing(self):
        client = PlatformVideoClient(self.transport)
        payload = {"model": "fixture-seedance-2.5", "content": [{"type": "text", "text": "原提示词：换服装，保留动作"},
                   {"type": "image_url", "image_url": {"url": "asset://fixture-person"}}],
                   "resolution": "720p", "ratio": "9:16", "duration": 6,
                   "generate_audio": False, "watermark": False}
        self.result = {"id": "cgt-fixture-task"}
        self.assertEqual(client.create_task(payload), "cgt-fixture-task")
        self.assertEqual(self.calls[-1], ("video.create", payload))
        self.result = {"items": [{"id": "cgt-fixture-task"}, "ignored"]}
        self.assertEqual(client.list_tasks(page_size=150), [{"id": "cgt-fixture-task"}])
        self.assertEqual(self.calls[-1], ("video.list", {"page_size": 100}))
        self.result = {"id": "cgt-fixture-task", "status": "succeeded", "content": {"video_url": "https://example.invalid/output.mp4"}}
        self.assertEqual(client.get_task("cgt-fixture-task"), self.result)
        self.assertEqual(self.calls[-1], ("video.get", {"task_id": "cgt-fixture-task"}))
        self.assertFalse(hasattr(client, "api_key"))
        self.assertFalse(hasattr(client, "session"))

    def test_image_generation_retains_prompt_references_and_options(self):
        self.result = {"model": "fixture-image", "data": [{"url": "https://example.invalid/scene.jpg", "size": "2K"}],
                       "usage": {"generated_images": 1}}
        result = PlatformVideoClient(self.transport).generate_image(
            prompt=" 保持空间，移除人物 ", image_sources=["https://example.invalid/frame1.jpg", "https://example.invalid/frame2.jpg"],
            model="fixture-image", size="2K", watermark=False)
        self.assertEqual(self.calls[0], ("image.generate", {
            "model": "fixture-image", "prompt": "保持空间，移除人物",
            "image": ["https://example.invalid/frame1.jpg", "https://example.invalid/frame2.jpg"],
            "size": "2K", "sequential_image_generation": "disabled", "stream": False,
            "response_format": "url", "watermark": False,
        }))
        self.assertEqual(result, {"url": "https://example.invalid/scene.jpg", "size": "2K", "model": "fixture-image", "usage": {"generated_images": 1}})

    def test_paid_create_error_is_not_retried_or_switched_to_byok(self):
        self.rpc.side_effect = ArkConnectionError("POST", "synthetic uncertain response", 1)
        with self.assertRaises(ArkConnectionError):
            PlatformVideoClient(self.transport).create_task({"model": "fixture"})
        self.rpc.assert_called_once_with("video.create", {"model": "fixture"})

    def test_task_recovery_keeps_original_fingerprint_matching_and_read_only_queries(self):
        self.result = {"items": [{"id": "cgt-new", "model": "fixture", "created_at": 100,
                                  "resolution": "480p", "ratio": "9:16", "duration": 6, "generate_audio": False}]}
        task_id = PlatformVideoClient(self.transport).recover_created_task(
            {"cgt-old"}, model="fixture", created_after=99, created_before=101, resolution="480p",
            ratio="9:16", duration=6, generate_audio=False, attempts=1)
        self.assertEqual(task_id, "cgt-new")
        self.assertEqual(self.calls, [("video.list", {"page_size": 50})])

    def test_asset_methods_keep_native_actions_and_project_scope(self):
        client = PlatformAssetsClient(self.transport, project_name="fixture-project")
        self.result = {"Result": {"Items": [{"Id": "group-fixture123"}]}}
        self.assertEqual(client.list_asset_groups(group_type="AIGC"), [{"Id": "group-fixture123"}])
        self.assertEqual(self.calls[-1][0], "assets.ListAssetGroups")
        self.assertEqual(self.calls[-1][1]["Filter"], {"GroupType": "AIGC"})
        self.result = {"Result": {"Id": "group-fixture123"}}
        self.assertEqual(client.create_asset_group(name="新角色", description="虚拟角色"), "group-fixture123")
        client.update_asset_group("group-fixture123", name="新名称")
        client.delete_asset_group("group-fixture123")
        self.result = {"Result": {"Items": [{"Id": "asset-fixture123"}]}}
        self.assertEqual(client.list_assets(group_type="AIGC", group_ids=["group-fixture123"], statuses=["Active"]),
                         [{"Id": "asset-fixture123"}])
        self.result = {"Result": {"Id": "asset-fixture123"}}
        self.assertEqual(client.create_asset(group_id="group-fixture123", url="https://media.example.invalid/person.png", name="角色"),
                         "asset-fixture123")
        self.assertEqual(client.get_asset("asset-fixture123"), {"Id": "asset-fixture123"})
        client.delete_asset("asset-fixture123")
        self.assertEqual([operation for operation, _ in self.calls], [
            "assets.ListAssetGroups", "assets.CreateAssetGroup", "assets.UpdateAssetGroup", "assets.DeleteAssetGroup",
            "assets.ListAssets", "assets.CreateAsset", "assets.GetAsset", "assets.DeleteAsset"])
        self.assertTrue(all(payload["ProjectName"] == "fixture-project" for _, payload in self.calls))
        creation = next(payload for operation, payload in self.calls if operation == "assets.CreateAsset")
        self.assertNotIn("compliance_confirmed", creation, "The adapter must not invent user consent")
        self.assertFalse(hasattr(client, "access_key"))
        self.assertFalse(hasattr(client, "secret_key"))

    def test_visual_validation_methods_keep_their_request_and_result_contract(self):
        client = PlatformAssetsClient(self.transport, project_name="fixture-project")
        self.result = {"Result": {"BytedToken": "synthetic-validation-id", "H5Link": "https://example.invalid/validation"}}
        self.assertEqual(client.create_visual_validate_session(callback_url="https://example.invalid/callback")["byted_token"],
                         "synthetic-validation-id")
        self.result = {"Result": {"GroupId": "group-fixture123", "Status": "Success"}}
        self.assertEqual(client.get_visual_validate_result(byted_token="synthetic-validation-id"),
                         {"group_id": "group-fixture123", "status": "Success"})
        self.assertEqual([operation for operation, _ in self.calls],
                         ["assets.CreateVisualValidateSession", "assets.GetVisualValidateResult"])

    def test_performance_analysis_reuses_original_prompts_and_normalization(self):
        self.result = {"output_text": json.dumps({"has_dialogue": False, "dialogue": [], "performance": [
            {"actor_slot": 1, "start": 0, "end": 1, "core_intent": "观察", "visible_evidence": "视线固定", "confidence": 0.9}]})}
        reference_session = Mock()
        reference_session.post.return_value = SimpleNamespace(status_code=200, json=lambda: self.result)
        reference = ArkPerformanceAnalyzer("synthetic-test-value", model="fixture-analysis", session=reference_session)
        expected = reference.analyze("https://example.invalid/shot.mp4", duration=1, max_people=1)
        result = PlatformPerformanceAnalyzer(self.transport, model="fixture-analysis").analyze(
            "https://example.invalid/shot.mp4", duration=1, max_people=1)
        self.assertEqual(result, expected)
        self.assertEqual(self.calls, [("analysis.create", reference_session.post.call_args.kwargs["json"])])
        self.assertNotIn("Authorization", self.calls[0][1])

    def test_cast_analysis_reuses_original_timeline_prompt_and_normalization(self):
        self.result = {"output_text": json.dumps({"characters": [{"character_id": 1, "description": "虚拟人物"}],
            "assignments": [{"shot_index": 1, "slot": 1, "character_id": 1, "confidence": 0.9}]})}
        reference_session = Mock()
        reference_session.post.return_value = SimpleNamespace(status_code=200, json=lambda: self.result)
        reference = ArkPerformanceAnalyzer("synthetic-test-value", model="fixture-analysis", session=reference_session)
        options = {"shot_slot_counts": {1: 1}, "shot_ranges": [{"index": 1, "start": 0, "end": 1}],
                   "max_characters": 2, "retry_instruction": "保留第一个人物"}
        expected = reference.analyze_cast_continuity("data:video/mp4;base64,AAAA", **options)
        result = PlatformPerformanceAnalyzer(self.transport, model="fixture-analysis").analyze_cast_continuity(
            "data:video/mp4;base64,AAAA", **options)
        self.assertEqual(result, expected)
        self.assertEqual(self.calls, [("analysis.create", reference_session.post.call_args.kwargs["json"])])

    @staticmethod
    def modules():
        def original_factory(*_args, **_kwargs):
            raise AssertionError("BYOK factory must not be used")
        web = SimpleNamespace(api_client=original_factory, ark_assets_client=original_factory,
                              ark_assets_credentials=original_factory, performance_analyzer=original_factory,
                              SeedanceVideoReferenceSource=lambda **values: SimpleNamespace(**values),
                              ArkCharacterUploadSource=lambda **values: SimpleNamespace(**values))
        core = SimpleNamespace()
        return web, core

    def test_all_three_project_contexts_share_factories_without_any_local_key_or_http_session(self):
        with patch.dict(os.environ, {}, clear=True), patch("requests.Session", side_effect=AssertionError("No worker HTTP session")):
            for project in ("wardrobe", "virtual", "real"):
                with self.subTest(project=project):
                    web, core = self.modules()
                    self.assertEqual(install_platform(web, core, self.transport, capabilities()), "platform")
                    self.assertIsInstance(web.api_client(), PlatformVideoClient)
                    self.assertIsInstance(web.ark_assets_client(), PlatformAssetsClient)
                    self.assertEqual(web.performance_analyzer().model, "fixture-analysis")
                    self.assertTrue(web.ark_assets_configured())
                    self.assertEqual(web.ark_assets_credentials(), ("", ""))
                    self.assertEqual(web.ark_assets_project_name(), "default")
                    self.assertIs(web.TempFileMediaStore, core.TempFileMediaStore)
                    self.assertIs(web.TosMediaStore, core.TosMediaStore)
                    self.assertFalse(web.TosMediaStore.configured())
                    with self.assertRaises(WorkflowError):
                        web.TosMediaStore()
        self.rpc.assert_not_called()
        self.upload.assert_not_called()

    def test_platform_media_source_preserves_url_metadata_and_never_implicitly_deletes_cloud_objects(self):
        web, core = self.modules()
        install_platform(web, core, self.transport, capabilities())
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic.mp4"
            source.write_bytes(b"synthetic-video-placeholder")
            video = web.prepare_seedance_stable_video_reference(source)
            image = web.prepare_ark_character_upload_source(source)
            store = web.TempFileMediaStore()
            uploaded = store.upload_video(source, expires_hours=1, attempts=5)
            store.delete(uploaded.object_key)
            self.assertEqual(video.url, uploaded.signed_url)
            self.assertEqual(image.url, uploaded.signed_url)
            self.assertEqual(video.channel, "platform")
            self.assertEqual(video.object_key, uploaded.object_key)
            self.assertFalse(hasattr(video, "tos"))
            self.assertFalse(hasattr(video, "temporary_store"))
        self.assertEqual(self.upload.call_count, 3)
        self.rpc.assert_not_called()

    def test_unready_platform_does_not_fall_back_to_keys_and_disabled_assets_stay_unavailable(self):
        for public_state in ({}, {"ready": False}, {"ready": "true"}):
            with self.subTest(capabilities=public_state):
                web, core = self.modules()
                install_platform(web, core, self.transport, public_state)
                for factory in (web.api_client, web.ark_assets_client, web.performance_analyzer, web.TempFileMediaStore):
                    with self.assertRaisesRegex(WorkflowError, "平台服务尚未配置就绪"):
                        factory()
                self.assertFalse(web.ark_assets_configured())
        web, core = self.modules()
        install_platform(web, core, self.transport, capabilities(capabilities={"video": True, "assets": False}))
        self.assertIsInstance(web.api_client(), PlatformVideoClient)
        self.assertFalse(web.ark_assets_configured())
        with self.assertRaises(WorkflowError):
            web.ark_assets_client()
        self.rpc.assert_not_called()

    def test_invalid_operation_or_response_cannot_fall_back_to_supplier_http(self):
        with self.assertRaises(WorkflowError):
            self.transport.rpc("arbitrary.proxy", {})
        with self.assertRaises(WorkflowError):
            PlatformVideoClient(self.transport)._request("POST", "/unexpected", json_body={})
        with self.assertRaises(WorkflowError):
            PlatformVideoClient(self.transport).get_task("../other")
        with self.assertRaises(WorkflowError):
            PlatformAssetsClient(self.transport).call("ArbitraryAction", {})
        self.rpc.assert_not_called()
        self.rpc.side_effect = None
        self.rpc.return_value = ["invalid"]
        with self.assertRaisesRegex(WorkflowError, "结果格式异常"):
            self.transport.rpc("video.list", {})

    def test_each_platform_service_requires_its_strict_public_capability_flag(self):
        for flag in (False, "true", 1, None):
            with self.subTest(flag=flag):
                web, core = self.modules()
                install_platform(web, core, self.transport, capabilities(capabilities={
                    "video": flag, "image": flag, "analysis": flag, "assets": flag, "media": flag,
                }))
                client = web.api_client()
                with self.assertRaises(WorkflowError):
                    client.create_task({"model": "fixture"})
                with self.assertRaises(WorkflowError):
                    client.generate_image(prompt="fixture", image_sources=["https://example.invalid/image.png"])
                for factory in (web.ark_assets_client, web.performance_analyzer, web.TempFileMediaStore):
                    with self.assertRaises(WorkflowError):
                        factory()
                self.assertFalse(web.ark_assets_configured())
                self.assertFalse(web.TempFileMediaStore.available())
        self.rpc.assert_not_called()
        self.upload.assert_not_called()

    def test_invalid_upload_response_or_missing_file_stops_before_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic.mp4"
            with self.assertRaises(WorkflowError):
                self.transport.upload(source)
            self.upload.assert_not_called()
            source.write_bytes(b"synthetic-video-placeholder")
            for result in ({}, {"object_key": "fixture", "signed_url": "http://example.invalid/video.mp4"},
                           {"object_key": "fixture", "signed_url": "https://user:password@example.invalid/video.mp4"}):
                with self.subTest(result=result):
                    self.upload.return_value = result
                    with self.assertRaisesRegex(WorkflowError, "有效素材地址"):
                        self.transport.upload(source)
        self.rpc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
