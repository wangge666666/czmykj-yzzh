import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np
import web_app
from wardrobe_dynamic import object_final_prompt
from wardrobe_object import upload_failure, video_digest
from workflow_core import ArkAPIError, ArkConnectionError, WorkflowError, build_motion_reference_payload, build_video_reference_seedance_payload


class ObjectWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in (("web_app.PROJECT_DIR", self.root), ("workflow_core.PROJECT_DIR", self.root), ("workflow_core.RUNS_DIR", self.root / "runs")):
            self.stack.enter_context(patch(name, value))
        self.stack.enter_context(patch.dict(web_app.JOBS, {}, clear=True))
        self.thread = self.stack.enter_context(patch("web_app.threading.Thread"))
        self.client = web_app.app.test_client()
        self.assets = self.stack.enter_context(patch("web_app.ark_assets_client"))
        self.assets.return_value.get_asset.side_effect = lambda aid: {"Id": aid, "Name": "素材名称", "AssetType": "Video" if "video" in aid else "Image", "Status": "Active", "URL": "https://example.com/preview.mp4"}

    def source(self, workflow):
        job = web_app.wardrobe_object.new_source(workflow)
        job.status = "succeeded"
        if workflow == "references":
            for name in ("reference.mp4", "white_model.mp4", "face_mosaic.mp4"):
                (job.run_dir / name).write_bytes(name.encode())
            job.white_model_path = job.run_dir / "white_model.mp4"
            job.mosaic_path = job.run_dir / "face_mosaic.mp4"
        else:
            job.cast_continuity["wardrobe_dynamic"]["video_asset"] = "asset://asset-video123456"
        web_app.wardrobe_object.save(job)
        return job

    @staticmethod
    def image():
        ok, content = cv2.imencode(".png", np.full((540, 960, 3), 120, np.uint8))
        assert ok
        return io.BytesIO(content.tobytes()), "reference.png"

    def generate(self, job, **extra):
        data = {"source_job_id": job.id, "mode": "dynamic_object", "request_id": "object-generation-0001", "paid_confirmed": "true", "prompt": "把右手的蓝杯子换成透明玻璃杯", **extra}
        response = self.client.post("/api/wardrobe-swap/generate", data=data)
        return response.status_code, response.get_json()

    def test_references_bind_character_clothing_and_optional_object_in_order(self):
        for with_object in (False, True):
            with self.subTest(with_object=with_object):
                source = self.source("references")
                extra = {"person_asset": "asset://asset-person123456", "clothing_image": self.image(), "white_reviewed": "true"}
                if with_object:
                    extra.update(replacement_image=self.image(), special_object="true", prompt="换成@图片3的玻璃杯")
                code, body = self.generate(source, **extra)
                self.assertEqual(code, 202, body)
                kwargs = self.thread.call_args.kwargs["kwargs"]
                images = kwargs["reference_images"]
                self.assertEqual(len(images), 3 if with_object else 2)
                self.assertEqual(images[0], "asset://asset-person123456")
                self.assertIn("clothing", images[1])
                self.assertEqual(kwargs["depth_path"], source.white_model_path)
                self.assertFalse(kwargs["depth_reference"])
                self.assertIn("还原", kwargs["options"]["prompt"])
                self.assertEqual("@图片3" in kwargs["options"]["prompt"], with_object)
                payload = build_motion_reference_payload(prompt=kwargs["options"]["prompt"], image_sources=images, video_reference="https://example.com/white.mp4")
                self.assertEqual(payload["content"][1]["image_url"]["url"], images[0])

    def test_library_uses_video_asset_with_zero_or_one_image_and_no_local_video_upload(self):
        for with_object in (False, True):
            source = self.source("library")
            extra = {"replacement_image": self.image(), "special_object": "true", "prompt": "换成@图片1的杯子"} if with_object else {}
            code, body = self.generate(source, **extra)
            self.assertEqual(code, 202, body)
            kwargs = self.thread.call_args.kwargs["kwargs"]
            self.assertIsNone(kwargs["depth_path"])
            self.assertEqual(kwargs["depth_reference"], "asset://asset-video123456")
            self.assertEqual(kwargs["video_only"], not with_object)
            self.assertEqual(len(kwargs["reference_images"] or []), int(with_object))
            if not with_object:
                options = kwargs["options"]
                payload = build_video_reference_seedance_payload(prompt=options["prompt"], video_reference=kwargs["depth_reference"], model=options["model"], ratio="adaptive", duration=-1)
                self.assertEqual([item["type"] for item in payload["content"]], ["text", "video_url"])
                self.assertEqual(payload["content"][1]["video_url"]["url"], "asset://asset-video123456")

    def test_missing_or_wrong_material_blocks_before_paid_submission(self):
        source = self.source("references")
        cases = [{}, {"white_reviewed": "true"},
                 {"special_object": "true"}, {"prompt": "参考@图片3"}, {"paid_confirmed": "false"}]
        for extra in cases:
            code, body = self.generate(source, **extra); self.assertEqual(code, 400, body)
        self.thread.assert_not_called()
        source = self.source("library")
        for extra in ({"prompt": "参考@图片1"}, {"prompt": ""}, {"special_object": "true"}, {"person_asset": "asset://asset-person123456"}):
            code, body = self.generate(source, **extra); self.assertEqual(code, 400, body)
        self.thread.assert_not_called()
        self.assets.return_value.get_asset.return_value = {"AssetType": "Video", "Status": "Processing"}
        self.assets.return_value.get_asset.side_effect = None
        code, _ = self.generate(source); self.assertEqual(code, 400)

    def test_duplicate_generate_is_idempotent_and_busy_attempt_blocks_new_request(self):
        source = self.source("library")
        code, first = self.generate(source); self.assertEqual(code, 202)
        code, second = self.generate(source); self.assertEqual(code, 202)
        self.assertEqual(first["id"], second["id"]); self.assertEqual(self.thread.call_count, 1)
        code, _ = self.generate(source, request_id="object-generation-0002"); self.assertEqual(code, 400)

    def test_restore_library_source_without_local_video_and_isolate_branches(self):
        reference = self.source("references"); library = self.source("library")
        web_app.JOBS.clear()
        restored = web_app.restore_wardrobe_swap_job(library.id)
        self.assertEqual(restored.cast_continuity["wardrobe_dynamic"]["video_asset"], "asset://asset-video123456")
        for workflow, expected in (("references", reference.id), ("library", library.id)):
            response = self.client.get("/api/wardrobe-object/latest?workflow=" + workflow)
            self.assertEqual(response.get_json()["id"], expected)
        legacy = self.source("references"); legacy.cast_continuity["wardrobe_dynamic"].pop("workflow_version")
        web_app.wardrobe_object.save(legacy)
        code, _ = self.generate(legacy); self.assertEqual(code, 400)
        self.assertEqual(self.client.get("/api/wardrobe-object/latest?workflow=references").get_json()["id"], reference.id)

    def test_object_mosaic_masks_all_faces_and_white_prompt_only_whitens_protagonist(self):
        source = self.source("references")
        response = self.client.post("/api/wardrobe-swap/mosaic", data={"mode": "dynamic_object", "source_job_id": source.id, "face_score_threshold": ".40"})
        self.assertEqual(response.status_code, 202, response.get_json())
        job = web_app.JOBS[response.get_json()["id"]]
        with patch("web_app.normalize_wardrobe_source_duration", return_value=job.run_dir / "reference.mp4"), patch("web_app.run_wardrobe_face_mosaic") as mask:
            self.thread.call_args.kwargs["target"]()
            self.assertEqual(mask.call_args.kwargs["score_threshold"], .4)
        self.assertEqual(job.cast_continuity["wardrobe_dynamic"]["object_workflow"], "references")
        response = self.client.post("/api/wardrobe-swap/white-model", data={"source_job_id": source.id, "request_id": "object-white-model-0001", "paid_confirmed": "true", "mosaic_reviewed": "true"})
        self.assertEqual(response.status_code, 202, response.get_json())
        with patch("web_app.run_wardrobe_white_model") as run:
            self.thread.call_args.kwargs["target"]()
            prompt = run.call_args.kwargs["prompt_override"]
            self.assertIn("只将主要人物转为白模", prompt)
            self.assertIn("交互道具保持原外观与质感", prompt)
            self.assertNotIn("物品白模", prompt)

    def test_import_white_is_local_and_rejects_invalid_video(self):
        with patch("wardrobe_object.validate_seedance_reference_video", return_value=Mock(duration=8)):
            response = self.client.post("/api/wardrobe-object/import-white", data={"white_video": (io.BytesIO(b"video"), "white.mp4")})
        self.assertEqual(response.status_code, 201, response.get_json())
        body = response.get_json(); self.assertTrue(body["has_white_model"]); self.assertFalse(body["has_mosaic"])
        self.thread.assert_not_called(); self.assets.assert_not_called()
        response = self.client.post("/api/wardrobe-object/import-white", data={"white_video": (io.BytesIO(b"bad"), "bad.mp4")})
        self.assertEqual(response.status_code, 400)

    def test_video_upload_ingests_video_and_receipt_prevents_duplicate_ingestion(self):
        client = self.assets.return_value
        client.create_asset.return_value = "asset-video123456"
        groups = {"groups": [{"id": "group-123456789"}]}
        with patch.object(web_app.wardrobe_object, "library_records", return_value=groups), patch("wardrobe_object.validate_seedance_reference_video", return_value=Mock(duration=8)):
            data = {"request_id": "upload-video-object-0001", "group_id": "group-123456789", "name": "原片", "reference_video": (io.BytesIO(b"original-file"), "source.mp4")}
            response = self.client.post("/api/wardrobe-object/upload-video", data=data)
        self.assertEqual(response.status_code, 202, response.get_json())
        upload = Mock(url="https://example.com/video.mp4")
        with patch("web_app.prepare_seedance_stable_video_reference", return_value=upload), patch("web_app._cleanup_ark_asset_upload") as cleanup:
            self.thread.call_args.kwargs["target"]()
            self.assertEqual(client.create_asset.call_args.kwargs["asset_type"], "Video")
            self.assertEqual(cleanup.call_args.args[2].read_bytes(), b"original-file")
        response = self.client.post("/api/wardrobe-object/upload-video", data={"request_id": "upload-video-object-0001"})
        self.assertEqual(response.status_code, 202); self.assertEqual(response.get_json()["uri"], "asset://asset-video123456")
        self.assertEqual(client.create_asset.call_count, 1); self.assertEqual(self.thread.call_count, 1)

    def test_video_library_filters_images_but_retains_pending_video_and_group_type(self):
        client = self.assets.return_value
        client.list_asset_groups.side_effect = lambda **kw: [{"Id": "group-123456789", "Name": kw["group_type"]}]
        client.list_assets.return_value = [{"Id": "asset-person123456", "AssetType": "Image"}, {"Id": "asset-video123456", "AssetType": "Video", "Status": "Processing"}]
        with patch("web_app.ark_assets_configured", return_value=True):
            body = self.client.get("/api/wardrobe-object/video-library").get_json()
        self.assertEqual({group["group_type"] for group in body["groups"]}, {"AIGC", "LivenessFace"})
        self.assertTrue(all(asset["id"] == "asset-video123456" for asset in body["assets"]))
        self.assertTrue(all(asset["status"] == "Processing" for asset in body["assets"]))

    def test_interrupted_ingestion_is_reported_without_resubmitting(self):
        key = "object-video-interrupted-0001"
        path = web_app.wardrobe_object.receipt_path(key)
        web_app.save_shot_manifest(path, {"request_id": key, "status": "submitting"})
        response = self.client.get("/api/wardrobe-object/upload-status/" + key)
        self.assertEqual(response.get_json()["status"], "uncertain")
        self.thread.assert_not_called(); self.assets.assert_not_called()

    def test_quota_and_rejections_are_definitive_but_network_loss_is_uncertain(self):
        cases = [(ArkAPIError(429, "shared pool is full", "QuotaSharedPoolExceeded"), "quota_full"),
                 (ArkAPIError(400, "invalid video", "InvalidParameter"), "failed"),
                 (ArkAPIError(429, "too fast", "RateLimitExceeded"), "failed"),
                 (ArkAPIError(503, "unavailable"), "uncertain"),
                 (ArkConnectionError("POST", "timeout", 1), "uncertain")]
        for error, expected in cases:
            self.assertEqual(upload_failure(error, submitting=True)["status"], expected)

    def test_legacy_quota_receipt_is_repaired_on_status_and_latest(self):
        key = "object-video-quota-000001"
        path = web_app.wardrobe_object.receipt_path(key)
        web_app.save_shot_manifest(path, {"request_id": key, "status": "uncertain", "error": "HTTP 429 [QuotaSharedPoolExceeded] shared pool is full"})
        response = self.client.get("/api/wardrobe-object/latest-upload")
        self.assertEqual(response.status_code, 200)
        body = response.get_json(); self.assertEqual(body["status"], "quota_full")
        self.assertIn("本次新增入库被拒绝", body["error"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "quota_full")
        self.thread.assert_not_called(); self.assets.assert_not_called()

    def test_duplicate_content_reuses_asset_without_upload_or_create(self):
        records = {"groups": [{"id": "group-123456789"}], "assets": []}
        with patch.object(web_app.wardrobe_object, "library_records", return_value=records), patch("wardrobe_object.validate_seedance_reference_video", return_value=Mock(duration=8)):
            response = self.client.post("/api/wardrobe-object/upload-video", data={"request_id": "duplicate-video-object-01", "group_id": "group-123456789", "name": "不同名称", "reference_video": (io.BytesIO(b"same-video"), "other.mp4")})
        self.assertEqual(response.status_code, 202)
        with patch.object(web_app.wardrobe_object, "existing_video", return_value={"id": "asset-video123456", "name": "库中原片"}), patch("web_app.prepare_seedance_stable_video_reference") as upload:
            self.thread.call_args.kwargs["target"]()
            upload.assert_not_called(); self.assets.return_value.create_asset.assert_not_called()
        body = self.client.get("/api/wardrobe-object/upload-status/duplicate-video-object-01").get_json()
        self.assertEqual(body["status"], "reused"); self.assertTrue(body["deduplicated"])
        self.assertEqual(body["asset_status"], "Active")
        self.assertEqual(body["uri"], "asset://asset-video123456")

    def test_remote_dedup_requires_matching_bytes_not_name_or_length(self):
        path = self.root / "video.mp4"; path.write_bytes(b"same-video")
        assets = [{"id": "asset-video123456", "uri": "asset://asset-video123456", "name": "different-name", "status": "Active"}]
        response = Mock(status_code=200, headers={"Content-Length": str(path.stat().st_size)})
        for content, matches in ((b"same-video", True), (b"diff-video", False), (b"same-video-extra", False)):
            response.iter_content.return_value = [content]
            manager = Mock(); manager.__enter__ = Mock(return_value=response); manager.__exit__ = Mock(return_value=False)
            with patch("wardrobe_object.requests.get", return_value=manager):
                result = web_app.wardrobe_object.existing_video(path, video_digest(path), assets)
            self.assertEqual(result is not None, matches)

    def test_known_hash_reuse_verifies_asset_is_still_active(self):
        path = self.root / "video.mp4"; path.write_bytes(b"same-video")
        receipt = web_app.wardrobe_object.receipt_path("known-video-hash-000001")
        web_app.save_shot_manifest(receipt, {"sha256": video_digest(path), "asset_id": "asset-video123456"})
        with patch("wardrobe_object.requests.get") as download:
            result = web_app.wardrobe_object.existing_video(path, video_digest(path), [])
            self.assertEqual(result["id"], "asset-video123456"); download.assert_not_called()
            self.assets.return_value.get_asset.side_effect = lambda aid: {"Status": "Failed", "AssetType": "Video"}
            self.assertIsNone(web_app.wardrobe_object.existing_video(path, video_digest(path), []))

    def test_prompt_normalizes_only_bound_mentions(self):
        prompt = object_final_prompt("references", "将杯子替换成 @ 图片 3 的水瓶", True)
        self.assertIn("@图片3", prompt)
        for custom in ("@图片3", "@视频2", "@陌生素材"):
            with self.assertRaises(WorkflowError): object_final_prompt("references", custom, False)


if __name__ == "__main__": unittest.main()
