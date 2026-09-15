import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import web_app
from wardrobe_continuation import assemble_continuation, continuation_prompt, ffmpeg, prepare_cut
from workflow_core import WorkflowError, inspect_video, resolve_ffmpeg


class ContinuationMediaTests(unittest.TestCase):
    def test_prefix_frames_audio_and_cut_are_from_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/"source.mp4";tail=root/"tail.mp4"
            ffmpeg(["-f","lavfi","-i","testsrc2=size=320x480:rate=30:duration=4","-f","lavfi","-i","sine=frequency=440:duration=4","-c:v","libx264","-crf","16","-c:a","aac",str(source)])
            ffmpeg(["-f","lavfi","-i","color=blue:size=480x320:rate=24:duration=2","-f","lavfi","-i","sine=frequency=880:duration=2","-c:v","libx264","-c:a","aac",str(tail)])
            digest=hashlib.sha256(source.read_bytes()).digest()
            settings=prepare_cut(source,root,2,web_app)
            self.assertEqual(settings["keep_seconds"],2)
            output=assemble_continuation(source,tail,root/"out.mp4",2)
            self.assertAlmostEqual(inspect_video(output).duration,4,delta=.1)
            captures=[cv2.VideoCapture(str(path)) for path in (source,output)]
            try:
                for index in range(60):
                    frames=[capture.read()[1] for capture in captures]
                    self.assertTrue(all(frame is not None for frame in frames))
                    self.assertLess(np.abs(frames[0].astype(float)-frames[1]).mean(),4,f"prefix frame {index}")
                ok,frame=captures[1].read();self.assertTrue(ok)
                self.assertGreater(frame[220:260,120:200,0].mean(),220)
                cut=cv2.imdecode(np.frombuffer(Path(settings["cut_frame"]).read_bytes(),np.uint8),cv2.IMREAD_COLOR)
                self.assertLess(np.abs(cut.astype(float)-frames[0]).mean(),2)
            finally:
                for capture in captures:capture.release()
            raw=subprocess.run([str(resolve_ffmpeg()),"-v","error","-i",str(output),"-map","0:a:0","-ac","1","-ar","8000","-f","f32le","-"],capture_output=True,check=True).stdout
            audio=np.frombuffer(raw,np.float32)
            for start,frequency in ((.5,440),(2.5,880)):
                segment=audio[int(start*8000):int((start+.4)*8000)]
                peak=np.fft.rfftfreq(len(segment),1/8000)[np.argmax(abs(np.fft.rfft(segment)))]
                self.assertAlmostEqual(peak,frequency,delta=5)
            self.assertEqual(hashlib.sha256(source.read_bytes()).digest(),digest)

    def test_prompt_and_cut_validation(self):
        self.assertIn("不要重播",continuation_prompt("人物转身离开"))
        for prompt in ("", "@图片10", "@视频2", "x"*1201):
            with self.assertRaises(WorkflowError):continuation_prompt(prompt)
        with patch("wardrobe_continuation.inspect_video",return_value=SimpleNamespace(duration=20,fps=30)):
            for cut in (float("nan"),float("inf"),.1,20,21):
                with self.assertRaises(WorkflowError):prepare_cut(Path("unused"),Path("unused"),cut,web_app)


class ContinuationAPITests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name,value in (("web_app.PROJECT_DIR",self.root),("workflow_core.PROJECT_DIR",self.root),("workflow_core.RUNS_DIR",self.root/"runs")):
            self.stack.enter_context(patch(name,value))
        self.stack.enter_context(patch.dict(web_app.JOBS,{},clear=True))
        self.thread=self.stack.enter_context(patch("wardrobe_continuation.threading.Thread"))
        self.service=web_app.wardrobe_continuation
        web_app.app.config.update(TESTING=True);self.client=web_app.app.test_client()
        self.job=web_app.new_job("wardrobe_continuation_prepare");self.job.status="succeeded"
        source=self.job.run_dir/"reference.mp4";source.write_bytes(b"full original beyond 15s")
        context=self.job.run_dir/"context.mp4";context.write_bytes(b"prefix context")
        frame=self.job.run_dir/"cut_frame.png";ok,image=cv2.imencode(".png",np.full((480,640,3),120,np.uint8));self.assertTrue(ok);frame.write_bytes(image.tobytes())
        self.job.cast_continuity["continuation"]={"source":str(source),"context":str(context),"cut_frame":str(frame),"source_duration":40,"keep_seconds":25,"attempts":{}}
        self.service.save(self.job)

    def data(self,**changes):
        return dict({"source_job_id":self.job.id,"prompt":"人物发现桌上的礼物，惊喜地拆开包装","suffix_seconds":"8","resolution":"720p","generate_audio":"true","paid_confirmed":"true","cut_reviewed":"true","request_id":"continuation-api-0001"},**changes)

    def post(self,endpoint,data):
        with self.client.post("/api/wardrobe-continuation/"+endpoint,data=data) as response:return response.status_code,response.get_json()

    def test_only_suffix_is_generated_and_repeat_is_idempotent(self):
        code,body=self.post("generate",self.data());self.assertEqual(code,202,body)
        worker=self.thread.call_args.kwargs["target"]
        with patch("web_app.run_generation") as generation,patch.object(self.service,"finish"):
            worker();kwargs=generation.call_args.kwargs
            self.assertEqual(kwargs["depth_path"].name,"context.mp4")
            self.assertEqual(kwargs["reference_images"],[self.service.data(self.job)["cut_frame"]])
            self.assertEqual(kwargs["options"]["duration"],8)
            self.assertIn("只生成",kwargs["options"]["prompt"])
        calls=self.thread.call_count
        code,_=self.post("generate",self.data());self.assertEqual(code,202);self.assertEqual(self.thread.call_count,calls)
        code,_=self.post("generate",self.data(request_id="continuation-api-0002"));self.assertEqual(code,400)

    def test_preview_duration_prompt_and_payment_validation(self):
        for changes in ({"paid_confirmed":"false"},{"cut_reviewed":"false"},{"suffix_seconds":"16"},{"suffix_seconds":"3"},{"prompt":""},{"prompt":"@图片10"},{"request_id":""}):
            code,body=self.post("generate",self.data(**changes));self.assertEqual(code,400,body)
        self.thread.assert_not_called()

    def test_full_source_reuse_and_restart_restore_do_not_crop_to_15_seconds(self):
        with patch("wardrobe_continuation.inspect_video",return_value=SimpleNamespace(duration=40)):
            code,body=self.post("prepare",{"source_job_id":self.job.id,"keep_seconds":"25"})
        self.assertEqual(code,202,body);new=web_app.JOBS[body["id"]]
        self.assertEqual(Path(self.service.data(new)["source"]).read_bytes(),b"full original beyond 15s")
        self.assertEqual(body["settings"]["source_duration"],40)
        web_app.JOBS.pop(self.job.id)
        restored=self.service.load(self.job.id)
        self.assertEqual(self.service.data(restored)["keep_seconds"],25)
        with self.client.get(f"/api/wardrobe-continuation/jobs/{restored.id}/file/source") as response:self.assertEqual(response.status_code,200)
        with self.client.get(f"/api/wardrobe-continuation/jobs/{restored.id}/file/config") as response:self.assertEqual(response.status_code,404)

    def test_recover_downloaded_suffix_skips_model_submission(self):
        code,_=self.post("generate",self.data());self.assertEqual(code,202)
        data=self.service.data(self.job);key=data["active_attempt"];folder=self.job.run_dir/"attempts"/key
        tail=folder/"generated.mp4";tail.write_bytes(b"generated")
        (folder/"job.json").write_text(json.dumps({"task_id":"existing-task","output":str(tail),"status":"succeeded"}),encoding="utf-8")
        self.job.status="failed"
        code,body=self.post("recover",{"source_job_id":self.job.id});self.assertEqual(code,202,body)
        with patch("web_app.api_client") as client,patch("web_app.resume_cloud_job") as resume,patch.object(self.service,"finish") as finish:
            self.thread.call_args.kwargs["target"]()
            client.assert_not_called();resume.assert_not_called();self.assertEqual(finish.call_args.args[1].output_path,tail)


if __name__=="__main__":unittest.main()
