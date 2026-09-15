import inline_cast
"""Rewrite a whole reference video in one request; no interval extraction or splice."""
import json
import re
import threading
from pathlib import Path

from flask import jsonify, request
from workflow_core import WorkflowError, build_motion_reference_payload, build_video_reference_seedance_payload, validate_seedance_reference_video
from wardrobe_intervals import normalize_segments


def full_rewrite_prompt(segments, workflow, extra_count, duration, people=None, first_clothing=True):
    image_count = extra_count + (1 + int(first_clothing) if workflow == 'references' else 0) + sum(1 + bool(p.get('clothing_index')) for p in people or [])
    allowed = {'@视频1'} | {f'@图片{i+1}' for i in range(image_count)}
    instructions = []
    for item in segments:
        custom = re.sub(r'@\s*(视频|图片)\s*(\d+)', lambda m:f'@{m[1]}{m[2]}', item['prompt'].strip())
        if '@' in re.sub(r'@(视频|图片)\d+', '', custom) or any(token not in allowed for token in re.findall(r'@(?:视频|图片)\d+',custom)):
            raise WorkflowError('提示词含有未绑定的素材，请重新选择 @ 标签。')
        instructions.append(f"原片 {item['start']:.3f}–{item['end']:.3f} 秒：{custom}")
    if sum(len(item['prompt']) for item in segments) > 1000:
        raise WorkflowError('所有区间的改写要求合计最多 1000 字，请适当精简。')
    prompt = (f'参考完整 @视频1 生成一条完整的改写视频，时长跟随原片（约 {duration:.3f} 秒），所有秒数均以原片时间轴为准。'
              '将下方列出的时间范围按对应要求修改，其余时间尽量保持原有画面内容、事件顺序、人物位置、道具、背景和镜头运动。'
              '一次输出从开头到结尾的完整视频，不只输出指定片段，不重排时间，不插入静帧、分屏或素材拼贴。')
    if people:
        prompt += '@视频1是完整的人脸打码视频。' + inline_cast.contract(people, first_clothing=first_clothing)
    elif workflow == 'references':
        prompt += ('@视频1是完整的人脸打码视频；@图片1仅提供人物身份、五官、发型和体型；@图片2仅提供服装。'
                   '用人物参考还原自然清晰的面部，不保留马赛克，不继承图片的背景、姿势、排版或机位。')
    else:
        prompt += '@视频1为角色库原片，人物身份和服装沿用原片。'
    if workflow == 'references' and not first_clothing:
        prompt = prompt.replace('@图片1仅提供人物身份、五官、发型和体型；@图片2仅提供服装。', '@图片1同时提供人物身份、五官、发型、体型及服装。')
    if extra_count:
        base = 1 + int(first_clothing) if workflow == 'references' else 0
        prompt += '、'.join(f'@图片{i+1}' for i in range(base,base+extra_count))+'为其他参考图，仅按对应要求用于物品、场景或细节。'
    prompt += '按新要求自然展开动作和剧情，保持前后连贯，不新增字幕、标题、水印或边框。\n按时间修改要求：\n'+'\n'.join(instructions)
    if len(prompt)>2000:
        raise WorkflowError('总提示词过长，请精简各区间要求。')
    return prompt


class FullVideoRewrite:
    def __init__(self,parent):
        self.p,self.h=parent,parent.h

    def generate(self,job):
        data=self.p.data(job);service=self.p.intervals
        key=request.form.get('request_id','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',key):
            raise WorkflowError('缺少有效的提交标识。')
        if request.form.get('paid_confirmed')!='true':
            raise WorkflowError('请确认本次视频生成。')
        if key in data['attempts']:
            return jsonify(self.p.public(job)),202
        if job.status in {'running','queued','submitted'}:
            raise WorkflowError('任务正在运行，请等待完成。')
        workflow=data.get('rewrite_workflow')
        if workflow not in {'library','references'}:
            raise WorkflowError('请先选择角色库原片，或使用有人物服装参考流程上传原片并打码。')
        if any(service.needs_recovery(job,e) for a in data['attempts'].values() for e in a.get('segments',[])):
            raise WorkflowError('已有生成任务的结果待确认，请先恢复查询，不会重复提交。')
        source=self.p.safe_path(job,data.get('source',''))
        info=validate_seedance_reference_video(source)
        segments=normalize_segments(json.loads(request.form.get('segments','null')),info.duration,info.fps,require_prompts=True)
        resolution=request.form.get('resolution','720p')
        if resolution not in {'480p','720p'}:
            raise WorkflowError('请选择 480p 或 720p。')
        images=[];reference_path='';video_asset=''
        if workflow=='references':
            if request.form.get('mosaic_reviewed')!='true':
                raise WorkflowError('请先预览并确认人脸打码视频。')
            reference_path=str(self.p.safe_path(job,data.get('mosaic','')))
            validate_seedance_reference_video(reference_path)
            person=request.form.get('person_asset','').strip();self.h.wardrobe_object.asset(person,'Image')
            clothing_upload=request.files.get('clothing_image')
            if clothing_upload and clothing_upload.filename:
                folder=job.run_dir/'materials'/key;folder.mkdir(parents=True,exist_ok=True)
                clothing=self.h.save_upload(clothing_upload,folder,'clothing')
            elif data.get('clothing') and request.form.get('primary_clothing_state') != 'none':
                clothing=self.p.safe_path(job,data['clothing'])
            else:
                clothing=None
            images=[person]+([str(clothing)] if clothing else [])
        else:
            video_asset=data.get('video_asset','');self.h.wardrobe_object.asset(video_asset,'Video')
            if request.form.get('person_asset') or request.files.get('clothing_image'):
                raise WorkflowError('人物和服装参考请在有人物服装参考流程中使用。')
        extras=service.collect_extras(job,key)
        images += [str(self.p.safe_path(job,item['path'])) for item in extras]
        people=inline_cast.collect(self.h,job,job,images,workflow=='references')
        prompt=full_rewrite_prompt(segments,workflow,len(extras),info.duration,people,first_clothing=workflow=='references' and bool(clothing))
        options=dict(prompt=prompt,model=self.h.DEFAULT_SEEDANCE_25_MODEL,resolution=resolution,ratio='adaptive',duration=-1,
                     generate_audio=request.form.get('generate_audio')=='true',watermark=False,delete_tos_after=True,reference_upload_strategy='stable')
        common=dict(prompt=prompt,model=options['model'],resolution=resolution,duration=-1,video_reference=video_asset or 'https://validation.invalid/full-mosaic.mp4')
        if images:build_motion_reference_payload(image_sources=images,**common)
        else:build_video_reference_seedance_payload(**common)
        job.cast_continuity["inline_cast"]=people
        entry=dict(key=key,index=0,child_id=f'{job.id}-full-{len(data["attempts"])+1}',status='pending',duration=-1,final_prompt=prompt)
        data.update(segments=segments,active_attempt=key,extra_references=extras,source_duration=info.duration,fps=info.fps,
                    resolution=resolution,generate_audio=options['generate_audio'],generation_mode='full_video')
        if workflow=='references':data.update(person_asset=person,clothing=str(clothing) if clothing else '')
        data['attempts'][key]=dict(key=key,mode='full_video',segments=[entry],images=images,reference_path=reference_path,video_asset=video_asset,
                                   resolution=resolution,generate_audio=options['generate_audio'],options=options)
        job.update(status='running',stage='正在参考完整视频改写',progress=1,error='',output_path=None)
        job.log('本次提交 1 次完整视频生成；时间区间作为提示词要求，不提取衔接图，不裁剪拼接。')
        self.p.save(job)
        threading.Thread(target=self.run,args=(job,key),daemon=True,name=f'full-rewrite-{job.id}').start()
        return jsonify(self.p.public(job)),202

    def finish(self,job,child,entry):
        if child.status!='succeeded' or not child.output_path:
            raise WorkflowError(child.error or '视频尚未生成成功。')
        output=self.p.safe_path(job,str(child.output_path))
        entry.update(output=str(output),status='succeeded')
        self.p.data(job)['attempts'][entry['key']]['output']=str(output)
        job.update(status='succeeded',stage='完整改写视频已生成，请预览效果',progress=100,output_path=output,error='')
        self.p.save(job)

    def run(self,job,key):
        attempt=self.p.data(job)['attempts'][key];entry=attempt['segments'][0]
        try:
            child=self.p.intervals.make_child(job,entry)
            entry['status']='running';self.p.save(job)
            self.h.run_generation(child,depth_path=Path(attempt['reference_path']) if attempt['reference_path'] else None,
                depth_reference=attempt['video_asset'],person_source='',clothing_source='',scene_source='',
                reference_images=attempt['images'] or None,video_only=not attempt['images'],options=attempt['options'])
            self.finish(job,child,entry)
        except Exception as exc:
            entry['status']='failed';self.h.job_error(job,exc);self.p.save(job)
