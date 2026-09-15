import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_wardrobe_rewrite_sources as materials
import web_app
from wardrobe_full_rewrite import full_rewrite_prompt
from workflow_core import WorkflowError


class FullRewriteTests(unittest.TestCase):
    setUp=materials.RewriteMaterialTests.setUp
    form=materials.RewriteMaterialTests.form
    post=materials.RewriteMaterialTests.post
    use_workflow=materials.RewriteMaterialTests.use_workflow
    create_success=materials.RewriteMaterialTests.create_success

    def full_form(self,**changes):
        return self.form(generation_mode='full_video',ranges_reviewed='false',**changes)

    def validate_source(self):
        return patch('wardrobe_full_rewrite.validate_seedance_reference_video',return_value=SimpleNamespace(duration=10,fps=30))

    def test_one_full_reference_request_without_prepare_trim_or_stitch(self):
        data=self.use_workflow('library');data['segments']=[]
        with self.validate_source(),patch.object(web_app.wardrobe_object,'asset'):
            code,body=self.post('generate',self.full_form())
        self.assertEqual(code,202,body);self.assertEqual(body['generation_mode'],'full_video')
        self.assertEqual(len(data['attempts'][data['active_attempt']]['segments']),1)
        with patch('web_app.run_generation',side_effect=self.create_success) as run,patch('wardrobe_intervals.prepare_intervals') as prepare,patch('wardrobe_intervals.assemble_intervals') as stitch,patch('web_app.conform_video_duration') as trim:
            self.parent.full.run(self.job,data['active_attempt'])
        run.assert_called_once();prepare.assert_not_called();trim.assert_not_called();stitch.assert_not_called()
        call=run.call_args.kwargs
        self.assertEqual(call['depth_reference'],'asset://asset-video123456');self.assertIsNone(call['depth_path'])
        self.assertEqual(call['options']['duration'],-1)
        self.assertIn('2.000–4.000 秒',call['options']['prompt']);self.assertIn('7.000–9.000 秒',call['options']['prompt'])
        self.assertEqual(self.job.output_path.read_bytes(),b'generated')
        calls=self.thread.call_count
        code,body=self.post('generate',self.full_form());self.assertEqual(code,202,body);self.assertEqual(self.thread.call_count,calls)

    def test_masked_workflow_uses_full_mosaic_actor_clothing_and_optional_image(self):
        data=self.use_workflow('references');data['segments']=[]
        png=Path(self.segments[0]['start_frame_image']).read_bytes()
        form=self.full_form(mosaic_reviewed='true',person_asset='asset://asset-person123456',clothing_image=(io.BytesIO(png),'clothing.png'),
                            extra_references='[{"upload":0}]',extra_images=(io.BytesIO(png),'object.png'))
        with self.validate_source(),patch.object(web_app.wardrobe_object,'asset'):
            code,body=self.post('generate',form)
        self.assertEqual(code,202,body)
        with patch('web_app.run_generation',side_effect=self.create_success) as run,patch('web_app.conform_video_duration') as trim:
            self.parent.full.run(self.job,data['active_attempt'])
        self.assertEqual(str(run.call_args.kwargs['depth_path']),data['mosaic'])
        self.assertEqual(len(run.call_args.kwargs['reference_images']),3);trim.assert_not_called()

    def test_invalid_times_or_materials_do_not_submit(self):
        self.use_workflow('library')
        for changes in [dict(paid_confirmed='false'),dict(segments='[]'),dict(segments=json.dumps([dict(start=0,end=2,prompt='@图片1')])),
                        dict(segments=json.dumps([dict(start=0,end=2,prompt='a'*1001)]))]:
            with self.validate_source(),patch.object(web_app.wardrobe_object,'asset'):
                code,body=self.post('generate',self.full_form(**changes))
            self.assertEqual(code,400,body)
        with patch('wardrobe_full_rewrite.validate_seedance_reference_video',side_effect=WorkflowError('完整原片超过 15 秒')):
            code,body=self.post('generate',self.full_form())
        self.assertEqual(code,400,body);self.thread.assert_not_called()

    def test_recovery_returns_full_output_without_another_submission_or_trim(self):
        data=self.use_workflow('library')
        with self.validate_source(),patch.object(web_app.wardrobe_object,'asset'):
            code,body=self.post('generate',self.full_form())
        self.assertEqual(code,202,body)
        key=data['active_attempt'];entry=data['attempts'][key]['segments'][0]
        child=self.parent.intervals.make_child(self.job,entry);entry['status']='failed';self.job.status='failed'
        web_app.save_shot_manifest(child.run_dir/'job.json',dict(status='ambiguous',submitted_at=100,duration=-1))
        with self.validate_source():
            code,body=self.post('generate',self.full_form(request_id='full-second-request-0001'))
        self.assertEqual(code,400,body)
        output=child.run_dir/'original_output.mp4';output.write_bytes(b'unchanged model output')
        web_app.save_shot_manifest(child.run_dir/'job.json',dict(status='succeeded',output=str(output),task_id='known'))
        code,body=self.post('recover',{'source_job_id':self.job.id});self.assertEqual(code,202,body)
        with patch('web_app.run_generation') as run,patch('web_app.conform_video_duration') as trim,patch('wardrobe_intervals.assemble_intervals') as stitch:
            self.thread.call_args.kwargs['target']()
        run.assert_not_called();trim.assert_not_called();stitch.assert_not_called()
        self.assertEqual(self.job.output_path,output);self.assertEqual(self.job.status,'succeeded')


if __name__=='__main__':unittest.main()
