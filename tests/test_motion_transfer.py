from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

import web_app as host
from motion_transfer import PROJECT
from workflow_core import WorkflowError, build_motion_reference_payload


class ImmediateThread:
    def __init__(self, target, **kwargs):
        self.target = target
    def start(self):
        self.target()


class MotionTransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.engine = host.motion_transfer
        for p in (patch.object(host, "PROJECT_DIR", self.root), patch.object(host, "JOBS", {}),
                  patch.object(host, "timestamped_run_dir", side_effect=self.directory),
                  patch("motion_transfer.threading.Thread", ImmediateThread),
                  patch.object(host, "validate_seedance_reference_video", return_value=SimpleNamespace(duration=5)),
                  patch.object(host, "ark_assets_client", return_value=Mock(get_asset=Mock(return_value={"Status":"Active","AssetType":"Image"})))):
            p.start()
            self.addCleanup(p.stop)
        self.client = host.app.test_client()
        host.app.config.update(TESTING=True)

    def directory(self, suffix):
        p = self.root / "runs" / f"20260911_{suffix}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def image(self):
        return io.BytesIO(cv2.imencode(".png", np.full((640,640,3),220,dtype=np.uint8))[1].tobytes())

    def job(self):
        job = self.engine.new()
        white = job.run_dir / "white.mp4"
        white.write_bytes(b"white fixture")
        job.white_model_path = white
        job.source_duration = 5
        job.status = "succeeded"
        self.engine.save(job)
        return job

    def data(self, job, **extra):
        return {"project_id":job.id,"mode":"cast","prompt":"让@图片2复刻@视频1的动作，场景为@图片1。",
                "roles":json.dumps([{"uri":"asset://actor_123456","name":"主角","role":"原片左侧人物"}]),
                "scene":(self.image(),"scene.png"), "request_id":"request_123456789", "paid_confirmed":"true", "white_reviewed":"true", **extra}

    def generate_success(self, child, **kwargs):
        child.task_id = "paid_task_existing"
        child.output_path = child.run_dir / "output.mp4"
        child.output_path.write_bytes(b"final fixture")
        child.update(status="succeeded",cloud_status="succeeded",stage="完成",progress=100)
        host.persist_cloud_job(child, status="succeeded")

    def test_page_and_isolated_project(self):
        r = self.client.get("/projects/motion-transfer")
        self.assertEqual(r.status_code,200)
        self.assertIn("首帧 / 尾帧参考",r.get_data(as_text=True))
        r.close()
        self.assertEqual(host.project_for_kind("motion_prepare"),PROJECT)
        other = host.new_job("wardrobe_prepare_custom")
        self.assertEqual(self.client.get(f"/api/motion-transfer/projects/{other.id}").status_code,400)

    def test_import_white_and_restore_without_cloud_calls(self):
        with patch.object(host,"api_client") as cloud:
            r = self.client.post("/api/motion-transfer/import-white",data={"white_video":(io.BytesIO(b"white"),"white.mp4")})
            self.assertEqual(r.status_code,200)
            p=r.get_json();host.JOBS.clear()
            restored=self.client.get(f"/api/motion-transfer/projects/{p['id']}").get_json()
            self.assertTrue(restored["white_url"])
            self.assertEqual(restored["source_origin"],"white")
            cloud.assert_not_called()

    def test_prepare_only_masks_locally(self):
        def mosaic(job,source,**kwargs):
            job.mosaic_path=job.run_dir/"masked.mp4";job.mosaic_path.write_bytes(b"masked");job.source_duration=5
        with patch.object(host,"inspect_video",return_value=SimpleNamespace(duration=5)), patch.object(host,"normalize_wardrobe_source_duration",side_effect=lambda job,source,**kw:source), patch.object(host,"run_wardrobe_face_mosaic",side_effect=mosaic), patch.object(host,"run_generation") as cloud:
            r=self.client.post("/api/motion-transfer/prepare",data={"source":(io.BytesIO(b"source"),"source.mp4")})
            self.assertEqual(r.status_code,202)
            self.assertTrue(r.get_json()["mosaic_url"])
            cloud.assert_not_called()

    def test_white_requires_mosaic_and_paid_confirmation(self):
        job=self.job();job.white_model_path=None
        for extra in ({},{"mosaic_reviewed":"true"},{"paid_confirmed":"true"}):
            with self.subTest(extra=extra), patch.object(host,"run_wardrobe_white_model") as cloud:
                r=self.client.post("/api/motion-transfer/white-model",data={"project_id":job.id,"request_id":"white_123456789",**extra})
                self.assertEqual(r.status_code,400);cloud.assert_not_called()

    def test_white_uses_mosaic_and_is_idempotent(self):
        job=self.job();job.white_model_path=None
        job.depth_path=job.run_dir/"source.mp4";job.depth_path.write_bytes(b"source")
        job.mosaic_path=job.run_dir/"mosaic.mp4";job.mosaic_path.write_bytes(b"mosaic")
        def white(job,source,**kwargs):
            self.assertEqual(kwargs["child_project"],PROJECT)
            self.assertEqual(kwargs["reference_source_factory"],self.engine.video_reference)
            self.assertEqual(kwargs["prompt_override"],host.REAL_PERSON_SAFE_WHITE_MODEL_PROMPT)
            job.white_model_path=job.run_dir/"white.mp4";job.white_model_path.write_bytes(b"white")
        data={"project_id":job.id,"request_id":"white_123456789","mosaic_reviewed":"true","paid_confirmed":"true"}
        with patch.object(host,"run_wardrobe_white_model",side_effect=white) as generate, patch.object(host,"run_wardrobe_face_mosaic") as mosaic:
            for _ in range(2):self.assertEqual(self.client.post("/api/motion-transfer/white-model",data=data).status_code,202 if _==0 else 200)
            self.assertEqual(generate.call_count,1);mosaic.assert_not_called()

    def test_cast_payload_order_and_dedup_survive_restart(self):
        job=self.job()
        with patch.object(host,"run_generation",side_effect=self.generate_success) as generate:
            r=self.client.post("/api/motion-transfer/generate",data=self.data(job))
            self.assertEqual(r.status_code,202,r.get_json())
            args=generate.call_args.kwargs
            self.assertEqual(args["reference_source_factory"],self.engine.video_reference)
            self.assertTrue(args["reference_images"][0].endswith(".png"))
            self.assertEqual(args["reference_images"][1],"asset://actor_123456")
            self.assertIn("原片左侧人物",args["options"]["prompt"])
            self.assertEqual(args["options"]["duration"],-1)
            output_id = r.get_json()["outputs"][0]["id"]
            snapshot = json.loads((job.run_dir / "outputs" / output_id / "motion_inputs.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["image_sources"], args["reference_images"])
            self.assertEqual(snapshot["roles"][0]["uri"], "asset://actor_123456")
            host.JOBS.clear()
            r=self.client.post("/api/motion-transfer/generate",data={"project_id":job.id,"request_id":"request_123456789"})
            self.assertEqual(r.status_code,202)
            self.assertEqual(generate.call_count,1)
            self.assertTrue(r.get_json()["outputs"][0]["url"])

    def test_paid_and_review_gates(self):
        job=self.job()
        for field in ("paid_confirmed","white_reviewed"):
            with self.subTest(field=field), patch.object(host,"run_generation") as generate:
                r=self.client.post("/api/motion-transfer/generate",data=self.data(job,**{field:"false"}))
                self.assertEqual(r.status_code,400);generate.assert_not_called()

    def test_invalid_mentions_and_processing_roles_prevent_payment(self):
        job=self.job()
        for prompt in ("参考@图片9和@视频1","参考@视频2","【已移除的人物图片】"):
            with patch.object(host,"run_generation") as generate:
                r=self.client.post("/api/motion-transfer/generate",data=self.data(job,prompt=prompt))
                self.assertEqual(r.status_code,400);generate.assert_not_called()
        with patch.object(host,"ark_assets_client",return_value=Mock(get_asset=Mock(return_value={"Status":"Processing","AssetType":"Image"}))), patch.object(host,"run_generation") as generate:
            self.assertEqual(self.client.post("/api/motion-transfer/generate",data=self.data(job)).status_code,400)
            generate.assert_not_called()

    def test_frame_guides_include_white_video_without_mixed_api_roles(self):
        job=self.job()
        for with_last in (False,True):
            data=self.data(job,mode="frames",prompt="复刻@视频1，以@图片1开场。",request_id=f"frame_request_{with_last}",first=(self.image(),"first.png"))
            data.pop("scene")
            if with_last:data["last"]=(self.image(),"last.png")
            with patch.object(host,"run_generation",side_effect=self.generate_success) as generate:
                r=self.client.post("/api/motion-transfer/generate",data=data)
                self.assertEqual(r.status_code,202,r.get_json())
                args=generate.call_args.kwargs
                self.assertEqual(len(args["reference_images"]),2 if with_last else 1)
                payload=build_motion_reference_payload(prompt=args["options"]["prompt"],image_sources=args["reference_images"],video_reference="https://example.com/white.mp4")
                self.assertEqual(payload["content"][-1]["role"],"reference_video")
                self.assertTrue(all(c.get("role")=="reference_image" for c in payload["content"][1:-1]))

    def test_draft_preserves_uploads_when_only_prompt_changes(self):
        job=self.job();data=self.data(job,mode="frames",first=(self.image(),"first.png"),last=(self.image(),"last.png"))
        self.assertEqual(self.client.post("/api/motion-transfer/draft",data=data).status_code,200)
        first=job.cast_continuity["draft"]["first_path"]
        r=self.client.post("/api/motion-transfer/draft",data={"project_id":job.id,"prompt":"新提示词","clear_last":"true"})
        self.assertEqual(r.status_code,200)
        self.assertEqual(job.cast_continuity["draft"]["first_path"],first)
        self.assertFalse(r.get_json()["draft"]["last_url"])
        host.JOBS.clear();restored=self.engine.load(job.id)
        self.assertEqual(restored.cast_continuity["draft"]["prompt"],"新提示词")

    def test_existing_white_result_is_recovered_without_new_paid_task(self):
        job=self.job();job.white_model_path=None
        directory=job.run_dir/"white_model_task";directory.mkdir()
        child=host.WebJob(id=f"{job.id}-white",kind="motion_white",project=PROJECT,run_dir=directory)
        self.generate_success(child);host.JOBS[child.id]=child
        self.assertTrue(self.engine.public(job)["needs_query"])
        with patch.object(host,"run_wardrobe_white_model") as paid,patch.object(host,"resume_cloud_job") as query,patch.object(host,"conform_video_duration",return_value=child.output_path):
            r=self.client.post("/api/motion-transfer/recover",data={"project_id":job.id})
            self.assertEqual(r.status_code,202);self.assertTrue(r.get_json()["white_url"])
            paid.assert_not_called();query.assert_not_called()

    def test_ambiguous_submission_blocks_new_generation_and_recovers_read_only(self):
        job=self.job()
        def ambiguous(child,**kwargs):
            child.update(status="failed",recovery_action="recover_seedance_submission")
            host.persist_cloud_job(child,status="ambiguous",submitted_at=100,model=host.DEFAULT_SEEDANCE_25_MODEL,duration=-1)
        with patch.object(host,"run_generation",side_effect=ambiguous) as generate:
            first=self.client.post("/api/motion-transfer/generate",data=self.data(job))
            self.assertTrue(first.get_json()["needs_query"])
            again=self.client.post("/api/motion-transfer/generate",data=self.data(job,request_id="request_987654321"))
            self.assertEqual(again.status_code,400);self.assertEqual(generate.call_count,1)
        cloud=Mock(recover_created_task=Mock(return_value="task_recovered"))
        with patch.object(host,"api_client",return_value=cloud),patch.object(host,"resume_cloud_job",side_effect=self.generate_success),patch.object(host,"run_generation") as paid:
            r=self.client.post("/api/motion-transfer/recover",data={"project_id":job.id})
            self.assertEqual(r.status_code,202,r.get_json());paid.assert_not_called()
            self.assertEqual(cloud.recover_created_task.call_args.kwargs["duration"],0)
            self.assertTrue(r.get_json()["outputs"][0]["url"])

    def test_file_routes_reject_escape_and_keep_output(self):
        job=self.job()
        outside=self.root/"secret.png";outside.write_bytes(b"private")
        job.cast_continuity["draft"]["scene_path"]=str(outside)
        self.assertEqual(self.client.get(f"/api/motion-transfer/projects/{job.id}/files/scene").status_code,404)
        r=self.client.get(f"/api/motion-transfer/projects/{job.id}/files/white?download=1")
        self.assertEqual(r.status_code,200);self.assertIn("attachment",r.headers["Content-Disposition"]);r.close()

    def test_payload_rejects_missing_or_excessive_materials(self):
        for images in ([],["asset://example_123"]*10,[""]):
            with self.assertRaises(WorkflowError):build_motion_reference_payload(prompt="动作迁移",image_sources=images,video_reference="https://example.com/white.mp4")


if __name__ == "__main__":
    unittest.main()
