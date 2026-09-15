"""Background replacement: library original or green character proxy."""
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
from wardrobe_dynamic import DYNAMIC_SCENE_WHITE_PROMPT, scene_final_prompt
from workflow_core import WorkflowError, build_motion_reference_payload, build_video_reference_seedance_payload


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in (("web_app.PROJECT_DIR", self.root), ("workflow_core.PROJECT_DIR", self.root), ("workflow_core.RUNS_DIR", self.root / "runs")):
            self.stack.enter_context(patch(name, value))
        self.stack.enter_context(patch.dict(web_app.JOBS, {}, clear=True))
        self.thread = self.stack.enter_context(patch("web_app.threading.Thread"))
        self.client = web_app.app.test_client()
        self.assets = self.stack.enter_context(patch("web_app.ark_assets_client"))
        self.assets.return_value.get_asset.side_effect = lambda aid: {"Id": aid, "Name": "原片", "GroupId": "group-example123", "AssetType": "Video" if "video" in aid else "Image", "Status": "Active", "URL": "https://example.com/source.mp4"}

    def source(self, workflow, service=None):
        service = service or web_app.wardrobe_scene
        job = service.new_source(workflow); job.status = "succeeded"
        if workflow == "references":
            for name in ("reference.mp4", "white_model.mp4", "face_mosaic.mp4"):
                (job.run_dir / name).write_bytes(name.encode())
            job.white_model_path = job.run_dir / "white_model.mp4"
            job.mosaic_path = job.run_dir / "face_mosaic.mp4"
        else:
            service.data(job)["video_asset"] = "asset://asset-video123456"
        service.save(job)
        return job

    @staticmethod
    def image():
        ok, data = cv2.imencode(".png", np.full((540, 960, 3), 120, np.uint8))
        assert ok
        return io.BytesIO(data.tobytes()), "reference.png"

    def generate(self, source, **extra):
        response = self.client.post("/api/wardrobe-swap/generate", data={"source_job_id": source.id, "mode": "dynamic_scene", "request_id": "scene-generation-00001", "paid_confirmed": "true", "prompt": "将海边背景换为森林小路", **extra})
        return response.status_code, response.get_json()

    def references(self):
        return {"white_reviewed": "true", "person_asset": "asset://asset-person123456", "clothing_image": self.image(), "replacement_image": self.image()}

    def test_library_original_with_text_or_optional_scene_image(self):
        for has_image in (False, True):
            source = self.source("library")
            extra = {"replacement_image": self.image(), "prompt": "换成 @ 图片 1 的森林场景"} if has_image else {}
            code, body = self.generate(source, **extra)
            self.assertEqual(code, 202, body)
            kwargs = self.thread.call_args.kwargs["kwargs"]
            self.assertIsNone(kwargs["depth_path"])
            self.assertEqual(kwargs["depth_reference"], "asset://asset-video123456")
            self.assertEqual(kwargs["video_only"], not has_image)
            self.assertEqual(len(kwargs["reference_images"] or []), int(has_image))
            prompt = kwargs["options"]["prompt"]
            self.assertIn("只更换下方指定的背景场景", prompt)
            self.assertNotIn("@图片2", prompt)
            if has_image:
                payload = build_motion_reference_payload(prompt=prompt, image_sources=kwargs["reference_images"], video_reference=kwargs["depth_reference"])
            else:
                payload = build_video_reference_seedance_payload(prompt=prompt, video_reference=kwargs["depth_reference"], model=kwargs["options"]["model"], ratio="adaptive", duration=-1)
            self.assertEqual([x["video_url"]["url"] for x in payload["content"] if x["type"] == "video_url"], ["asset://asset-video123456"])

    def test_green_proxy_binds_identity_clothing_scene_in_order_and_restores_result(self):
        source = self.source("references")
        code, body = self.generate(source, prompt="将绿底替换成@图片3场景", **self.references())
        self.assertEqual(code, 202, body)
        kwargs = self.thread.call_args.kwargs["kwargs"]
        images = kwargs["reference_images"]
        self.assertEqual(len(images), 3)
        self.assertEqual(images[0], "asset://asset-person123456")
        self.assertIn("clothing", images[1]); self.assertIn("replacement", images[2])
        self.assertEqual(kwargs["depth_path"], source.white_model_path)
        self.assertFalse(kwargs["depth_reference"])
        self.assertIn("绿底白模动作母版", kwargs["options"]["prompt"])
        self.assertIn("@图片3只提供场景", kwargs["options"]["prompt"])
        job = web_app.JOBS[body["id"]]
        job.output_path = job.run_dir / "output.mp4"; job.output_path.write_bytes(b"output")
        job.status = "succeeded"; kwargs["on_finished"](job)
        web_app.JOBS.clear()
        restored = web_app.restore_wardrobe_swap_job(job.id)
        self.assertEqual(restored.kind, "wardrobe_generate_dynamic_scene")
        self.assertTrue(restored.output_path.is_file())
        self.assertTrue(restored.replacement_path.is_file())
        self.assertEqual(restored.cast_continuity["wardrobe_dynamic"]["scene_workflow"], "references")

    def test_missing_material_and_cross_mode_requests_never_submit(self):
        source = self.source("references")
        for missing in ("white_reviewed", "person_asset", "replacement_image"):
            refs = self.references(); refs.pop(missing)
            code, body = self.generate(source, **refs); self.assertEqual(code, 400, body)
        for changes in ({"mode": "dynamic_object"}, {"prompt": "@图片4"}, {"paid_confirmed": "false"}):
            refs = self.references(); refs.update(changes)
            code, body = self.generate(source, **refs); self.assertEqual(code, 400, body)
        source = self.source("library")
        for changes in ({"person_asset": "asset://asset-person123456"}, {"clothing_image": self.image()}, {"prompt": "@图片1"}, {"prompt": ""}):
            code, body = self.generate(source, **changes); self.assertEqual(code, 400, body)
        self.thread.assert_not_called()

    def test_optional_clothing_uses_character_outfit_and_binds_scene_as_second_image(self):
        source = self.source("references")
        refs = self.references(); refs.pop("clothing_image")
        code, body = self.generate(source, **refs)
        self.assertEqual(code, 202, body)
        kwargs = self.thread.call_args.kwargs["kwargs"]
        self.assertEqual(len(kwargs["reference_images"]), 2)
        self.assertEqual(kwargs["reference_images"][0], "asset://asset-person123456")
        self.assertIn("未上传独立服装参考", kwargs["options"]["prompt"])
        self.assertIn("@图片2仅提供新背景环境", kwargs["options"]["prompt"])

    def test_references_mask_all_faces_with_threshold_then_make_green_proxy(self):
        source = self.source("references")
        response = self.client.post("/api/wardrobe-swap/mosaic", data={"mode": "dynamic_scene", "source_job_id": source.id, "face_score_threshold": ".40"})
        self.assertEqual(response.status_code, 202, response.get_json())
        job = web_app.JOBS[response.get_json()["id"]]
        with patch("web_app.normalize_wardrobe_source_duration", return_value=job.run_dir / "reference.mp4"), patch("web_app.run_wardrobe_face_mosaic") as mask:
            self.thread.call_args.kwargs["target"]()
            self.assertEqual(mask.call_args.kwargs["score_threshold"], .4)
        self.assertEqual(job.cast_continuity["wardrobe_dynamic"]["scene_workflow"], "references")
        self.assertEqual(job.cast_continuity["wardrobe_dynamic"]["mosaic_scope"], "all_faces")
        base = {"source_job_id": source.id, "request_id": "scene-white-model-00001", "paid_confirmed": "true", "mosaic_reviewed": "true"}
        self.thread.reset_mock()
        for change in ({"white_prompt": "参考@图片1"}, {"mosaic_reviewed": "false"}):
            response = self.client.post("/api/wardrobe-swap/white-model", data={**base, **change})
            self.assertEqual(response.status_code, 400, response.get_json())
        self.thread.assert_not_called()
        response = self.client.post("/api/wardrobe-swap/white-model", data=base)
        self.assertEqual(response.status_code, 202, response.get_json())
        with patch("web_app.run_wardrobe_white_model") as white:
            self.thread.call_args.kwargs["target"]()
            self.assertEqual(white.call_args.kwargs["prompt_override"], DYNAMIC_SCENE_WHITE_PROMPT)
            self.assertIn("#00FF00", white.call_args.kwargs["prompt_override"])
        again = self.client.post("/api/wardrobe-swap/white-model", data=base)
        self.assertEqual(again.get_json()["id"], source.id)
        self.assertEqual(self.thread.call_count, 1)

    def test_import_green_proxy_is_free_and_validates_video_length(self):
        for duration in (2, 15):
            with patch("wardrobe_object.validate_seedance_reference_video", return_value=Mock(duration=duration)):
                response = self.client.post("/api/wardrobe-scene/import-white", data={"white_video": (io.BytesIO(b"white"), "white.mp4")})
            self.assertEqual(response.status_code, 201, response.get_json())
            self.assertTrue(response.get_json()["has_white_model"])
            self.assertEqual(response.get_json()["wardrobe_dynamic"]["scene_workflow"], "references")
            self.assertIn("绿底白模", response.get_json()["stage"])
        with patch("wardrobe_object.validate_seedance_reference_video", side_effect=WorkflowError("视频需为 2–15 秒")):
            response = self.client.post("/api/wardrobe-scene/import-white", data={"white_video": (io.BytesIO(b"white"), "white.mp4")})
        self.assertEqual(response.status_code, 400)
        self.thread.assert_not_called(); self.assets.assert_not_called()

    def test_video_selection_requires_active_video_and_records_group(self):
        response = self.client.post("/api/wardrobe-scene/select-video", data={"video_asset": "asset://asset-video123456"})
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertFalse(response.get_json()["has_white_model"])
        self.assertEqual(response.get_json()["wardrobe_dynamic"]["video_group_id"], "group-example123")
        for status, kind in (("Processing", "Video"), ("Active", "Image")):
            self.assets.return_value.get_asset.side_effect = None
            self.assets.return_value.get_asset.return_value = {"Status": status, "AssetType": kind}
            response = self.client.post("/api/wardrobe-scene/select-video", data={"video_asset": "asset://asset-video123456"})
            self.assertEqual(response.status_code, 400)
        self.thread.assert_not_called()

    def test_histories_are_separate_and_legacy_scene_proxy_cannot_be_reused(self):
        expected = {workflow: self.source(workflow).id for workflow in ("library", "references")}
        self.source("library", web_app.wardrobe_object)
        legacy = self.source("references")
        legacy.cast_continuity["wardrobe_dynamic"].pop("workflow_version")
        web_app.wardrobe_scene.save(legacy)
        web_app.JOBS.clear()
        for workflow, job_id in expected.items():
            body = self.client.get("/api/wardrobe-scene/latest?workflow=" + workflow).get_json()
            self.assertEqual(body["id"], job_id)
        code, body = self.generate(web_app.restore_wardrobe_swap_job(legacy.id), **self.references())
        self.assertEqual(code, 400, body)
        response = self.client.post("/api/wardrobe-swap/white-model", data={"source_job_id": legacy.id, "request_id": "scene-legacy-white-0001", "paid_confirmed": "true", "mosaic_reviewed": "true"})
        self.assertEqual(response.status_code, 400)
        self.thread.assert_not_called()

    def test_generation_is_idempotent(self):
        source = self.source("library")
        code, first = self.generate(source); self.assertEqual(code, 202)
        code, second = self.generate(source); self.assertEqual(code, 202)
        self.assertEqual(first["id"], second["id"])
        code, body = self.generate(source, request_id="scene-generation-00002")
        self.assertEqual(code, 400, body); self.assertEqual(self.thread.call_count, 1)

    def test_video_upload_receipts_and_dedup_are_shared_without_generation(self):
        self.assertIs(web_app.wardrobe_scene.lock, web_app.wardrobe_object.lock)
        key = "scene-existing-upload-0001"
        web_app.save_shot_manifest(web_app.wardrobe_object.receipt_path(key), {"request_id": key, "status": "reused", "asset_id": "asset-video123456", "uri": "asset://asset-video123456"})
        response = self.client.post("/api/wardrobe-scene/upload-video", data={"request_id": key})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["status"], "reused")
        response = self.client.get("/api/wardrobe-scene/upload-status/" + key)
        self.assertEqual(response.get_json()["asset_status"], "Active")
        self.thread.assert_not_called(); self.assets.return_value.create_asset.assert_not_called()

    def test_prompt_only_allows_bound_mentions(self):
        self.assertIn("@图片3", scene_final_prompt("references", "换成 @ 图片 3 场景", True))
        for workflow, image, custom in (("references", False, "换森林"), ("library", False, "@图片1"), ("library", True, "@图片2"), ("references", True, "@视频2"), ("library", False, "@素材")):
            with self.assertRaises(WorkflowError): scene_final_prompt(workflow, custom, image)


if __name__ == "__main__": unittest.main()
