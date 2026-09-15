"""Shared colored-cast workflow with per-person image bindings and durable receipts."""
import json
import uuid
from pathlib import Path

from flask import jsonify, request, send_file, redirect

from cast_colors import COLOR_NAMES, color_contract, color_model_prompt, normalize_plan, source_anchor, validate_mentions
from motion_transfer import MotionTransfer
from workflow_core import WorkflowError

MODES = {"person": "更换人物", "clothing": "更换服装", "custom": "人物服装自由替换", "dynamic": "动态人物替换",
         "object": "动态物品替换", "scene": "动态背景替换", "motion": "人物动作迁移", "rewrite": "按秒改写视频"}


class MultiCast(MotionTransfer):
    PROJECT = "colored_cast"
    KIND = "multi_cast_prepare"
    API = "/api/multi-cast"
    PAGE = "multi-cast"
    TEMPLATE = "multi_cast.html"
    MANIFEST = "multi_cast.json"

    def page(self):
        mode = request.args.get('mode', 'dynamic')
        destination = {'object': '/projects/wardrobe#dynamic_object', 'scene': '/projects/wardrobe#dynamic_scene',
                       'rewrite': '/projects/wardrobe-continuation', 'motion': '/projects/motion-transfer'}
        return redirect(destination.get(mode, '/projects/wardrobe#' + (mode if mode in MODES else 'dynamic')))

    def history(self):
        projects=[]
        for path in self.manifests():
            try:
                data=json.loads(path.read_text(encoding='utf-8'))
                meta=data.get('cast_continuity',{});mode=meta.get('draft',{}).get('edit_mode','')
                if data.get('project')==self.PROJECT:
                    projects.append({'id':data['id'],'created_at':data['created_at'],'edit_mode':mode,
                                     'name':MODES.get(mode,'多人制作')+' · '+meta.get('source_name','原片')})
            except (OSError,ValueError,KeyError):
                continue
        return jsonify(projects=projects)

    def latest(self):
        for path in self.manifests():
            try:
                data=json.loads(path.read_text(encoding='utf-8'))
                mode=data.get('cast_continuity',{}).get('draft',{}).get('edit_mode')
                if not request.args.get('mode') or mode==request.args['mode']:
                    return jsonify(self.public(self.load(data['id'])))
            except (OSError,ValueError,KeyError):
                continue
        return jsonify(id='')

    def new(self):
        job = super().new()
        mode = request.form.get("edit_mode", "dynamic")
        if mode not in MODES:
            raise WorkflowError("请选择有效的多人制作功能。")
        plan = normalize_plan(json.loads(request.form.get("cast_plan", "null")))
        self.meta(job)['draft'].update(edit_mode=mode, cast_plan=plan, roles=[dict(r, uri="") for r in plan], extras=[])
        return job

    def prepare(self):
        job = self.new()
        try:
            source = self.video_upload(job, "source")
            info = self.h.validate_seedance_reference_video(source)
            threshold = float(request.form.get('face_score_threshold', '.55'))
            if not .30 <= threshold <= .90:
                raise WorkflowError("人脸检测阈值范围为 0.30–0.90。")
            job.depth_path = source
            job.source_duration = info.duration
            self.meta(job).update(origin="source", source_name=request.files['source'].filename, face_score_threshold=threshold)
            def worker():
                self.h.run_wardrobe_face_mosaic(job, source, score_threshold=threshold, project_label="多人制作")
                if self.meta(job)['draft']['edit_mode'] == 'rewrite':
                    # Rewriting directly references the complete masked clip, without a colored-model call.
                    job.white_model_path = job.mosaic_path
            self.launch(job, "正在打码原片中出现的全部人脸", worker)
            return jsonify(self.public(job)), 202
        except Exception as exc:
            self.h.job_error(job, exc)
            self.save(job)
            raise

    def import_white(self):
        if request.form.get('edit_mode') == 'rewrite':
            raise WorkflowError("按秒改写请上传原片并打码，不使用分色模型。")
        return super().import_white()

    def white_model(self):
        job = self.current()
        key = self.request_key()
        if key in self.meta(job)['requests'] or job.white_model_path:
            return jsonify(self.public(job))
        self.idle(job)
        if self.pending(job):
            raise WorkflowError("先查询已提交任务，避免重复生成。")
        if not self.h.form_bool('paid_confirmed') or not self.h.form_bool('mosaic_reviewed'):
            raise WorkflowError("请预览确认全部人脸已打码，并确认分色模型生成费用。")
        if not self.h._runs_record_file(job.mosaic_path):
            raise WorkflowError("请先完成人脸打码。")
        draft = self.meta(job)['draft']
        prompt = color_model_prompt(draft['cast_plan'], green=draft['edit_mode'] in {'scene','motion'})
        self.meta(job).update(color_prompt=prompt, active_child='white')
        self.meta(job)['requests'][key] = 'white'
        def worker():
            self.h.run_wardrobe_white_model(job, job.depth_path, span=100, prompt_override=prompt,
                child_project=self.PROJECT, child_kind='motion_white', project_label='多人分色母版',
                reference_source_factory=self.video_reference, preserve_scene=True)
        self.launch(job, '正在生成红白等分色人物母版', worker)
        return jsonify(self.public(job)), 202

    def update_draft(self, job):
        draft = json.loads(json.dumps(self.meta(job)['draft']))
        roles = json.loads(request.form.get('roles', json.dumps(draft['roles'])))
        if not isinstance(roles,list) or len(roles) != len(draft['cast_plan']):
            raise WorkflowError('必须为每个颜色槽位分别绑定人物及服装。')
        old = {r['id']: r for r in draft['roles']}
        cleaned = []
        for plan, role in zip(draft['cast_plan'], roles):
            if not isinstance(role,dict) or role.get('id') != plan['id']:
                raise WorkflowError('人物槽位顺序已变化，请恢复原来的颜色对应关系。')
            uri = str(role.get('uri',''))
            if uri:
                self.h.wardrobe_object.asset(uri, 'Image')
            clothing = self.image_upload(job, f"clothing_{plan['id']}") or old[plan['id']].get('clothing_path','')
            cleaned.append(dict(plan, uri=uri, name=str(role.get('name',''))[:100], clothing_path=clothing))
        draft['roles'] = cleaned
        prompt = request.form.get('prompt', draft['prompt']).strip()
        if len(prompt)>1000:
            raise WorkflowError('改写或替换要求最多 1000 字。')
        draft.update(prompt=prompt, resolution=request.form.get('resolution',draft['resolution']), generate_audio=self.h.form_bool('generate_audio'))
        if draft['resolution'] not in {'480p','720p'}:
            raise WorkflowError('请选择 480p 或 720p。')
        for field in ('scene','replacement'):
            uploaded = self.image_upload(job,field)
            if uploaded:
                draft[field+'_path']=uploaded
            elif self.h.form_bool('clear_'+field):
                draft.pop(field+'_path',None)
        # References retain stable IDs across removal and reload; indices are never used as identity.
        selected = json.loads(request.form.get('keep_extras',json.dumps([r['id'] for r in draft['extras']])))
        if not isinstance(selected,list) or len(set(selected)) != len(selected) or any(i not in {r['id'] for r in draft['extras']} for i in selected):
            raise WorkflowError('其他参考图片已变化，请重新选择。')
        draft['extras']=[r for r in draft['extras'] if r['id'] in selected]
        uploads=request.files.getlist('extra_images')
        if len(uploads)>7:
            raise WorkflowError('其他参考图片数量过多。')
        for upload in uploads:
            # Reuse the validated image uploader with a distinct immutable filename.
            if not upload.filename:
                continue
            path=self.save_image_upload(job,upload,'extra')
            draft['extras'].append({'id':uuid.uuid4().hex[:12],'path':path})
        image_count=len(cleaned)*2+bool(draft.get('scene_path'))+bool(draft.get('replacement_path'))+len(draft['extras'])
        if image_count>9:
            raise WorkflowError(f'人物、服装和其他图片合计最多 9 张，当前 {image_count} 张。')
        validate_mentions(prompt,image_count)
        self.meta(job)['draft']=draft
        return draft

    def references(self, draft, *, validate_roles=True):
        images=[]; mapping=[]
        mode=draft['edit_mode']
        for role in draft['roles']:
            if validate_roles:
                self.h.wardrobe_object.asset(role['uri'],'Image')
            clothing=self.h._runs_record_file(role.get('clothing_path'))
            if not clothing:
                raise WorkflowError(f"请上传{COLOR_NAMES[role['color']]}模人物的服装参考。")
            i=len(images)+1
            images.extend([role['uri'],str(clothing)])
            target=(f"原片人物「{source_anchor(role,len(draft['roles']))}」" if mode=='rewrite' else f"{COLOR_NAMES[role['color']]}模人物")
            mapping.append(f'{target}只对应身份@图片{i}与服装@图片{i+1}；身份图只提供脸部、发型与体型，服装图只提供该人的穿戴。')
        if mode in {'scene','motion'} and not draft.get('scene_path'):
            raise WorkflowError('请上传目标场景参考图。')
        for field,label in (('scene','场景外观、材质和光照'),('replacement','目标物品外观')):
            if draft.get(field+'_path'):
                path=self.h._runs_record_file(draft[field+'_path'])
                if not path: raise WorkflowError('参考图片丢失，请重新上传。')
                images.append(str(path));mapping.append(f'@图片{len(images)}只提供{label}。')
        for extra in draft['extras']:
            path=self.h._runs_record_file(extra['path'])
            if not path: raise WorkflowError('其他参考图片丢失，请重新上传。')
            images.append(str(path));mapping.append(f'@图片{len(images)}为可选细节参考，仅按用户要求使用。')
        if mode in {'object','rewrite'} and not draft['prompt']:
            raise WorkflowError('请填写具体替换物品或按秒改写的要求。')
        instructions={
            'person':'只替换所列人物身份，服装使用各自原片服装参考，背景道具原样保留。',
            'clothing':'人物图必须为各自原片身份，分别换成各自服装参考；场景道具原样保留。',
            'dynamic':'分别替换所列人物及其服装，背景道具和运镜完整保留。',
            'custom':'分别替换人物、服装；有场景参考时按其外观更换背景，否则保留原背景。',
            'object':'人物服装图用于还原各自原片形象，只按要求替换指定物品，保留握持接触、动作与遮挡。',
            'scene':'人物服装图用于还原各自原片形象，用场景图重建动态背景，保留原运镜、透视及视差。',
            'motion':'用各自新人物和服装复刻各自动作及互动，场景图提供新环境。',
            'rewrite':'根据完整打码原片和对应人物服装参考，在用户指定的原片秒数范围内修改剧情；其余部分尽量保留，一次输出完整视频，不裁剪拼接。',
        }[mode]
        contract=('参考完整@视频1。'+color_contract(draft['cast_plan'])+'逐帧保留每个人的占位、比例、前后景、模糊、遮挡、互动、动作时序和镜头运动。'
                  '只让指定身份与服装作用于对应人物，禁止串脸、换位、服装互换、加人或漏人；不继承图片姿势、背景、排版、机位。'
                  '输出正常彩色视频，去除人物红白等代理材质及马赛克，不生成拼贴、分屏、新字幕、水印或色卡。')
        if mode=='rewrite':
            contract=contract.replace('参考完整@视频1。','参考完整@视频1（打码原片，没有分色人物；红白等标签仅用于素材分组）。')
        prompt=contract+instructions+''.join(mapping)+'\n用户要求：'+validate_mentions(draft['prompt'],len(images))
        if len(prompt)>2000: raise WorkflowError('总提示词过长，请精简要求和原片人物描述。')
        return images,prompt

    def public(self,job):
        result=super().public(job)
        draft=json.loads(json.dumps(self.meta(job)['draft']))
        for role in draft['roles']:
            role['clothing_url']=self.url(job,'clothing-'+role['id'],role.pop('clothing_path',''))
        for field in ('scene','replacement'):
            draft[field+'_url']=self.url(job,field,draft.pop(field+'_path',''))
        for extra in draft['extras']:
            extra['url']=self.url(job,'extra-'+extra['id'],extra.pop('path',''))
        result['draft']=draft
        result['color_prompt']=self.meta(job).get('color_prompt',color_model_prompt(draft['cast_plan'],green=draft['edit_mode'] in {'scene','motion'}))
        return result

    def file(self,job_id,kind):
        job=self.load(job_id);draft=self.meta(job)['draft']
        paths={'replacement':draft.get('replacement_path')}
        paths.update({'clothing-'+r['id']:r.get('clothing_path') for r in draft['roles']})
        paths.update({'extra-'+r['id']:r['path'] for r in draft['extras']})
        if kind in paths:
            path=self.h._runs_record_file(paths[kind])
            if not path: return jsonify(error='参考图片不存在。'),404
            return send_file(path,conditional=True)
        return super().file(job_id,kind)
