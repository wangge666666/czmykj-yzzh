import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

import cv2
import numpy as np
import web_app
from wardrobe_intervals import normalize_segments, interval_prompt, prepare_intervals, assemble_intervals
from wardrobe_continuation import ffmpeg
from workflow_core import WorkflowError, inspect_video, resolve_ffmpeg


class IntervalMediaTests(unittest.TestCase):
    def test_start_middle_end_and_original_gaps_keep_frames_audio_and_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'source.mp4'; replacement=root/'blue.mp4'
            ffmpeg(['-f','lavfi','-i','testsrc2=size=320x240:rate=30:duration=8','-f','lavfi','-i','sine=frequency=440:duration=8','-c:v','libx264','-crf','16','-c:a','aac',str(source)])
            ffmpeg(['-f','lavfi','-i','color=blue:size=240x320:rate=24:duration=4','-f','lavfi','-i','sine=frequency=880:duration=4','-c:v','libx264','-c:a','aac',str(replacement)])
            digest=hashlib.sha256(source.read_bytes()).digest()
            raw=[{'start':0,'end':.5},{'start':2,'end':4},{'start':6,'end':8}]
            segments=normalize_segments(raw,8,30)
            output=assemble_intervals(source,[{**s,'output':str(replacement)} for s in segments],root/'output.mp4')
            self.assertAlmostEqual(inspect_video(output).duration,8,delta=.08)
            caps=[cv2.VideoCapture(str(path)) for path in (source,output)]
            try:
                for index in range(240):
                    frames=[cap.read()[1] for cap in caps]
                    self.assertTrue(all(frame is not None for frame in frames), index)
                    if 15<=index<60 or 120<=index<180:
                        self.assertLess(np.abs(frames[0].astype(float)-frames[1]).mean(),4,f'original frame {index}')
                    else:
                        self.assertGreater(frames[1][100:140,140:180,0].mean(),220,f'rewritten frame {index}')
            finally:
                for cap in caps:cap.release()
            raw_audio=subprocess.run([str(resolve_ffmpeg()),'-v','error','-i',str(output),'-map','0:a:0','-ac','1','-ar','8000','-f','f32le','-'],capture_output=True,check=True).stdout
            audio=np.frombuffer(raw_audio,np.float32)
            for start,frequency in ((.1,880),(1,440),(2.5,880),(5,440),(7,880)):
                sample=audio[int(start*8000):int((start+.2)*8000)]
                peak=np.fft.rfftfreq(len(sample),1/8000)[np.argmax(abs(np.fft.rfft(sample)))]
                self.assertAlmostEqual(peak,frequency,delta=5)
            self.assertEqual(hashlib.sha256(source.read_bytes()).digest(),digest)
            prepared=prepare_intervals(source,root,segments,web_app)
            self.assertTrue(all(Path(s['start_frame_image']).is_file() for s in prepared))
            self.assertTrue(Path(prepared[1]['end_frame_image']).is_file())
            self.assertNotIn('end_frame_image',prepared[-1])
            self.assertTrue(all(2<=inspect_video(Path(s['context'])).duration<=15.1 for s in prepared))

    def test_full_rewrite_and_silent_segment_have_correct_length(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); source=root/'source.mp4'; replacement=root/'silent.mp4'
            ffmpeg(['-f','lavfi','-i','color=red:size=320x240:rate=25:duration=2','-c:v','libx264',str(source)])
            ffmpeg(['-f','lavfi','-i','color=blue:size=320x240:rate=30:duration=4','-c:v','libx264',str(replacement)])
            seg=normalize_segments([{'start':0,'end':2}],2,25)
            output=assemble_intervals(source,[{**seg[0],'output':str(replacement)}],root/'out.mp4')
            self.assertAlmostEqual(inspect_video(output).duration,2,delta=.08)

    def test_validation_and_frame_alignment(self):
        invalid=[[],[{'start':2,'end':4},{'start':3,'end':5}],[{'start':-1,'end':2}],
                 [{'start':2,'end':2}],[{'start':0,'end':16}],[{'start':39,'end':41}],
                 [{'start':'nan','end':3}],[{'start':1,'end':float('inf')}],[{'start':1,'end':1.001}]]
        for raw in invalid:
            with self.subTest(raw=raw),self.assertRaises(WorkflowError):normalize_segments(raw,40,30)
        result=normalize_segments([{'start':7,'end':9},{'start':2.005,'end':4.002}],40,30)
        self.assertEqual([(s['start'],s['end']) for s in result],[(2,4),(7,9)])
        self.assertEqual(result[0]['generation_seconds'],4)
        with self.assertRaises(WorkflowError):normalize_segments([{'start':0,'end':1}],40,30,require_prompts=True)

    def test_prompt_binds_boundaries_and_short_generation_padding(self):
        segment=normalize_segments([{'start':2,'end':4,'prompt':'人物转身看向 @ 图片 2'}],8,30)[0]
        prompt=interval_prompt(segment)
        self.assertIn('@图片2',prompt);self.assertIn('输出前 2.0000 秒',prompt)
        self.assertIn('2.0000–4.0000',prompt)
        for custom in ('@视频2','@图片3','@未知'):
            with self.assertRaises(WorkflowError):interval_prompt({**segment,'prompt':custom})
        with self.assertRaises(WorkflowError):interval_prompt({**segment,'has_after':False,'prompt':'@图片2'})


class IntervalAPITests(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.root=Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name,value in (('web_app.PROJECT_DIR',self.root),('workflow_core.PROJECT_DIR',self.root),('workflow_core.RUNS_DIR',self.root/'runs')):
            self.stack.enter_context(patch(name,value))
        self.stack.enter_context(patch.dict(web_app.JOBS,{},clear=True))
        self.thread=self.stack.enter_context(patch('wardrobe_intervals.threading.Thread'))
        self.parent=web_app.wardrobe_continuation;self.service=self.parent.intervals;self.client=web_app.app.test_client()
        self.job=web_app.new_job('wardrobe_continuation_prepare');self.job.status='succeeded'
        source=self.job.run_dir/'reference.mp4';source.write_bytes(b'full source')
        self.segments=normalize_segments([{'start':2,'end':4,'prompt':'人物转身'},{'start':7,'end':9,'prompt':'拿起杯子'}],40,30)
        for i,segment in enumerate(self.segments):
            folder=self.job.run_dir/'segments'/str(i);folder.mkdir(parents=True)
            context=folder/'context.mp4';context.write_bytes(b'context');segment['context']=str(context)
            for kind in ('start_frame_image','end_frame_image'):
                ok,buffer=cv2.imencode('.png',np.full((480,640,3),120,np.uint8));self.assertTrue(ok)
                image=folder/(kind+'.png');image.write_bytes(buffer.tobytes());segment[kind]=str(image)
        self.job.cast_continuity['continuation']={'workflow_version':2,'source':str(source),'source_duration':40,'fps':30,'segments':self.segments,'attempts':{}}
        self.parent.save(self.job)

    def form(self,**changes):
        return {'source_job_id':self.job.id,'segments':json.dumps(self.segments),'resolution':'720p','generate_audio':'true',
                'ranges_reviewed':'true','paid_confirmed':'true','request_id':'interval-generation-00001',**changes}

    def post(self,route,data):
        response=self.client.post('/api/wardrobe-continuation/'+route,data=data)
        return response.status_code,response.get_json()

    def create_success(self,child,**kwargs):
        child.output_path=child.run_dir/'generated.mp4';child.output_path.write_bytes(b'generated');child.status='succeeded'

    def test_each_interval_uses_own_context_and_frames_with_idempotent_batch(self):
        code,body=self.post('generate',self.form());self.assertEqual(code,202,body)
        self.assertEqual(len(body['segments']),2)
        with patch('web_app.run_generation',side_effect=self.create_success) as run,patch('web_app.conform_video_duration',side_effect=lambda path,*a,**kw:path),patch('wardrobe_intervals.has_audio',return_value=False),patch('wardrobe_intervals.assemble_intervals') as assemble:
            self.service.run(self.job,'interval-generation-00001')
            self.assertEqual(run.call_count,2)
            for i,call in enumerate(run.call_args_list):
                kwargs=call.kwargs
                self.assertEqual(str(kwargs['depth_path']),self.segments[i]['context'])
                self.assertEqual(kwargs['reference_images'],[self.segments[i]['start_frame_image'],self.segments[i]['end_frame_image']])
                self.assertEqual(kwargs['options']['duration'],4)
                self.assertIn(self.segments[i]['prompt'],kwargs['options']['prompt'])
            self.assertEqual(len(assemble.call_args.args[1]),2)
        calls=self.thread.call_count
        code,again=self.post('generate',self.form());self.assertEqual(code,202);self.assertEqual(again['id'],body['id']);self.assertEqual(self.thread.call_count,calls)

    def test_invalid_or_changed_ranges_never_submit(self):
        for change in ({'paid_confirmed':'false'},{'ranges_reviewed':'false'},{'segments':'not-json'},
                       {'segments':json.dumps([{'start':2,'end':4,'prompt':''}])},
                       {'segments':json.dumps([{'start':2,'end':4,'prompt':'转身'},{'start':3,'end':5,'prompt':'离开'}])},
                       {'segments':json.dumps([{**s,'prompt':'@图片3'} for s in self.segments])},
                       {'segments':json.dumps([{**self.segments[0],'start':1},self.segments[1]])}):
            code,body=self.post('generate',self.form(**change));self.assertEqual(code,400,body)
        self.thread.assert_not_called()

    def test_prepare_full_source_restore_and_safe_segment_files(self):
        with patch('wardrobe_intervals.inspect_video',return_value=SimpleNamespace(duration=40,fps=30)):
            code,body=self.post('prepare',{'source_job_id':self.job.id,'segments':json.dumps([{'start':0,'end':2},{'start':38,'end':40}])})
        self.assertEqual(code,202,body)
        self.assertEqual(Path(self.parent.data(web_app.JOBS[body['id']])['source']).read_bytes(),b'full source')
        web_app.JOBS.pop(self.job.id)
        restored=self.parent.load(self.job.id)
        self.assertEqual(self.parent.public(restored)['workflow_version'],2)
        for path,status in (('segments/0/file/start_frame_image',200),('segments/0/file/source',404),('segments/9/file/context',404)):
            response=self.client.get(f'/api/wardrobe-continuation/jobs/{self.job.id}/'+path);self.assertEqual(response.status_code,status);response.close()
        self.parent.data(restored)['segments'][0]['context']=str(self.root/'outside.txt');(self.root/'outside.txt').write_text('outside')
        response=self.client.get(f'/api/wardrobe-continuation/jobs/{self.job.id}/segments/0/file/context');self.assertEqual(response.status_code,404)

    def test_partial_failure_reuses_success_and_generates_only_remaining(self):
        code,body=self.post('generate',self.form());self.assertEqual(code,202,body)
        calls=[]
        def run(child,**kwargs):
            calls.append(child)
            if len(calls)==2:child.status='failed';child.error='reference rejected'
            else:self.create_success(child)
        with patch('web_app.run_generation',side_effect=run),patch('web_app.conform_video_duration',side_effect=lambda path,*a,**kw:path),patch('wardrobe_intervals.has_audio',return_value=False):
            self.service.run(self.job,'interval-generation-00001')
        self.assertEqual(self.job.status,'failed');self.assertEqual(len(calls),2)
        code,body=self.post('generate',self.form(request_id='interval-generation-00002'));self.assertEqual(code,202,body)
        self.assertTrue(body['segments'][0]['reusable'])
        with patch('web_app.run_generation',side_effect=self.create_success) as run,patch('web_app.conform_video_duration',side_effect=lambda path,*a,**kw:path),patch('wardrobe_intervals.has_audio',return_value=False),patch('wardrobe_intervals.assemble_intervals'):
            self.service.run(self.job,'interval-generation-00002');self.assertEqual(run.call_count,1)

    def test_ambiguous_segment_requires_recovery_and_never_resubmits_or_starts_pending(self):
        code,body=self.post('generate',self.form());self.assertEqual(code,202,body)
        data=self.parent.data(self.job);entry=data['attempts'][data['active_attempt']]['segments'][0]
        child=self.service.make_child(self.job,entry);entry['status']='failed';self.job.status='failed'
        cloud={'status':'ambiguous','submitted_at':100,'known_task_ids':[],'duration':4}
        web_app.save_shot_manifest(child.run_dir/'job.json',cloud)
        code,body=self.post('generate',self.form(request_id='interval-generation-00002'));self.assertEqual(code,400,body)
        self.assertTrue(self.parent.public(self.job)['needs_recovery'])
        output=child.run_dir/'generated.mp4';output.write_bytes(b'generated')
        web_app.save_shot_manifest(child.run_dir/'job.json',{'status':'succeeded','output':str(output),'task_id':'known'})
        code,body=self.post('recover',{'source_job_id':self.job.id});self.assertEqual(code,202,body)
        with patch('web_app.run_generation') as run,patch('web_app.api_client') as client,patch('web_app.resume_cloud_job') as resume,patch('web_app.conform_video_duration',return_value=output),patch('wardrobe_intervals.has_audio',return_value=False):
            self.thread.call_args.kwargs['target']()
            run.assert_not_called();client.assert_not_called();resume.assert_not_called()
        self.assertEqual(entry['status'],'succeeded')
        self.assertEqual(data['attempts'][data['active_attempt']]['segments'][1]['status'],'pending')
        self.assertFalse(self.parent.public(self.job)['needs_recovery'])


if __name__=='__main__':unittest.main()
