from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import cv2
import numpy as np

from workflow_core import (
    ArkAPIError,
    ArkAssetsClient,
    ArkConnectionError,
    ArkVideoClient,
    DEFAULT_SEEDANCE_25_MODEL,
    DEFAULT_SEEDREAM_MODEL,
    TempFileMediaStore,
    TemporaryFileServer,
    WorkflowError,
    build_multi_seedance_payload,
    build_scene_seedance_payload,
    build_seedance_payload,
    build_video_reference_seedance_payload,
    download_file,
    extract_scene_reference_frames,
    image_to_data_url,
    load_env_file,
    validate_video_reference,
)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict,
        reason: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.reason = reason
        self.ok = 200 <= status_code < 300
        self.headers = headers or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class WorkflowCoreTests(unittest.TestCase):
    def test_temporary_file_server_exposes_only_the_tokenized_image(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "person.png"
            image.write_bytes(b"test-character-image")
            server = TemporaryFileServer(image, "secret-token", public_name="person.png")
            try:
                url = server.start()
                head = requests.head(url, timeout=5)
                body = requests.get(url, timeout=5)
                missing = requests.get(f"{server.local_origin}/person.png", timeout=5)
                self.assertEqual(head.status_code, 200)
                self.assertEqual(head.headers["Content-Type"], "image/png")
                self.assertEqual(body.content, b"test-character-image")
                self.assertEqual(missing.status_code, 404)
            finally:
                server.close()

    def test_ark_assets_client_signs_lists_creates_and_deletes_exact_asset(self) -> None:
        session = Mock()
        session.post.side_effect = [
            FakeResponse(200, {"Result": {"Items": [{"Id": "group-202608190001-abc"}]}}),
            FakeResponse(200, {"Result": {"Id": "asset-202608190001-abc"}}),
            FakeResponse(200, {"Result": {"Id": "asset-202608190001-abc"}}),
        ]
        client = ArkAssetsClient("ak-test", "sk-test", session=session)
        groups = client.list_asset_groups(group_type="LivenessFace")
        asset_id = client.create_asset(
            group_id="group-202608190001-abc",
            url="https://example.com/person.png",
            name="person",
        )
        client.delete_asset(asset_id)
        self.assertEqual(groups[0]["Id"], "group-202608190001-abc")
        self.assertEqual(asset_id, "asset-202608190001-abc")
        for call in session.post.call_args_list:
            self.assertIn("HMAC-SHA256 Credential=ak-test/", call.kwargs["headers"]["Authorization"])
        self.assertIn("Action=DeleteAsset", session.post.call_args_list[-1].args[0])
        self.assertIn(b'"Id":"asset-202608190001-abc"', session.post.call_args_list[-1].kwargs["data"])

    def test_ark_assets_client_creates_aigc_virtual_portrait_group(self) -> None:
        session = Mock()
        session.post.return_value = FakeResponse(
            200,
            {"Result": {"Id": "group-virtualperson123"}},
        )
        client = ArkAssetsClient(
            "ak-test",
            "sk-test",
            project_name="default",
            session=session,
        )
        group_id = client.create_asset_group(
            name="虚拟女主角",
            description="真实人物复刻重绘使用",
            group_type="AIGC",
        )
        self.assertEqual(group_id, "group-virtualperson123")
        call = session.post.call_args
        self.assertIn("Action=CreateAssetGroup", call.args[0])
        self.assertIn(b'"GroupType":"AIGC"', call.kwargs["data"])
        self.assertIn(b'"ProjectName":"default"', call.kwargs["data"])

    def test_ark_assets_client_creates_and_resolves_real_person_validation_group(self) -> None:
        session = Mock()
        session.post.side_effect = [
            FakeResponse(200, {"Result": {
                "BytedToken": "token-real-person-123",
                "H5Link": "https://example.com/validate/123",
                "CallbackURL": "http://127.0.0.1:7860/callback",
            }}),
            FakeResponse(200, {"Result": {
                "GroupId": "group-realperson123",
                "Status": "Success",
            }}),
            FakeResponse(200, {"Result": {}}),
            FakeResponse(200, {"Result": {}}),
        ]
        client = ArkAssetsClient(
            "ak-test",
            "sk-test",
            project_name="real-project",
            session=session,
        )
        validation = client.create_visual_validate_session(
            callback_url="http://127.0.0.1:7860/callback",
        )
        result = client.get_visual_validate_result(byted_token=validation["byted_token"])
        client.update_asset_group(
            result["group_id"],
            name="女主角",
            description="已获授权真人角色",
        )
        client.delete_asset_group(result["group_id"])
        self.assertEqual(validation["h5_link"], "https://example.com/validate/123")
        self.assertEqual(result["group_id"], "group-realperson123")
        actions = [call.args[0] for call in session.post.call_args_list]
        self.assertIn("Action=CreateVisualValidateSession", actions[0])
        self.assertIn("Action=GetVisualValidateResult", actions[1])
        self.assertIn("Action=UpdateAssetGroup", actions[2])
        self.assertIn("Action=DeleteAssetGroup", actions[3])
        for call in session.post.call_args_list:
            self.assertIn(b'"ProjectName":"real-project"', call.kwargs["data"])

    def test_load_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# comment\nTEST_WORKFLOW_VALUE='abc'\n", encoding="utf-8")
            old_value = os.environ.pop("TEST_WORKFLOW_VALUE", None)
            try:
                loaded = load_env_file(path)
                self.assertEqual(loaded["TEST_WORKFLOW_VALUE"], "abc")
                self.assertEqual(os.environ["TEST_WORKFLOW_VALUE"], "abc")
            finally:
                if old_value is None:
                    os.environ.pop("TEST_WORKFLOW_VALUE", None)
                else:
                    os.environ["TEST_WORKFLOW_VALUE"] = old_value

    def test_image_to_data_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tiny.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n")
            result = image_to_data_url(path)
            self.assertTrue(result.startswith("data:image/png;base64,"))

    def test_scene_seedance_payload_uses_only_scene_and_depth_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scene = Path(tmp) / "scene.png"
            scene.write_bytes(b"\x89PNG\r\n\x1a\n")
            payload = build_scene_seedance_payload(
                prompt="重绘无人场景",
                scene_source=str(scene),
                depth_video_reference="https://example.com/depth.mp4",
                duration=5,
            )
        self.assertEqual([item["type"] for item in payload["content"]], ["text", "image_url", "video_url"])
        self.assertEqual(payload["content"][1]["role"], "reference_image")
        self.assertEqual(payload["content"][2]["role"], "reference_video")

    def test_video_only_seedance_payload_has_no_image_reference(self) -> None:
        payload = build_video_reference_seedance_payload(
            prompt="将人物转换为无服装、无发型的中性人体白模",
            video_reference="https://example.com/mosaic.mp4",
            duration=6,
        )
        self.assertEqual([item["type"] for item in payload["content"]], ["text", "video_url"])
        self.assertEqual(payload["content"][1]["role"], "reference_video")
        self.assertEqual(payload["content"][1]["video_url"]["url"], "https://example.com/mosaic.mp4")
        self.assertFalse(payload["generate_audio"])

    def test_seedance_25_video_editing_payload_accepts_required_auto_controls(self) -> None:
        payload = build_video_reference_seedance_payload(
            prompt="按照参考视频进行重绘",
            video_reference="https://example.com/white-model.mp4",
            model=DEFAULT_SEEDANCE_25_MODEL,
            ratio="adaptive",
            duration=-1,
        )
        self.assertEqual(payload["ratio"], "adaptive")
        self.assertEqual(payload["duration"], -1)

        multi_payload = build_multi_seedance_payload(
            prompt="按照白模视频替换人物并重绘",
            character_sources=[
                (
                    "asset://asset-person-one",
                    "https://example.com/sketch.png",
                    "https://example.com/clothing.png",
                )
            ],
            scene_source="https://example.com/scene.png",
            depth_video_reference="https://example.com/white-model.mp4",
            model=DEFAULT_SEEDANCE_25_MODEL,
            ratio="adaptive",
            duration=-1,
        )
        self.assertEqual(multi_payload["ratio"], "adaptive")
        self.assertEqual(multi_payload["duration"], -1)

        with self.assertRaisesRegex(WorkflowError, "必须使用 adaptive"):
            build_video_reference_seedance_payload(
                prompt="按照参考视频进行重绘",
                video_reference="https://example.com/white-model.mp4",
                model=DEFAULT_SEEDANCE_25_MODEL,
                ratio="16:9",
                duration=-1,
            )

        with self.assertRaisesRegex(WorkflowError, "只有 Seedance 2.5"):
            build_video_reference_seedance_payload(
                prompt="按照参考视频进行重绘",
                video_reference="https://example.com/white-model.mp4",
                ratio="adaptive",
                duration=-1,
            )

    def test_scene_frames_save_under_unicode_windows_path(self) -> None:
        capture = Mock()
        capture.isOpened.return_value = True
        capture.get.side_effect = lambda prop: 25.0 if prop == cv2.CAP_PROP_FPS else 100
        capture.read.return_value = (True, np.zeros((48, 64, 3), dtype=np.uint8))
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "中文场景目录"
            with patch("workflow_core.cv2.VideoCapture", return_value=capture):
                frames = extract_scene_reference_frames("reference.mp4", output_dir)
            self.assertEqual(len(frames), 3)
            self.assertTrue(all(path.is_file() and path.stat().st_size > 0 for path in frames))
        capture.release.assert_called_once()

    def test_payload_keeps_expected_asset_order(self) -> None:
        payload = build_seedance_payload(
            prompt="参考@视频 1，人物用@图片 1。",
            person_source="asset://person-1",
            clothing_source="https://example.com/clothes.png",
            scene_source="https://example.com/scene.png",
            depth_video_reference="https://example.com/depth.mp4",
            duration=8,
        )
        self.assertEqual(payload["content"][0]["type"], "text")
        self.assertEqual(payload["content"][1]["image_url"]["url"], "asset://person-1")
        self.assertEqual(payload["content"][2]["image_url"]["url"], "https://example.com/clothes.png")
        self.assertEqual(payload["content"][3]["image_url"]["url"], "https://example.com/scene.png")
        self.assertEqual(payload["content"][4]["video_url"]["url"], "https://example.com/depth.mp4")
        self.assertEqual(payload["content"][4]["role"], "reference_video")
        self.assertFalse(payload["generate_audio"])

    def test_multi_payload_keeps_actor_pairs_and_scene_order(self) -> None:
        characters = [
            (f"asset://person-{index}", f"https://example.com/clothing-{index}.png")
            for index in range(1, 5)
        ]
        payload = build_multi_seedance_payload(
            prompt="参考@视频1并分别替换四位人物。",
            character_sources=characters,
            scene_source="https://example.com/scene.png",
            depth_video_reference="https://example.com/depth.mp4",
            duration=9,
        )
        images = [item for item in payload["content"] if item["type"] == "image_url"]
        videos = [item for item in payload["content"] if item["type"] == "video_url"]
        self.assertEqual(len(images), 9)
        self.assertEqual(images[0]["image_url"]["url"], "asset://person-1")
        self.assertEqual(images[1]["image_url"]["url"], "https://example.com/clothing-1.png")
        self.assertEqual(images[-1]["image_url"]["url"], "https://example.com/scene.png")
        self.assertEqual(videos[0]["video_url"]["url"], "https://example.com/depth.mp4")

    def test_multi_payload_rejects_more_than_four_people(self) -> None:
        with self.assertRaises(WorkflowError):
            build_multi_seedance_payload(
                prompt="test",
                character_sources=[("asset://person", "https://example.com/clothes.png")] * 5,
                scene_source="https://example.com/scene.png",
                depth_video_reference="https://example.com/depth.mp4",
            )

    def test_real_person_payload_accepts_one_or_two_triple_reference_actors(self) -> None:
        with patch("workflow_core.image_to_data_url", side_effect=lambda value: f"data:{value}"), patch(
            "workflow_core.validate_video_reference", return_value="https://example.com/video.mp4"
        ), patch("workflow_core.validate_image_payload_size"):
            payload = build_multi_seedance_payload(
                prompt="真人双参考",
                character_sources=[("masked-1", "sketch-1", "clothing-1")],
                scene_source="scene",
                depth_video_reference="https://example.com/video.mp4",
                duration=4,
            )
        urls = [item["image_url"]["url"] for item in payload["content"] if item["type"] == "image_url"]
        self.assertEqual(urls, ["data:masked-1", "data:sketch-1", "data:clothing-1", "data:scene"])

    def test_real_person_payload_can_embed_scene_in_control_video(self) -> None:
        with patch("workflow_core.image_to_data_url", side_effect=lambda value: f"data:{value}"), patch(
            "workflow_core.validate_video_reference", return_value="https://example.com/control.mp4"
        ), patch("workflow_core.validate_image_payload_size"):
            payload = build_multi_seedance_payload(
                prompt="真人时空控制",
                character_sources=[("masked-1", "sketch-1", "clothing-1")],
                scene_source="",
                depth_video_reference="https://example.com/control.mp4",
                duration=4,
                include_scene_reference=False,
            )
        images = [item["image_url"]["url"] for item in payload["content"] if item["type"] == "image_url"]
        self.assertEqual(images, ["data:masked-1", "data:sketch-1", "data:clothing-1"])
        self.assertEqual(payload["content"][-1]["video_url"]["url"], "https://example.com/control.mp4")

    def test_invalid_local_video_reference_is_rejected(self) -> None:
        with self.assertRaises(WorkflowError):
            validate_video_reference(r"C:\private\depth.mp4")

    def test_free_temp_store_upload_verifies_and_deletes_direct_video(self) -> None:
        session = Mock()
        session.post.return_value = FakeResponse(
            200,
            {"files": [{"id": "temporary123", "size": 4}]},
        )
        session.head.return_value = FakeResponse(
            200,
            {},
            headers={"Content-Type": "video/mp4", "Content-Length": "4"},
        )
        session.delete.return_value = FakeResponse(200, {"success": True})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "depth.mp4"
            path.write_bytes(b"test")
            store = TempFileMediaStore(session=session)
            uploaded = store.upload_video(path, attempts=1)
            self.assertEqual(uploaded.object_key, "temporary123")
            self.assertEqual(
                uploaded.signed_url,
                "https://tempfile.org/temporary123/download",
            )
            store.delete(uploaded.object_key)
        session.head.assert_called_once()
        session.delete.assert_called_once()

    @patch("workflow_core.time.sleep")
    @patch("workflow_core.requests.get")
    def test_download_file_resumes_after_connection_reset(
        self,
        mock_get: Mock,
        _mock_sleep: Mock,
    ) -> None:
        class StreamingResponse:
            def __init__(self, status_code: int, headers: dict[str, str], chunks: list[object]) -> None:
                self.status_code = status_code
                self.headers = headers
                self.chunks = chunks

            def __enter__(self) -> "StreamingResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def raise_for_status(self) -> None:
                if self.status_code >= 400:
                    raise requests.HTTPError(f"HTTP {self.status_code}")

            def iter_content(self, chunk_size: int) -> object:
                del chunk_size
                for item in self.chunks:
                    if isinstance(item, Exception):
                        raise item
                    yield item

        mock_get.side_effect = [
            StreamingResponse(
                200,
                {"Content-Length": "6"},
                [b"abc", requests.ConnectionError("reset")],
            ),
            StreamingResponse(
                206,
                {"Content-Length": "3", "Content-Range": "bytes 3-5/6"},
                [b"def"],
            ),
        ]
        retries: list[tuple[int, int, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result.mp4"
            result = download_file(
                "https://example.com/result.mp4",
                output,
                attempts=2,
                on_retry=lambda attempt, total, error: retries.append((attempt, total, error)),
            )
            self.assertEqual(result.read_bytes(), b"abcdef")
            self.assertFalse(output.with_suffix(".mp4.part").exists())
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(mock_get.call_args_list[1].kwargs["headers"], {"Range": "bytes=3-"})
        self.assertEqual(retries[0][:2], (2, 2))

    @patch("workflow_core.MAX_FREE_UPLOAD_BYTES", 3)
    def test_free_temp_store_rejects_oversized_video_before_upload(self) -> None:
        session = Mock()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "depth.mp4"
            path.write_bytes(b"test")
            with self.assertRaises(WorkflowError):
                TempFileMediaStore(session=session).upload_video(path, attempts=1)
        session.post.assert_not_called()

    def test_create_task_returns_id(self) -> None:
        session = Mock()
        session.request.return_value = FakeResponse(200, {"id": "cgt-test"})
        client = ArkVideoClient("secret", session=session)
        self.assertEqual(client.create_task({"model": "x", "content": []}), "cgt-test")
        headers = session.request.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer secret")

    def test_seedream_generation_keeps_reference_order_and_options(self) -> None:
        session = Mock()
        session.request.return_value = FakeResponse(
            200,
            {
                "model": DEFAULT_SEEDREAM_MODEL,
                "data": [{"url": "https://example.com/scene.jpg", "size": "2K"}],
                "usage": {"generated_images": 1},
            },
        )
        client = ArkVideoClient("secret", session=session)
        result = client.generate_image(
            prompt="删除人物并恢复原场景",
            image_sources=[
                "https://example.com/frame-1.jpg",
                "https://example.com/frame-2.jpg",
                "https://example.com/frame-3.jpg",
            ],
        )

        self.assertEqual(result["url"], "https://example.com/scene.jpg")
        call = session.request.call_args
        self.assertTrue(call.args[1].endswith("/images/generations"))
        payload = call.kwargs["json"]
        self.assertEqual(
            payload["image"],
            [
                "https://example.com/frame-1.jpg",
                "https://example.com/frame-2.jpg",
                "https://example.com/frame-3.jpg",
            ],
        )
        self.assertEqual(payload["model"], DEFAULT_SEEDREAM_MODEL)
        self.assertEqual(payload["sequential_image_generation"], "disabled")
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["watermark"])

    def test_api_error_is_user_facing(self) -> None:
        session = Mock()
        session.request.return_value = FakeResponse(
            401,
            {"error": {"code": "AuthenticationError", "message": "invalid api key"}},
            "Unauthorized",
        )
        client = ArkVideoClient("secret", session=session)
        with self.assertRaises(ArkAPIError) as caught:
            client.get_task("cgt-test")
        self.assertEqual(caught.exception.status_code, 401)
        self.assertIn("invalid api key", str(caught.exception))

    def test_real_person_privacy_error_is_translated_for_every_project(self) -> None:
        session = Mock()
        session.request.return_value = FakeResponse(
            400,
            {
                "error": {
                    "code": "InputImageSensitiveContentDetected.PrivacyInformation",
                    "message": "The input image content[1] may contain real person.",
                }
            },
            "Bad Request",
        )
        client = ArkVideoClient("secret", session=session)
        with self.assertRaises(ArkAPIError) as caught:
            client.generate_image(prompt="test", image_sources=["https://example.com/person.jpg"])
        message = str(caught.exception)
        self.assertIn("真人/隐私安全审核拒绝", message)
        self.assertIn("asset://", message)
        self.assertNotIn("content[1]", message)

    @patch("workflow_core.time.sleep")
    def test_read_only_request_retries_transient_tls_failure(self, _mock_sleep: Mock) -> None:
        session = Mock()
        session.request.side_effect = [
            requests.exceptions.SSLError("temporary TLS EOF"),
            FakeResponse(200, {"items": []}),
        ]
        client = ArkVideoClient("secret", session=session)
        self.assertEqual(client.list_tasks(), [])
        self.assertEqual(session.request.call_count, 2)

    def test_paid_create_request_is_never_blindly_retried(self) -> None:
        session = Mock()
        session.request.side_effect = requests.exceptions.SSLError("lost response")
        client = ArkVideoClient("secret", session=session)
        with self.assertRaises(ArkConnectionError) as caught:
            client.create_task({"model": "x", "content": []})
        self.assertEqual(caught.exception.method, "POST")
        self.assertEqual(session.request.call_count, 1)

    @patch("workflow_core.time.sleep")
    def test_ambiguous_create_can_recover_one_matching_new_task(self, _mock_sleep: Mock) -> None:
        client = ArkVideoClient("secret", session=Mock())
        client.list_tasks = Mock(
            return_value=[
                {"id": "old", "model": "model-a", "created_at": 100},
                {"id": "new", "model": "model-a", "created_at": 205},
            ]
        )
        recovered = client.recover_created_task(
            {"old"}, model="model-a", created_after=200, attempts=1
        )
        self.assertEqual(recovered, "new")

    @patch("workflow_core.time.sleep")
    def test_ambiguous_create_uses_full_submission_fingerprint(self, _mock_sleep: Mock) -> None:
        client = ArkVideoClient("secret", session=Mock())
        client.list_tasks = Mock(
            return_value=[
                {
                    "id": "matching",
                    "model": "model-a",
                    "created_at": 205,
                    "resolution": "720p",
                    "ratio": "9:16",
                    "duration": 5,
                    "generate_audio": False,
                },
                {
                    "id": "other-user-task",
                    "model": "model-a",
                    "created_at": 206,
                    "resolution": "720p",
                    "ratio": "16:9",
                    "duration": 7,
                    "generate_audio": True,
                },
            ]
        )
        recovered = client.recover_created_task(
            set(),
            model="model-a",
            created_after=200,
            created_before=210,
            resolution="720p",
            ratio="9:16",
            duration=5,
            generate_audio=False,
            attempts=1,
        )
        self.assertEqual(recovered, "matching")


if __name__ == "__main__":
    unittest.main()
