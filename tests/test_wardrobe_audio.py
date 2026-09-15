import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import numpy as np
import web_app as host
import wardrobe_audio
import inline_cast
from video_proxy_rules import PROXY_DIALOGUE, FINAL_DIALOGUE
from wardrobe_dynamic import DYNAMIC_WHITE_PROMPT, DYNAMIC_FINAL_PROMPT
from workflow_core import resolve_ffmpeg, inspect_video
from long_video_core import video_has_audio


class WardrobeAudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.runs=self.root/'runs';self.runs.mkdir()
        for mock in (patch.object(host,'PROJECT_DIR',self.root),patch.dict(host.JOBS,{},clear=True)):
            mock.start();self.addCleanup(mock.stop)
        self.parent_dir=self.runs/'parent';self.parent_dir.mkdir()

    def video(self,path,frequency=None,duration=3,audio_duration=None):
        args=[str(resolve_ffmpeg()),'-hide_banner','-loglevel','error','-y','-f','lavfi','-i',f'color=c=blue:s=160x90:r=30:d={duration}']
        if frequency: args+=['-f','lavfi','-i',f'sine=frequency={frequency}:duration={audio_duration or duration}']
        args+=['-t',str(duration),'-c:v','libx264','-pix_fmt','yuv420p']
        if frequency: args+=['-c:a','aac']
        subprocess.run(args+[str(path)],check=True,capture_output=True)
        return path

    def tone(self,path,at=.5):
        result=subprocess.run([str(resolve_ffmpeg()),'-hide_banner','-loglevel','error','-ss',str(at),'-i',str(path),'-t','0.25','-vn','-ac','1','-ar','8000','-f','f32le','pipe:1'],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8',errors='replace'))
        samples=np.frombuffer(result.stdout,dtype=np.float32)
        return np.fft.rfftfreq(len(samples),1/8000)[np.abs(np.fft.rfft(samples)).argmax()]

    def test_white_pipeline_submits_dialogue_and_keeps_original_audio_not_generated_tone(self):
        original=self.video(self.parent_dir/'reference.mp4',440)
        silent=self.video(self.parent_dir/'masked.mp4')
        generated=self.video(self.parent_dir/'cloud.mp4',880,5)
        job=host.WebJob(id='voice-parent',kind='wardrobe_prepare_dynamic',run_dir=self.parent_dir,mosaic_path=silent)
        unchanged=original.read_bytes()
        def generate(child,**kwargs):
            self.assertTrue(kwargs['options']['generate_audio'])
            self.assertIn(PROXY_DIALOGUE,kwargs['options']['prompt'])
            self.assertTrue(video_has_audio(kwargs['depth_path']))
            self.assertAlmostEqual(self.tone(kwargs['depth_path']),440,delta=5)
            self.assertAlmostEqual(inspect_video(kwargs['depth_path']).duration,5,delta=.06)
            child.output_path=generated;child.status='succeeded'
        with patch.object(host,'run_generation',side_effect=generate),patch.object(host,'sanitize_video_visible_text_if_needed',side_effect=lambda src,*a,**kw:(src,{})):
            output=host.run_wardrobe_white_model(job,original,prompt_override=DYNAMIC_WHITE_PROMPT)
        self.assertAlmostEqual(inspect_video(output).duration,3,delta=.06)
        self.assertAlmostEqual(self.tone(output),440,delta=5)
        self.assertEqual(original.read_bytes(),unchanged)

    def test_legacy_silent_master_is_repaired_before_submit_and_after_recovery(self):
        original=self.video(self.parent_dir/'reference.mp4',440,audio_duration=1)
        white=self.video(self.parent_dir/'white.mp4')
        parent=host.WebJob(id='audio-parent',kind='wardrobe_prepare_dynamic',run_dir=self.parent_dir,white_model_path=white)
        host.JOBS[parent.id]=parent
        folder=self.runs/'final';folder.mkdir()
        job=host.WebJob(id='audio-final',kind='wardrobe_generate_dynamic',run_dir=folder,white_model_path=white)
        host.persist_wardrobe_swap_job(job,'dynamic',source_job_id=parent.id)
        options=dict(prompt='更换人物',generate_audio=True,model=host.DEFAULT_SEEDANCE_25_MODEL,duration=-1)
        reference,options=wardrobe_audio.prepare(host,job,white,'',options)
        self.assertIn(FINAL_DIALOGUE,options['prompt'])
        self.assertAlmostEqual(inspect_video(reference).duration,3,delta=.06,msg='audio ending early must not truncate video')
        self.assertAlmostEqual(self.tone(reference),440,delta=5)
        padded,options=host.prepare_wardrobe_final_timing(job,reference,'',options)
        self.assertAlmostEqual(inspect_video(padded).duration,5,delta=.06)
        host.persist_cloud_job(job,status='running')
        restored=host.WebJob(id=job.id,kind=job.kind,run_dir=folder)
        host.persist_cloud_job(restored,status='running')
        cloud=self.video(folder/'download.mp4',880,5)
        trimmed=host.finish_wardrobe_final_timing(restored,cloud)
        output=wardrobe_audio.finish(host,restored,trimmed)
        self.assertAlmostEqual(inspect_video(output).duration,3,delta=.06)
        self.assertAlmostEqual(self.tone(output),440,delta=5)

    def test_free_repair_is_local_and_silent_original_stays_silent(self):
        original=self.video(self.parent_dir/'reference.mp4',440)
        white=self.video(self.parent_dir/'white.mp4')
        final=self.video(self.parent_dir/'final.mp4',880)
        job=host.WebJob(id='repair-parent',kind='wardrobe_prepare_dynamic',project=host.WARDROBE_SWAP_PROJECT,run_dir=self.parent_dir,status='succeeded',white_model_path=white,output_path=final)
        host.JOBS[job.id]=job
        with patch.object(host,'api_client') as client:
            response=host.app.test_client().post('/api/wardrobe-swap/restore-audio',data=dict(source_job_id=job.id))
            self.assertEqual(response.status_code,200,response.get_json())
            client.assert_not_called()
        self.assertAlmostEqual(self.tone(job.white_model_path),440,delta=5)
        self.assertAlmostEqual(self.tone(job.output_path),440,delta=5)
        self.assertTrue(white.is_file());self.assertTrue(final.is_file())
        silent=self.video(self.parent_dir/'silent.mp4')
        result=wardrobe_audio.attach(final,silent,self.parent_dir/'silent-result.mp4')
        self.assertFalse(video_has_audio(result))

    def test_single_and_multi_prompt_previews_include_speaker_and_lip_rules(self):
        self.assertIn(FINAL_DIALOGUE,DYNAMIC_FINAL_PROMPT)
        for mode in ('dynamic','dynamic_object','dynamic_scene'):
            result=inline_cast.prompt_preview(host,dict(mode=mode,first_clothing=False,has_replacement=mode=='dynamic_scene',people=[dict(source='右侧人物')],custom='保持表演'))
            self.assertFalse(result['white_error']);self.assertFalse(result['final_error'])
            self.assertIn(PROXY_DIALOGUE,result['white_prompt'])
            self.assertIn(FINAL_DIALOGUE,result['final_prompt'])


if __name__=='__main__':unittest.main()
