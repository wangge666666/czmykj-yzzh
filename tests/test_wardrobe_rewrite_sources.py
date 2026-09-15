import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_wardrobe_intervals as base
import web_app
from wardrobe_rewrite_sources import rewrite_prompt
from workflow_core import WorkflowError


class RewriteMaterialTests(unittest.TestCase):
    setUp = base.IntervalAPITests.setUp
    form = base.IntervalAPITests.form
    post = base.IntervalAPITests.post
    create_success = base.IntervalAPITests.create_success

    def use_workflow(self, workflow):
        data = self.parent.data(self.job)
        data['rewrite_workflow'] = workflow
        if workflow == 'references':
            mosaic = self.job.run_dir/'masked.mp4'; mosaic.write_bytes(b'masked source')
            data['mosaic'] = str(mosaic)
        else:
            data.update(video_asset='asset://asset-video123456', video_name='original', video_group_id='group-123456789')
        return data

    def actor_form(self, **changes):
        png = Path(self.segments[0]['start_frame_image']).read_bytes()
        return self.form(mosaic_reviewed='true', person_asset='asset://asset-person123456',
                         clothing_image=(io.BytesIO(png), 'clothing.png'), **changes)

    def test_direct_reference_uses_asset_and_no_original_face_images(self):
        self.use_workflow('library')
        segments = [{**s, 'prompt':'参考 @视频1 改写动作'} for s in self.segments]
        with patch.object(web_app.wardrobe_object, 'asset') as asset:
            code, body = self.post('generate', self.form(segments=json.dumps(segments)))
        self.assertEqual(code, 202, body); asset.assert_called_once_with('asset://asset-video123456', 'Video')
        with patch('web_app.run_generation', side_effect=self.create_success) as run, patch('web_app.conform_video_duration', side_effect=lambda path,*a,**kw:path), patch('wardrobe_intervals.has_audio', return_value=False), patch('wardrobe_intervals.assemble_intervals'):
            self.service.run(self.job, 'interval-generation-00001')
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['depth_reference'], 'asset://asset-video123456')
            self.assertIsNone(call.kwargs['depth_path']); self.assertIsNone(call.kwargs['reference_images'])
            self.assertTrue(call.kwargs['video_only'])

    def test_reference_binding_and_recovery_snapshot(self):
        data = self.use_workflow('references')
        with patch.object(web_app.wardrobe_object, 'asset'):
            code, body = self.post('generate', self.actor_form())
        self.assertEqual(code, 202, body)
        images = data['attempts']['interval-generation-00001']['images']
        self.assertEqual(images[0], 'asset://asset-person123456')
        self.assertTrue(Path(images[1]).is_file()); self.assertIn('/file/clothing', body['clothing_url'])
        with patch('web_app.run_generation', side_effect=self.create_success) as run, patch('web_app.conform_video_duration', side_effect=lambda path,*a,**kw:path), patch('wardrobe_intervals.has_audio', return_value=False), patch('wardrobe_intervals.assemble_intervals'):
            self.service.run(self.job, 'interval-generation-00001')
        for i, call in enumerate(run.call_args_list):
            self.assertEqual(call.kwargs['reference_images'], images)
            self.assertEqual(str(call.kwargs['depth_path']), self.segments[i]['context'])
            self.assertEqual(call.kwargs['depth_reference'], '')
            self.assertIn('@图片1唯一提供人物身份', call.kwargs['options']['prompt'])

    def test_missing_or_wrong_materials_rejected_before_generation(self):
        self.use_workflow('references')
        with patch.object(web_app.wardrobe_object, 'asset') as asset:
            for data in [self.form(), self.form(mosaic_reviewed='true', person_asset='asset://asset-person123456')]:
                code, body = self.post('generate', data); self.assertEqual(code, 400, body)
        self.thread.assert_not_called()
        for workflow, prompt in [('library','改写 @图片1'),('references','改写 @图片3')]:
            with self.assertRaises(WorkflowError): rewrite_prompt({**self.segments[0], 'prompt':prompt}, workflow)

    def test_prepare_uses_masked_source_and_preserves_original_for_assembly(self):
        data = self.use_workflow('references')
        form = {'source_job_id':self.job.id, 'segments':json.dumps(self.segments)}
        code, _ = self.post('prepare', form); self.assertEqual(code, 400)
        with patch('wardrobe_intervals.inspect_video', return_value=SimpleNamespace(duration=40,fps=30)):
            code, body = self.post('prepare', {**form, 'mosaic_reviewed':'true'})
        self.assertEqual(code, 202, body)
        prepared = web_app.JOBS[body['id']]
        new_data = self.parent.data(prepared)
        self.assertEqual(Path(new_data['source']).read_bytes(), b'full source')
        with patch('wardrobe_intervals.prepare_intervals', return_value=self.segments) as extract:
            self.thread.call_args.kwargs['target']()
        self.assertEqual(Path(extract.call_args.args[0]).read_bytes(), b'masked source')
        self.assertNotEqual(new_data['mosaic'], data['mosaic'])

    def test_new_library_source_downloads_for_splicing_without_generation(self):
        raw = {'URL':'https://example.test/original.mp4', 'Name':'original', 'GroupId':'group-123456789'}
        with patch.object(web_app.wardrobe_object, 'asset', return_value=raw):
            code, body = self.post('source', {'rewrite_workflow':'library', 'video_asset':'asset://asset-video123456'})
        self.assertEqual(code,202,body)
        with patch('web_app.download_file', side_effect=lambda url,path:Path(path).write_bytes(b'downloaded original')), patch('wardrobe_rewrite_sources.validate_seedance_reference_video', return_value=SimpleNamespace(duration=10,fps=30)), patch('web_app.run_generation') as generate:
            self.thread.call_args.kwargs['target']()
        generate.assert_not_called()
        job = web_app.JOBS[body['id']]
        self.assertEqual(job.status,'succeeded'); self.assertEqual(self.parent.public(job)['settings']['source_duration'],10)
        self.assertEqual(Path(self.parent.data(job)['source']).read_bytes(),b'downloaded original')

    def test_new_reference_source_masks_whole_original_with_threshold(self):
        code, body = self.post('source', {'rewrite_workflow':'references', 'reference_video':(io.BytesIO(b'original'), 'source.mp4'), 'face_score_threshold':'.45'})
        self.assertEqual(code,202,body)
        def mask(job, source, **kwargs):
            path=job.run_dir/'masked.mp4';path.write_bytes(b'masked');return path
        with patch('wardrobe_rewrite_sources.validate_seedance_reference_video', return_value=SimpleNamespace(duration=10,fps=30)), patch('web_app.run_wardrobe_face_mosaic', side_effect=mask) as mosaic, patch('web_app.run_generation') as generate:
            self.thread.call_args.kwargs['target']()
        generate.assert_not_called();self.assertEqual(mosaic.call_args.kwargs['score_threshold'],.45)
        self.assertEqual(mosaic.call_args.kwargs['max_source_seconds'],15)
        job=web_app.JOBS[body['id']];self.assertEqual(job.status,'succeeded');self.assertTrue(self.parent.public(job)['mosaic_url'])

    def test_reference_changes_do_not_reuse_previous_partial_clip(self):
        data = self.use_workflow('references')
        with patch.object(web_app.wardrobe_object,'asset'):
            code, body=self.post('generate',self.actor_form())
            self.assertEqual(code,202,body)
            attempt=data['attempts'][data['active_attempt']];attempt['segments'][0]['output']=self.segments[0]['context'];self.job.status='failed'
            code, body=self.post('generate',self.form(request_id='interval-generation-00002',mosaic_reviewed='true',person_asset='asset://asset-another123456'))
        self.assertEqual(code,202,body);self.assertFalse(body['segments'][0]['reusable'])

    def test_optional_images_submitted_in_visible_order_in_all_flows(self):
        png = Path(self.segments[0]['start_frame_image']).read_bytes()
        for workflow, first_number in [('library',1), ('references',3), ('legacy',3)]:
            with self.subTest(workflow=workflow):
                data = self.use_workflow(workflow)
                data['attempts'] = {}; data.pop('active_attempt', None); self.job.status='succeeded'
                key = 'extra-reference-'+workflow
                form = self.actor_form(request_id=key) if workflow=='references' else self.form(request_id=key)
                form.update(segments=json.dumps([{**s,'prompt':f'将物品改为 @图片{first_number}，颜色参考 @图片{first_number+1}'} for s in self.segments]),
                    extra_references=json.dumps([{'upload':0},{'upload':1}]),
                    extra_images=[(io.BytesIO(png),'物品.png'),(io.BytesIO(png),'场景.png')])
                with patch.object(web_app.wardrobe_object,'asset'):
                    code, body = self.post('generate',form)
                self.assertEqual(code,202,body);self.assertEqual([x['name'] for x in body['extra_references']],['物品.png','场景.png'])
                with patch('web_app.run_generation',side_effect=self.create_success) as run,patch('web_app.conform_video_duration',side_effect=lambda path,*a,**kw:path),patch('wardrobe_intervals.has_audio',return_value=False),patch('wardrobe_intervals.assemble_intervals'):
                    self.service.run(self.job,key)
                for call in run.call_args_list:
                    images=call.kwargs['reference_images']
                    self.assertEqual(len(images),2 if workflow=='library' else 4)
                    self.assertTrue(images[-2].endswith('extra_0.png'));self.assertTrue(images[-1].endswith('extra_1.png'))
                    if workflow=='library': self.assertFalse(call.kwargs['video_only'])

    def test_optional_images_restore_reuse_and_explicit_remove(self):
        data=self.use_workflow('library');png=Path(self.segments[0]['start_frame_image']).read_bytes()
        with patch.object(web_app.wardrobe_object,'asset'):
            code,body=self.post('generate',self.form(extra_references='[{"upload":0}]',extra_images=(io.BytesIO(png),'物品.png')))
            self.assertEqual(code,202,body);reference=body['extra_references'][0]
            response=self.client.get(reference['url']);self.assertEqual(response.status_code,200);self.assertEqual(response.data,png);response.close()
            entries=data['attempts'][data['active_attempt']]['segments'];entries[0]['output']=self.segments[0]['context'];self.job.status='failed'
            self.parent.save(self.job)
            restored=self.parent.public(self.parent.load(self.job.id));self.assertEqual(restored['extra_references'][0]['sha256'],reference['sha256'])
            keep=dict(keep=0,source_job_id=self.job.id,sha256=reference['sha256'])
            code,body=self.post('generate',self.form(request_id='keep-extra-reference-0002',extra_references=json.dumps([keep])))
            self.assertEqual(code,202,body);self.assertTrue(body['segments'][0]['reusable'])
            self.job.status='failed'
            code,body=self.post('generate',self.form(request_id='remove-extra-reference-03',extra_references='[]'))
            self.assertEqual(code,202,body);self.assertEqual(body['extra_references'],[]);self.assertFalse(body['segments'][0]['reusable'])

    def test_extra_count_type_and_stale_reference_rejected(self):
        self.use_workflow('library')
        for extra in [{'extra_references':'{}'}, {'extra_references':json.dumps([{'upload':i} for i in range(8)])},
                      {'extra_references':'[{"upload":0}]','extra_images':(io.BytesIO(b'bad'),'not-image.txt')},
                      {'extra_references':json.dumps([{'keep':0,'source_job_id':self.job.id,'sha256':'stale'}])}]:
            code,body=self.post('generate',self.form(**extra));self.assertEqual(code,400,body)
        self.thread.assert_not_called()


if __name__ == '__main__': unittest.main()
