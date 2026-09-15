from __future__ import annotations

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
from face_mosaic import render_face_mosaic_video
from wardrobe_dynamic import DYNAMIC_FINAL_PROMPT, DYNAMIC_WHITE_PROMPT, dynamic_prompt
from workflow_core import WorkflowError


ANCHORS = [{"time": 0, "x": .2, "y": .2, "w": .2, "h": .3, "absent": False}]


class DynamicRulesTests(unittest.TestCase):
    def test_prompt_normalizes_actual_references_and_rejects_unbound_media(self):
        prompt = dynamic_prompt(DYNAMIC_FINAL_PROMPT, "画面左侧的主角", "按照 @ 图片 1 的身份，穿 @图片2 的衣服。")
        self.assertIn("@图片1", prompt)
        self.assertNotIn("@ 图片", prompt)
        for custom in ("参考@图片3", "换成@场景", "参考@视频2", "参考@"):
            with self.subTest(custom=custom), self.assertRaises(WorkflowError):
                dynamic_prompt(DYNAMIC_FINAL_PROMPT, "左侧主角", custom)
        with self.assertRaises(WorkflowError):
            dynamic_prompt(DYNAMIC_WHITE_PROMPT, "左侧主角", "参考@图片1", white=True)

    def test_white_prompt_works_without_manual_description_and_does_not_use_masks_as_target(self):
        prompt = dynamic_prompt(DYNAMIC_WHITE_PROMPT, white=True)
        self.assertIn("打码不是主角标记", prompt)
        self.assertIn("只将主要人物转为白模", prompt)
        self.assertNotIn("由用户指定", prompt)
        self.assertNotIn("指定主角：", prompt)

    def test_video_render_masks_every_detected_face_and_preserves_background(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory)/"source.mp4", Path(directory)/"masked.mp4"
            rng = np.random.default_rng(6)
            frame = np.zeros((240,320,3),np.uint8)
            frame[48:120,64:128] = rng.integers(0,255,(72,64,3),dtype=np.uint8)
            frame[70:130,235:295] = rng.integers(0,255,(60,60,3),dtype=np.uint8)
            writer = cv2.VideoWriter(str(source),cv2.VideoWriter_fourcc(*"mp4v"),25,(320,240))
            self.assertTrue(writer.isOpened())
            for _ in range(10): writer.write(frame)
            writer.release()
            with patch("face_mosaic.ensure_face_model"), patch("face_mosaic.YuNetFaceDetector") as detector:
                detector.return_value.detect.return_value = [(64,48,64,72),(235,70,60,60)]
                result = render_face_mosaic_video(source,target)
                self.assertEqual(detector.return_value.detect.call_count, 10)
            self.assertEqual(result["frames"],10); self.assertEqual(result["maximum_faces"],2)
            decoded = []
            for path in (source,target):
                capture = cv2.VideoCapture(str(path)); ok,image = capture.read(); capture.release()
                self.assertTrue(ok); decoded.append(image.astype(float))
            difference = np.abs(decoded[0]-decoded[1]).mean(axis=2)
            self.assertGreater(difference[60:105,75:115].mean(),20)
            self.assertGreater(difference[75:125,240:290].mean(),20)
            self.assertLess(difference[170:230,10:300].mean(),3)


class DynamicAPITests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch("web_app.PROJECT_DIR",self.root))
        self.stack.enter_context(patch("workflow_core.PROJECT_DIR",self.root))
        self.stack.enter_context(patch("workflow_core.RUNS_DIR",self.root/"runs"))
        self.stack.enter_context(patch.dict(web_app.JOBS,{},clear=True))
        self.thread = self.stack.enter_context(patch("web_app.threading.Thread"))
        self.assets = self.stack.enter_context(patch("web_app.ark_assets_client"))
        self.assets.return_value.get_asset.return_value = {"Status":"Active","AssetType":"Image"}
        web_app.app.config.update(TESTING=True)
        self.client = web_app.app.test_client()
        self.source = web_app.new_job("wardrobe_prepare_dynamic")
        self.source.status = "succeeded"
        for name in ("reference.mp4","face_mosaic.mp4","white_model.mp4"):
            (self.source.run_dir/name).write_bytes(name.encode())
        self.source.mosaic_path = self.source.run_dir/"face_mosaic.mp4"
        self.source.white_model_path = self.source.run_dir/"white_model.mp4"
        self.source.cast_continuity["wardrobe_dynamic"] = {"anchors":ANCHORS,"description":"左边红衣主角","requests":{}}
        web_app.persist_wardrobe_swap_job(self.source,"dynamic")

    def data(self, **changes):
        data = {"source_job_id":self.source.id,"mode":"dynamic","paid_confirmed":"true","request_id":"request-dynamic-test-0001","mosaic_reviewed":"true","white_reviewed":"true","person_asset":"asset://role-active-123"}
        data.update(changes)
        return data

    @staticmethod
    def clothing():
        ok, image = cv2.imencode(".png",np.full((480,640,3),180,np.uint8))
        assert ok
        return (io.BytesIO(image.tobytes()),"clothing.png")

    def post(self, path, data):
        with self.client.post("/api/wardrobe-swap/"+path,data=data) as response:
            return response.status_code,response.get_json()

    def test_white_requires_review_and_idempotently_submits_selective_prompt(self):
        status,_ = self.post("white-model",self.data(mosaic_reviewed="false"))
        self.assertEqual(status,400); self.thread.assert_not_called()
        status,result = self.post("white-model",self.data(white_prompt="地面保护员保持原样"))
        self.assertEqual(status,202,result)
        self.assertIsNone(self.source.white_model_path)
        self.assertTrue((self.source.run_dir/"white_model.mp4").is_file())
        self.assertIn("request-dynamic-test-0001",str(web_app.wardrobe_white_task_dir(self.source)))
        status,repeated = self.post("white-model",self.data())
        self.assertEqual(status,202,repeated); self.assertEqual(self.thread.call_count,1)
        worker = self.thread.call_args.kwargs["target"]
        with patch("web_app.run_wardrobe_white_model") as run:
            worker()
            self.assertTrue(run.call_args.kwargs["preserve_scene"])
            self.assertIn("地面保护员",run.call_args.kwargs["prompt_override"])

    def test_final_has_exactly_person_clothing_and_video_and_no_scene_extraction(self):
        status,result = self.post("generate",self.data(new_clothing_image=self.clothing(),prompt="参考@图片2的衣领。"))
        self.assertEqual(status,202,result)
        kwargs = self.thread.call_args.kwargs["kwargs"]
        self.assertEqual(kwargs["reference_images"],["asset://role-active-123",kwargs["clothing_source"]])
        self.assertEqual(kwargs["scene_source"],"")
        self.assertEqual(kwargs["depth_path"],self.source.white_model_path)
        payload = web_app.build_motion_reference_payload(prompt=kwargs["options"]["prompt"],image_sources=kwargs["reference_images"],video_reference="https://example.com/white.mp4")
        self.assertEqual([c["type"] for c in payload["content"]],["text","image_url","image_url","video_url"])
        self.assertEqual(payload["content"][1]["image_url"]["url"],"asset://role-active-123")
        status,repeated = self.post("generate",self.data())
        self.assertEqual(status,202,repeated); self.assertEqual(repeated["id"],result["id"])
        self.assertEqual(self.thread.call_count,1)

    def test_saved_white_prompt_matches_submitted_red_white_prompt(self):
        status, result = self.post('white-model', self.data(inline_cast_count='2'))
        self.assertEqual(status, 202, result)
        saved = self.source.cast_continuity['wardrobe_dynamic']['white_prompt']
        self.assertIn('男性为红模', saved)
        self.assertIn('模型去服装规则', saved)
        self.assertIn('去除原片已有', saved)
        with patch('web_app.run_wardrobe_white_model') as run:
            self.thread.call_args.kwargs['target']()
        self.assertEqual(run.call_args.kwargs['prompt_override'], saved)

    def test_final_rejects_pending_role_and_unbound_prompt_without_submission(self):
        self.assets.return_value.get_asset.return_value["Status"] = "Processing"
        status,result = self.post("generate",self.data(new_clothing_image=self.clothing()))
        self.assertEqual(status,400); self.assertIn("Active",result["error"])
        self.assets.return_value.get_asset.return_value["Status"] = "Active"
        status,result = self.post("generate",self.data(new_clothing_image=self.clothing(),prompt="参考@图片3"))
        self.assertEqual(status,400); self.assertIn("未提交",result["error"])
        self.thread.assert_not_called()

    def test_paid_confirmation_and_preview_gates(self):
        for endpoint, change in (("generate",{"paid_confirmed":"false"}), ("white-model",{"paid_confirmed":"false"}), ("generate",{"white_reviewed":"false"}), ("white-model",{"request_id":""})):
            with self.subTest(endpoint=endpoint,change=change):
                status,_ = self.post(endpoint,self.data(**change)); self.assertEqual(status,400)
        self.thread.assert_not_called()

    def test_remosaic_reuses_old_source_without_anchors_or_cloud_calls(self):
        status,result = self.post("mosaic",self.data())
        self.assertEqual(status,202,result)
        child = web_app.JOBS[result["id"]]
        self.assertNotEqual(child.id,self.source.id)
        self.assertEqual((child.run_dir/"reference.mp4").read_bytes(),b"reference.mp4")
        self.assertEqual(result["wardrobe_dynamic"]["mosaic_scope"],"all_faces")
        self.assertNotIn("anchors",result["wardrobe_dynamic"])
        worker = self.thread.call_args.kwargs["target"]
        with patch("web_app.normalize_wardrobe_source_duration",return_value=child.run_dir/"reference.mp4"), patch("web_app.run_wardrobe_face_mosaic") as mosaic:
            worker()
            mosaic.assert_called_once_with(child,child.run_dir/"reference.mp4",start=2,span=96,score_threshold=0.55)
        self.assets.assert_not_called()
        with self.client.get(f"/api/jobs/{child.id}/file/source") as response:
            self.assertEqual(response.status_code,200); self.assertEqual(response.data,b"reference.mp4")
            self.assertEqual(response.mimetype,"video/mp4")

    def test_upload_needs_no_target_fields_and_ignores_legacy_anchors(self):
        status,result = self.post("mosaic",{"mode":"dynamic","reference_video":(io.BytesIO(b"video fixture"),"new.mp4"),"target_anchors":"invalid-old-data"})
        self.assertEqual(status,202,result)
        self.assertEqual(result["wardrobe_dynamic"]["mosaic_scope"],"all_faces")
        self.assertEqual(result["wardrobe_dynamic"]["description"],"")
        self.assets.assert_not_called()

    def test_threshold_reaches_detector_and_survives_manifest_restore(self):
        status,result = self.post("mosaic",self.data(face_score_threshold="0.35"))
        self.assertEqual(status,202,result)
        child = web_app.JOBS[result["id"]]
        self.assertEqual(result["wardrobe_mosaic"]["face_score_threshold"],0.35)
        worker = self.thread.call_args.kwargs["target"]
        with patch("web_app.normalize_wardrobe_source_duration",return_value=child.run_dir/"reference.mp4"), patch("web_app.inspect_video",return_value=Mock(duration=15)), patch("web_app.render_face_mosaic_video",return_value={"frames":450,"frames_with_faces":300}) as render:
            worker()
            self.assertEqual(render.call_args.kwargs["score_threshold"],0.35)
            self.assertTrue(render.call_args.kwargs["detect_rotated_faces"])
        self.assertEqual(child.status,"succeeded")
        self.assertTrue(any("0.35" in line for line in child.logs))
        web_app.JOBS.pop(child.id)
        restored = web_app.restore_wardrobe_swap_job(child.id)
        self.assertEqual(restored.public()["wardrobe_mosaic"]["face_score_threshold"],0.35)
        self.assets.assert_not_called()

    def test_invalid_threshold_never_creates_job_or_starts_processing(self):
        original_ids = set(web_app.JOBS)
        for value in ("", "abc", "NaN", "Infinity", "-Infinity", "0.29", "0.91"):
            with self.subTest(value=value):
                status,result = self.post("mosaic",self.data(face_score_threshold=value))
                self.assertEqual(status,400,result)
                self.assertIn("阈值",result["error"])
        self.assertEqual(set(web_app.JOBS),original_ids)
        self.thread.assert_not_called()

    def test_static_mode_can_remosaic_own_source_but_not_another_mode(self):
        status,_ = self.post("mosaic",self.data(mode="person"))
        self.assertEqual(status,400)
        self.thread.assert_not_called()
        self.source.kind = "wardrobe_prepare_person"
        status,result = self.post("mosaic",self.data(mode="person",face_score_threshold="0.90"))
        self.assertEqual(status,202,result)
        self.assertEqual(result["wardrobe_mosaic"]["face_score_threshold"],0.90)
        self.assertEqual((web_app.JOBS[result["id"]].run_dir/"reference.mp4").read_bytes(),b"reference.mp4")

    def test_manifest_restores_target_and_attempt_and_rejects_static_extraction(self):
        self.source.cast_continuity["wardrobe_dynamic"]["white_attempt"] = "previous-dynamic-0001"
        web_app.persist_wardrobe_swap_job(self.source,"dynamic")
        web_app.JOBS.pop(self.source.id)
        restored = web_app.restore_wardrobe_swap_job(self.source.id)
        self.assertEqual(restored.cast_continuity,self.source.cast_continuity)
        for endpoint in ("extract-references","extract-person","prepare"):
            status,_ = self.post(endpoint,self.data()); self.assertEqual(status,400)
        self.thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
