import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import web_app
import inline_cast
from test_wardrobe_object import ObjectWorkflowTests as ObjectFixture
from test_wardrobe_full_rewrite import FullRewriteTests as RewriteFixture

PERSON = {'id': 'person-second-001', 'uri': 'asset://asset-personSECOND', 'source': '开头右侧的人'}


class InlineCastAPI(unittest.TestCase):
    setUp = ObjectFixture.setUp
    source = ObjectFixture.source
    image = staticmethod(ObjectFixture.image)
    generate = ObjectFixture.generate

    def test_object_and_scene_submit_pairs_restore_preview_and_keep_clothing(self):
        for mode in ('dynamic_object', 'dynamic_scene'):
            source = self.source('references')
            if mode == 'dynamic_scene':
                source.kind = 'wardrobe_prepare_dynamic_scene'
                source.cast_continuity['wardrobe_dynamic']['scene_workflow'] = 'references'
            args = dict(mode=mode, white_reviewed='true', person_asset='asset://asset-personFIRST', clothing_image=self.image(),
                        replacement_image=self.image(), prompt='换成@图片3，人物2使用@图片4并穿@图片5',
                        additional_people=json.dumps([PERSON]), additional_clothing_person_second_001=None)
            args.pop('additional_clothing_person_second_001')
            args['additional_clothing_'+PERSON['id']] = self.image()
            preview = self.client.post('/api/inline-cast/prompt-preview', json=dict(mode=mode,
                people=[dict(source=PERSON['source'], has_clothing=True)], has_replacement=True, custom=args['prompt']))
            self.assertEqual(preview.status_code, 200)
            expected_prompt = preview.get_json()['final_prompt']
            code, body = self.generate(source, **args)
            self.assertEqual(code, 202, body)
            call = self.thread.call_args.kwargs['kwargs']
            self.assertEqual(len(call['reference_images']), 5)
            self.assertEqual(call['reference_images'][3], PERSON['uri'])
            self.assertIn('人物2', call['options']['prompt'])
            self.assertIn('服装仅使用@图片5', call['options']['prompt'])
            self.assertEqual(expected_prompt, call['options']['prompt'])
            self.assertNotIn('clothing', body['inline_cast'][0])
            preview = self.client.get(body['inline_cast'][0]['clothing_url'])
            self.assertEqual(preview.status_code, 200); preview.close()
            child=web_app.JOBS[body['id']]; child.status='succeeded'
            web_app.persist_wardrobe_swap_job(child,mode,source_job_id=source.id)
            self.assertEqual(source.cast_continuity['inline_cast'][0]['uri'], PERSON['uri'])
            # Reload from manifest rather than retaining an in-memory binding.
            web_app.JOBS.clear(); source=web_app.restore_wardrobe_swap_job(source.id)
            self.assertEqual(source.public()['inline_cast'][0]['id'],PERSON['id'])
            args['additional_people']=json.dumps([{**PERSON,'keep_clothing':True}])
            args.pop('additional_clothing_'+PERSON['id'])
            args.update(clothing_image=self.image(),replacement_image=self.image(),request_id='inline-generate-next-001')
            code,body=self.generate(source,**args);self.assertEqual(code,202,body)
            self.assertEqual(self.thread.call_args.kwargs['kwargs']['reference_images'][4],call['reference_images'][4])

    def test_original_wardrobe_modes_bind_additional_people(self):
        for mode in ('person','clothing','scene','custom','dynamic'):
            source=self.source('references');source.kind='wardrobe_prepare_'+mode
            for field in ('clothing','scene'):
                path=source.run_dir/(field+'.png');path.write_bytes(self.image()[0].read());setattr(source,field+'_path',path)
            source.cast_continuity['wardrobe_dynamic']['description']=''
            web_app.persist_wardrobe_swap_job(source,mode)
            custom='人物2使用@图片3' if mode=='dynamic' else '人物2使用@图片4'
            preview=self.client.post('/api/inline-cast/prompt-preview',json=dict(mode=mode,
                people=[dict(source=PERSON['source'],has_clothing=False)],custom=custom)).get_json()
            code,body=self.generate(source,mode=mode,white_reviewed='true',person_asset='asset://asset-personFIRST',
                new_clothing_image=self.image(),new_scene_image=self.image(),additional_people=json.dumps([PERSON]),
                prompt=custom)
            self.assertEqual(code,202,body)
            call=self.thread.call_args.kwargs['kwargs']
            self.assertEqual(call['reference_images'][-1],PERSON['uri'])
            self.assertIn('人物2',call['options']['prompt'])
            self.assertEqual(preview['final_prompt'],call['options']['prompt'])

    def test_invalid_people_and_library_workflow_block_before_submit(self):
        for people in ([PERSON]*2,[{**PERSON,'id':'../outside'}],[{**PERSON,'uri':''}]):
            source=self.source('references')
            code,body=self.generate(source,white_reviewed='true',person_asset='asset://asset-personFIRST',clothing_image=self.image(),additional_people=json.dumps(people))
            self.assertEqual(code,400,body)
        code,body=self.generate(self.source('library'),additional_people=json.dumps([PERSON]))
        self.assertEqual(code,400,body);self.thread.assert_not_called()

    def test_primary_clothing_optional_and_mixed_people_match_actual_images(self):
        for mode in ('dynamic','dynamic_object','dynamic_scene'):
            for extra in (False,True):
                source=self.source('references');source.kind='wardrobe_prepare_'+mode
                if mode=='dynamic_scene':source.cast_continuity['wardrobe_dynamic']['scene_workflow']='references'
                people=[PERSON] if extra else []
                has_object=mode!='dynamic'
                values=dict(mode=mode,first_clothing=False,people=[dict(has_clothing=True,source=PERSON['source'])] if extra else [],has_replacement=has_object,custom='按参考图替换')
                preview=self.client.post('/api/inline-cast/prompt-preview',json=values).get_json()
                args=dict(mode=mode,white_reviewed='true',person_asset='asset://asset-personFIRST',prompt=values['custom'],additional_people=json.dumps(people))
                if has_object:args['replacement_image']=self.image()
                if extra:args['additional_clothing_'+PERSON['id']]=self.image()
                code,body=self.generate(source,**args)
                self.assertEqual(code,202,body)
                submitted=self.thread.call_args.kwargs['kwargs']
                self.assertEqual(submitted['options']['prompt'],preview['final_prompt'])
                self.assertIn('服装均使用@图片1',preview['final_prompt'])
                self.assertEqual(len(submitted['reference_images']),1+int(has_object)+2*int(extra))
                if extra:
                    index=2+int(has_object)
                    self.assertEqual(submitted['reference_images'][index-1],PERSON['uri'])
                    self.assertIn(f'服装仅使用@图片{index+1}',preview['final_prompt'])
                if has_object:self.assertIn('@图片2仅提供',preview['final_prompt'])

    def test_white_prompt_color_rules_and_page_redirects(self):
        with web_app.app.test_request_context('/test',method='POST',data={'inline_cast_count':'2'}):
            prompt,plan=inline_cast.white_prompt('dynamic_scene')
        self.assertEqual([p['color'] for p in plan],['red','white'])
        self.assertIn('男性为红模',prompt);self.assertIn('左侧人物为红模',prompt);self.assertIn('#00FF00',prompt)
        for mode,target in [('object','/projects/wardrobe#dynamic_object'),('scene','/projects/wardrobe#dynamic_scene'),('rewrite','/projects/wardrobe-continuation')]:
            response=self.client.get('/projects/multi-cast?mode='+mode)
            self.assertEqual(response.status_code,302);self.assertEqual(response.location,target)

    def test_preview_counts_sources_color_plan_and_invalid_mentions_without_side_effects(self):
        for count in (2,3,4):
            sources=['开头右侧的人']+['']*(count-2)
            values=dict(mode='dynamic',colored=True,people=[dict(source=s,has_clothing=True) for s in sources],custom='保留原镜头',white_extra='保留动作节奏')
            result=self.client.post('/api/inline-cast/prompt-preview',json=values).get_json()
            self.assertEqual(result['count'],count);self.assertEqual(result['image_count'],count*2)
            self.assertIn(f'处理 {count} 个主要人物',result['final_prompt'])
            if count == 2:self.assertNotIn('人物3',result['final_prompt'])
            with web_app.app.test_request_context('/test',method='POST',data=dict(inline_cast_count=str(count),inline_cast_sources=json.dumps(sources),white_prompt=values['white_extra'])):
                submitted,plan=inline_cast.white_prompt('dynamic')
            self.assertEqual(result['white_prompt'],submitted)
            self.assertEqual(plan[1]['source'],sources[0])
            for i,color in enumerate(['红','白','黄','蓝'][:count],1):
                self.assertIn(f'p{i}={color}模',submitted)
                self.assertIn(f'人物{i}=p{i}{color}模',result['final_prompt'])
        values['custom']='使用@图片9'
        result=self.client.post('/api/inline-cast/prompt-preview',json=values).get_json()
        self.assertEqual(result['final_prompt'],'');self.assertIn('@图片9',result['final_error'])
        self.assertTrue(result['white_prompt'])
        self.thread.assert_not_called();self.assets.assert_not_called()
        self.assertEqual(list(self.root.rglob('wardrobe_manifest.json')),[])

    def test_saved_colors_bind_the_same_reference_in_preview_and_submission(self):
        for mode in ('dynamic','dynamic_object','dynamic_scene'):
            for colors in (['red','white','yellow'],['red','white','yellow','blue'],['red','white','blue','yellow']):
                with self.subTest(mode=mode,colors=colors):
                    source=self.source('references');source.kind='wardrobe_prepare_'+mode
                    if mode=='dynamic_scene':source.cast_continuity['wardrobe_dynamic']['scene_workflow']='references'
                    plan=[dict(id=f'p{i+1}',color=color,source='') for i,color in enumerate(colors)]
                    source.cast_continuity['inline_color_plan']=plan
                    web_app.persist_wardrobe_swap_job(source,mode)
                    people=[dict(id=f'person-extra-{i:03}',uri=f'asset://asset-personEXTRA{i}',source='') for i in range(2,len(colors)+1)]
                    preview=self.client.post('/api/inline-cast/prompt-preview',json=dict(mode=mode,
                        color_plan=plan,people=[{} for p in people],has_replacement=mode!='dynamic',custom='按参考图替换')).get_json()
                    args=dict(mode=mode,white_reviewed='true',person_asset='asset://asset-personFIRST',
                              additional_people=json.dumps(people),prompt='按参考图替换')
                    if mode=='dynamic':args['new_clothing_image']=self.image()
                    else:args.update(clothing_image=self.image(),replacement_image=self.image())
                    code,body=self.generate(source,**args)
                    self.assertEqual(code,202,body)
                    call=self.thread.call_args.kwargs['kwargs']
                    self.assertEqual(preview['final_prompt'],call['options']['prompt'])
                    self.assertEqual(body['inline_color_plan'],plan)
                    for i,color in enumerate(colors[2:],3):
                        name={'yellow':'黄','blue':'蓝'}[color]
                        self.assertIn(f'人物{i}=p{i}{name}模',call['options']['prompt'])
                        self.assertIn(f'p{i}={name}模，对应首次可辨认的同框画面从左到右第 {i} 位',call['options']['prompt'])
                        index=i+(0 if mode=='dynamic' else 1)+1
                        self.assertIn(f'人物{i}仅使用@图片{index}',call['options']['prompt'])
                        self.assertEqual(call['reference_images'][index-1],people[i-2]['uri'])
                    self.assertIn('p3=黄模',preview['white_prompt'],'new masters always use the new color order')

    def test_changed_person_count_requires_a_matching_master(self):
        result=self.client.post('/api/inline-cast/prompt-preview',json=dict(mode='dynamic',
            color_plan=[dict(color=c) for c in ['red','white']],people=[{},{}])).get_json()
        self.assertEqual(result['final_prompt'],'')
        self.assertIn('当前母版为 2 人',result['final_error'])
        self.assertIn('p3=黄模',result['white_prompt'])
        self.thread.assert_not_called()


class InlineRewriteAPI(unittest.TestCase):
    setUp=RewriteFixture.setUp
    form=RewriteFixture.form
    full_form=RewriteFixture.full_form
    post=RewriteFixture.post
    use_workflow=RewriteFixture.use_workflow
    validate_source=RewriteFixture.validate_source

    def test_full_video_other_reference_before_additional_person(self):
        data=self.use_workflow('references')
        png=Path(self.segments[0]['start_frame_image']).read_bytes()
        form=self.full_form(mosaic_reviewed='true',person_asset='asset://asset-personFIRST',clothing_image=(io.BytesIO(png),'first.png'),
            extra_references='[{"upload":0}]',extra_images=(io.BytesIO(png),'object.png'),additional_people=json.dumps([PERSON]),
            **{'additional_clothing_'+PERSON['id']:(io.BytesIO(png),'second.png')})
        with self.validate_source(),patch.object(web_app.wardrobe_object,'asset'):
            code,body=self.post('generate',form)
        self.assertEqual(code,202,body)
        attempt=data['attempts'][data['active_attempt']]
        self.assertEqual(len(attempt['segments']),1)
        self.assertEqual(len(attempt['images']),5)
        self.assertEqual(attempt['images'][3],PERSON['uri'])
        self.assertIn('服装仅使用@图片5',attempt['options']['prompt'])
        self.assertIn('@图片3为其他参考图',attempt['options']['prompt'])
        self.assertEqual(body['inline_cast'][0]['uri'],PERSON['uri'])
        preview=self.client.post('/api/inline-cast/prompt-preview',json=dict(mode='rewrite',duration=10,
            segments=json.loads(form['segments']),extra_count=1,people=[dict(source=PERSON['source'],has_clothing=True)])).get_json()
        self.assertEqual(preview['final_prompt'],attempt['options']['prompt'])
        self.assertEqual(preview['white_prompt'],'')


if __name__=='__main__':unittest.main()
