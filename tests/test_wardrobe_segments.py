import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import web_app as host
import wardrobe_segments as segments
from workflow_core import resolve_ffmpeg, inspect_video
from long_video_core import pad_video_with_trailing_black, video_has_audio


class WardrobeSegmentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.runs=self.root/'runs';self.runs.mkdir()
        for mock in (patch.object(host,'PROJECT_DIR',self.root),patch.dict(host.JOBS,{},clear=True),
                     patch.object(host,'timestamped_run_dir',side_effect=self.directory)):
            mock.start();self.addCleanup(mock.stop)

    def directory(self,suffix):
        path=self.runs/('20260915_'+suffix);path.mkdir(parents=True,exist_ok=True);return path

    def video(self,path,duration=3,audio=True):
        args=[str(resolve_ffmpeg()),'-hide_banner','-loglevel','error','-y','-f','lavfi','-i',f'color=c=red:s=160x90:r=30:d={duration}']
        if audio:args+=['-f','lavfi','-i',f'sine=frequency=440:duration={duration}']
        args+=['-t',str(duration),'-c:v','libx264','-pix_fmt','yuv420p']
        if audio:args+=['-c:a','aac']
        subprocess.run(args+[str(path)],check=True,capture_output=True)
        return path

    def frame(self,path,time):
        cap=cv2.VideoCapture(str(path));cap.set(cv2.CAP_PROP_POS_MSEC,time*1000);ok,frame=cap.read();cap.release()
        self.assertTrue(ok);return frame

    def test_plan_has_no_gaps_or_lost_short_tail(self):
        for duration,expected in [(29,[15,14]),(31,[15,15,1]),(3,[3]),(30,[15,15])]:
            result=segments.plan(duration)
            self.assertEqual([s['duration'] for s in result],expected)
            self.assertEqual(result[-1]['end'],duration)
            self.assertTrue(all(a['end']==b['start'] for a,b in zip(result,result[1:])))

    def test_real_short_input_gets_black_tail_and_original_timing_after_restore(self):
        for seconds in (1.2,3):
            folder=self.root/str(seconds);folder.mkdir()
            source=self.video(folder/'source.mp4',seconds)
            before=source.read_bytes()
            job=host.WebJob(id='timing-test',kind='wardrobe_generate_dynamic',run_dir=folder)
            options=dict(model=host.DEFAULT_SEEDANCE_25_MODEL,prompt='按原片动作制作',duration=-1)
            padded,updated=host.prepare_wardrobe_final_timing(job,source,'',options)
            self.assertAlmostEqual(inspect_video(padded).duration,5,delta=.05)
            self.assertGreater(self.frame(padded,.5)[:,:,2].mean(),200)
            self.assertLess(self.frame(padded,4.5).mean(),2)
            self.assertTrue(video_has_audio(padded))
            self.assertIn('纯黑静音补时区间',updated['prompt'])
            self.assertEqual(updated['duration'],-1)
            host.persist_cloud_job(job,status='running')
            recovered=host.WebJob(id=job.id,kind=job.kind,run_dir=folder)
            host.persist_cloud_job(recovered,status='running')
            result=host.finish_wardrobe_final_timing(recovered,padded)
            self.assertAlmostEqual(inspect_video(result).duration,seconds,delta=.05)
            self.assertTrue(video_has_audio(result));self.assertEqual(source.read_bytes(),before)

    def test_real_29_second_upload_restores_all_segments_and_joins_in_order(self):
        job=host.new_job('wardrobe_prepare_dynamic')
        job.cast_continuity['wardrobe_dynamic']={'description':'','requests':{},'mosaic_scope':'all_faces'}
        source=self.video(job.run_dir/'reference.mp4',29)
        original=source.read_bytes()
        first=host.normalize_wardrobe_source_duration(job,source)
        group=segments.public(host,job)
        self.assertEqual([s['duration'] for s in group['segments']],[15,14])
        self.assertAlmostEqual(inspect_video(first).duration,15,delta=.05)
        second_id=group['segments'][1]['job_id'];host.JOBS.pop(second_id)
        second=host.restore_wardrobe_swap_job(second_id)
        self.assertIsNotNone(second)
        self.assertAlmostEqual(inspect_video(host.wardrobe_prepare_source(second)).duration,14,delta=.05)
        # A distinct second output proves the join includes the tail in order.
        blue=second.run_dir/'blue.mp4'
        subprocess.run([str(resolve_ffmpeg()),'-hide_banner','-loglevel','error','-y','-f','lavfi','-i','color=c=blue:s=320x180:r=30:d=14','-c:v','libx264','-pix_fmt','yuv420p',str(blue)],check=True,capture_output=True)
        segments.record_result(host,job,first)
        self.assertIsNone(segments.assemble(host,job))
        segments.record_result(host,second,blue)
        output=segments.assemble(host,second)
        self.assertAlmostEqual(inspect_video(output).duration,29,delta=.08)
        self.assertGreater(self.frame(output,2)[:,:,2].mean(),200)
        self.assertGreater(self.frame(output,20)[:,:,0].mean(),200)
        self.assertTrue(video_has_audio(output),'silent segments must not drop earlier audio')
        self.assertEqual(source.read_bytes(),original)
        self.assertTrue(segments.public(host,second)['output_url'])
        self.assertEqual(segments.assemble(host,second),output)
        replacement=host.new_job(second.kind)
        shutil.copyfile(host.wardrobe_prepare_source(second),replacement.run_dir/'reference.mp4')
        segments.replace_member(host,second,replacement)
        self.assertEqual(segments.public(host,replacement)['segments'][1]['job_id'],replacement.id)
        self.assertEqual(segments.public(host,replacement)['output_url'],'')
        segments.record_result(host,second,blue)
        self.assertFalse(segments.public(host,replacement)['segments'][1]['finished'],'late results cannot overwrite a newer attempt')

    def test_white_model_short_reference_uses_black_padding_and_five_seconds(self):
        source=self.video(self.root/'short.mp4',1.2,False)
        job=host.WebJob(id='white-short',kind='wardrobe_prepare_dynamic',run_dir=self.root,mosaic_path=source)
        def generate(child,**kwargs):
            self.assertEqual(kwargs['options']['duration'],5)
            self.assertAlmostEqual(inspect_video(kwargs['depth_path']).duration,5,delta=.05)
            self.assertLess(self.frame(kwargs['depth_path'],4.5).mean(),2)
            self.assertNotIn('结束姿势定格',kwargs['options']['prompt'])
            child.output_path=kwargs['depth_path'];child.status='succeeded'
        with patch.object(host,'run_generation',side_effect=generate),patch.object(host,'sanitize_video_visible_text_if_needed',side_effect=lambda src,*a,**k:(src,{})):
            output=host.run_wardrobe_white_model(job,source)
        self.assertAlmostEqual(inspect_video(output).duration,1.2,delta=.05)


if __name__=='__main__':unittest.main()
