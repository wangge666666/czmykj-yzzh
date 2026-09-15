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
from cast_colors import color_model_prompt, long_color_plans, normalize_plan
from workflow_core import WorkflowError


class ImmediateThread:
    def __init__(self,target,**kwargs): self.target=target
    def start(self): self.target()


class MultiCastTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.engine=host.multi_cast;host.app.config.update(TESTING=True);self.client=host.app.test_client()
        for p in [patch.object(host,'PROJECT_DIR',self.root),patch.object(host,'JOBS',{}),
                  patch.object(host,'timestamped_run_dir',side_effect=self.directory),patch('motion_transfer.threading.Thread',ImmediateThread),
                  patch.object(host,'validate_seedance_reference_video',return_value=SimpleNamespace(duration=5)),
                  patch.object(host,'ark_assets_client',return_value=Mock(get_asset=Mock(return_value={'Status':'Active','AssetType':'Image'})))]:
            p.start();self.addCleanup(p.stop)

    def directory(self,suffix):
        path=self.root/'runs'/f'20260914_{suffix}';path.mkdir(parents=True,exist_ok=True);return path

    def image(self,value=200): return io.BytesIO(cv2.imencode('.png',np.full((480,480,3),value,np.uint8))[1].tobytes())

    def job(self,mode='dynamic',count=2):
        with host.app.test_request_context('/',method='POST',data={'edit_mode':mode,'cast_plan':json.dumps([{}]*count)}):
            job=self.engine.new()
        white=job.run_dir/'white.mp4';white.write_bytes(b'colored master')
        job.white_model_path=white;job.source_duration=5;job.status='succeeded';self.engine.save(job);return job

    def data(self,job,**extra):
        roles=self.engine.meta(job)['draft']['roles']
        data={'project_id':job.id,'roles':json.dumps([{'id':r['id'],'uri':f'asset://asset-actor-{i}123456','name':f'人物{i}'} for i,r in enumerate(roles)]),
              'prompt':'分别替换红模和白模，保留互动与遮挡。','paid_confirmed':'true','white_reviewed':'true','request_id':'cast_request_123456','resolution':'720p'}
        data.update({f"clothing_{r['id']}":(self.image(150+i*10),f'clothing{i}.png') for i,r in enumerate(roles)})
        data.update(extra);return data

    def success(self,child,**kwargs):
        child.output_path=child.run_dir/'output.mp4';child.output_path.write_bytes(b'untrimmed full output')
        child.task_id='paid-task-existing';child.update(status='succeeded',cloud_status='succeeded');host.persist_cloud_job(child,status='succeeded')

    def test_pair_rule_and_four_color_plan(self):
        pair=normalize_plan([{},{}]);self.assertEqual([r['color'] for r in pair],['red','white'])
        prompt=color_model_prompt(pair,green=True)
        for text in ['男性为红模','女性为白模','左侧人物为红模','颜色只分配一次','纯绿色','全身']:
            self.assertIn(text,prompt)
        for count in (3,4):
            plan=normalize_plan([{}]*count)
            self.assertEqual([r['color'] for r in plan],['red','white','yellow','blue'][:count])
            prompt=color_model_prompt(plan)
            self.assertIn('p3=黄模，对应首次可辨认的同框画面从左到右第 3 位',prompt)
            if count==4:self.assertIn('p4=蓝模，对应首次可辨认的同框画面从左到右第 4 位',prompt)
        for invalid in ([{}]*5,[{'color':'red'},{'color':'red'}],[{'source':'@图片1'}]):
            with self.assertRaises(WorkflowError):normalize_plan(invalid)

    def test_long_three_and_four_person_colors_follow_stable_identity(self):
        for count in (3,4):
            job=SimpleNamespace(shots=[{'index':1,'suggested_actor_count':count},{'index':2,'suggested_actor_count':count}],cast_continuity={
                'characters':[{'character_id':i,'description':f'人物{i}'} for i in range(1,count+1)],
                'assignments':[{'shot_index':shot,'slot':slot,'character_id':slot if shot==1 else count+1-slot}
                               for shot in (1,2) for slot in range(1,count+1)]})
            result=long_color_plans(job,host)
            expected=['red','white','yellow','blue'][:count]
            self.assertEqual([r['color'] for r in result[1]],expected)
            self.assertEqual([r['color'] for r in result[2]],expected[::-1])

    def test_long_identity_colors_survive_gender_order_and_camera_swap(self):
        job=SimpleNamespace(shots=[{'index':1,'suggested_actor_count':2},{'index':2,'suggested_actor_count':2}],cast_continuity={
            'characters':[{'character_id':1,'description':'左侧女子'},{'character_id':2,'description':'右侧男子'}],
            'assignments':[{'shot_index':1,'slot':1,'character_id':1},{'shot_index':1,'slot':2,'character_id':2},
                           {'shot_index':2,'slot':1,'character_id':2},{'shot_index':2,'slot':2,'character_id':1}]})
        result=long_color_plans(job,host)
        self.assertEqual([r['color'] for r in result[1]],['white','red'])
        self.assertEqual([r['color'] for r in result[2]],['red','white'])
        job.cast_continuity['characters'][1]['description']='右侧女子'
        result=long_color_plans(job,host);self.assertEqual(result[1][0]['color'],'red');self.assertEqual(result[2][1]['color'],'red')
        with patch.object(host,'long_shot_performance_slot_anchor',side_effect=lambda shot,slot:'画面右侧' if slot==1 else '画面左侧'):
            result=long_color_plans(job,host);self.assertEqual(result[1][1]['color'],'red');self.assertEqual(result[2][0]['color'],'red')
        job.cast_continuity['assignments']=[]
        with self.assertRaises(WorkflowError):long_color_plans(job,host)

    def test_generate_binds_two_people_clothes_scene_and_extra_in_order(self):
        job=self.job('scene');payload=self.data(job,scene=(self.image(40),'scene.png'),extra_images=(self.image(50),'prop.png'),prompt='参考@图片5的新场景，@图片6只提供道具细节。')
        with patch.object(host,'run_generation',side_effect=self.success) as run,patch.object(host,'conform_video_duration') as trim:
            response=self.client.post('/api/multi-cast/generate',data=payload)
            self.assertEqual(response.status_code,202,response.json)
            refs=run.call_args.kwargs['reference_images'];self.assertEqual(len(refs),6)
            self.assertEqual(refs[0],'asset://asset-actor-0123456');self.assertEqual(refs[2],'asset://asset-actor-1123456')
            self.assertNotEqual(refs[1],refs[3]);prompt=run.call_args.kwargs['options']['prompt']
            self.assertIn('红模人物只对应身份@图片1与服装@图片2',prompt);self.assertIn('白模人物只对应身份@图片3与服装@图片4',prompt)
            self.assertIn('@图片5只提供场景',prompt);self.assertIn('@图片6为可选细节',prompt);trim.assert_not_called()
            response=self.client.post('/api/multi-cast/generate',data={'project_id':job.id,'request_id':'cast_request_123456'})
            self.assertEqual(response.status_code,202);self.assertEqual(run.call_count,1)
        public=self.engine.public(job);self.assertNotIn('clothing_path',public['draft']['roles'][0])
        for url in [public['draft']['roles'][0]['clothing_url'],public['draft']['extras'][0]['url']]:
            r=self.client.get(url);self.assertEqual(r.status_code,200);r.close()
        host.JOBS.clear();restored=self.engine.load(job.id)
        self.assertEqual(self.engine.public(restored)['draft']['roles'][1]['color'],'white')
        self.assertTrue(self.engine.public(restored)['outputs'][0]['url'])
        self.assertEqual(self.client.get('/api/motion-transfer/projects/'+job.id).status_code,400)

    def test_missing_slot_wrong_order_and_image_limit_block_paid_calls(self):
        with patch.object(host,'run_generation') as run:
            job=self.job();payload=self.data(job);payload.pop('clothing_p2')
            r=self.client.post('/api/multi-cast/generate',data=payload);self.assertEqual(r.status_code,400);self.assertIn('白模',r.json['error'])
            payload=self.data(job);roles=json.loads(payload['roles']);payload['roles']=json.dumps(roles[::-1]);r=self.client.post('/api/multi-cast/generate',data=payload);self.assertEqual(r.status_code,400)
            job=self.job('scene',4);r=self.client.post('/api/multi-cast/generate',data=self.data(job,scene=(self.image(),'scene.png'),extra_images=(self.image(),'extra.png')));self.assertEqual(r.status_code,400);self.assertIn('9 张',r.json['error'])
            run.assert_not_called()

    def test_rewrite_masks_all_faces_and_skips_colored_generation(self):
        def mask(job,source,**kwargs):
            job.mosaic_path=job.run_dir/'masked.mp4';job.mosaic_path.write_bytes(b'masked full source')
        with patch.object(host,'run_wardrobe_face_mosaic',side_effect=mask) as mosaic,patch.object(host,'run_wardrobe_white_model') as white:
            r=self.client.post('/api/multi-cast/prepare',data={'edit_mode':'rewrite','cast_plan':json.dumps([{},{}]),'source':(io.BytesIO(b'video'),'source.mp4'),'face_score_threshold':'.45'})
            self.assertEqual(r.status_code,202,r.json);self.assertTrue(r.json['white_url']);self.assertEqual(mosaic.call_args.kwargs['score_threshold'],.45);white.assert_not_called()
            job=self.engine.load(r.json['id'])
            with patch.object(host,'run_generation',side_effect=self.success) as run:
                response=self.client.post('/api/multi-cast/generate',data=self.data(job,prompt='2–4秒红模对应人物转身。'))
                self.assertEqual(response.status_code,202,response.json);self.assertEqual(run.call_args.kwargs['depth_path'],job.mosaic_path)
                self.assertIn('没有分色人物',run.call_args.kwargs['options']['prompt']);self.assertIn('不裁剪拼接',run.call_args.kwargs['options']['prompt'])

    def test_colored_native_prompt_and_material_qa(self):
        actors=[{'id':1,'role':'左侧女子','model_color':'white'},{'id':2,'role':'右侧男子','model_color':'red'}]
        prompt=host.build_long_shot_prompt(actors,'保持运镜',white_model_reference=True)
        self.assertIn('（白模）',prompt);self.assertIn('（红模）',prompt)
        path=self.root/'two-color.mp4';writer=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'mp4v'),10,(320,240))
        frame=np.zeros((240,320,3),np.uint8);frame[:]=(0,255,0);frame[30:220,40:120]=(0,0,210);frame[30:220,200:280]=255
        for _ in range(20):writer.write(frame)
        writer.release();profile=host._sample_white_model_profile(path,cast_colors=('red','white'))
        self.assertGreater(profile['median_cast_material_ratio'],.90)
        self.assertGreater(profile['median_colored_subject_ratio'],.3)

    def test_color_master_confirmation_deduplication_and_prompt(self):
        job=self.job('scene');job.mosaic_path=job.white_model_path;job.depth_path=job.white_model_path;job.white_model_path=None;self.engine.save(job)
        form={'project_id':job.id,'request_id':'colored_white_request_123','paid_confirmed':'true'}
        with patch.object(host,'run_wardrobe_white_model') as white:
            response=self.client.post('/api/multi-cast/white-model',data=form);self.assertEqual(response.status_code,400);white.assert_not_called()
            form['mosaic_reviewed']='true'
            def complete(parent,source,**kwargs):parent.white_model_path=parent.mosaic_path
            white.side_effect=complete
            response=self.client.post('/api/multi-cast/white-model',data=form);self.assertEqual(response.status_code,202,response.json)
            self.assertIn('男性为红模',white.call_args.kwargs['prompt_override']);self.assertIn('纯绿色',white.call_args.kwargs['prompt_override'])
            self.assertEqual(white.call_args.kwargs['child_project'],'colored_cast')
            response=self.client.post('/api/multi-cast/white-model',data=form);self.assertEqual(response.status_code,200);self.assertEqual(white.call_count,1)

    def test_ambiguous_task_blocks_resubmit_and_recovers_without_generation(self):
        job=self.job()
        def waiting(child,**kwargs):
            child.task_id='existing-cloud-task';child.update(status='failed',cloud_status='running',error='connection lost');host.persist_cloud_job(child,status='failed')
        with patch.object(host,'run_generation',side_effect=waiting) as run:
            response=self.client.post('/api/multi-cast/generate',data=self.data(job));self.assertEqual(response.status_code,202);self.assertTrue(response.json['needs_query'])
            response=self.client.post('/api/multi-cast/generate',data=self.data(job,request_id='second_request_123456'));self.assertEqual(response.status_code,400)
            with patch.object(host,'resume_cloud_job',side_effect=self.success),patch.object(host,'conform_video_duration') as trim:
                response=self.client.post('/api/multi-cast/recover',data={'project_id':job.id});self.assertEqual(response.status_code,202,response.json)
                self.assertTrue(response.json['outputs'][0]['url']);self.assertEqual(run.call_count,1);trim.assert_not_called()


if __name__=='__main__':unittest.main()
