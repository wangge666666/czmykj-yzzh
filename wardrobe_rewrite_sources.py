"""Material preparation for interval rewriting, before any paid generation."""
from __future__ import annotations

import math
import threading
from pathlib import Path

from flask import jsonify, request
from workflow_core import WorkflowError, inspect_video, validate_seedance_reference_video


class RewriteSources:
    def __init__(self, parent, app):
        self.p, self.h = parent, parent.h
        app.add_url_rule('/api/wardrobe-continuation/source', 'rewrite_source', self.prepare, methods=['POST'])
        # Use the same durable upload receipts and quota handling as the other editors.
        for route, method, name in [('video-library', 'GET', 'video_library'),
                                    ('upload-video', 'POST', 'upload_video'),
                                    ('upload-status/<key>', 'GET', 'upload_status')]:
            def handler(name=name, **kwargs):
                return getattr(self.h.wardrobe_object, name)(**kwargs)
            app.add_url_rule('/api/wardrobe-continuation/'+route, 'rewrite_'+name, handler, methods=[method])

    def prepare(self):
        job = None
        try:
            workflow = request.form.get('rewrite_workflow', '')
            if workflow not in {'library', 'references'}:
                raise WorkflowError('请选择无人物服装参考或有人物服装参考流程。')
            uri = request.form.get('video_asset', '').strip()
            threshold = float(request.form.get('face_score_threshold', '.55'))
            if not math.isfinite(threshold) or not .3 <= threshold <= .9:
                raise WorkflowError('人脸检测阈值需在 0.30–0.90 之间。')
            raw = self.h.wardrobe_object.asset(uri, 'Video') if workflow == 'library' else None
            old = None
            if workflow == 'references' and not request.files.get('reference_video'):
                old = self.p.load(request.form.get('source_job_id', ''))
                if old.status in {'running', 'queued', 'submitted'}:
                    raise WorkflowError('原片正在处理，请等待完成。')
            job = self.h.new_job('wardrobe_continuation_prepare')
            data = {'workflow_version': 2, 'rewrite_workflow': workflow, 'segments': [], 'attempts': {},
                    'face_score_threshold': threshold, 'source_duration': 0, 'fps': 30}
            job.cast_continuity['continuation'] = data
            if raw:
                value = self.h._ark_asset_value
                url = str(value(raw, 'URL', 'Url', 'url') or '')
                if not url.startswith(('https://', 'http://')):
                    raise WorkflowError('原片素材暂未提供下载地址，请刷新素材库。')
                data.update(video_asset=uri, video_name=str(value(raw, 'Name', 'name') or uri),
                            video_group_id=str(value(raw, 'GroupId', 'group_id') or ''))
                source = job.run_dir/'reference.mp4'
            elif old:
                original = self.p.safe_path(old, self.p.data(old)['source'])
                source = job.run_dir/('reference'+original.suffix)
                self.h.shutil.copyfile(original, source)
            else:
                source = self.h.save_upload(request.files.get('reference_video'), job.run_dir, 'reference')
            data['source'] = str(source)
            job.update(status='running', stage='正在准备角色库原片' if raw else '正在准备人脸打码', progress=1)
            self.p.save(job)

            def worker():
                try:
                    if raw:
                        # Keep a full local preview and validate the actual reference duration.
                        self.h.download_file(url, source)
                        info = validate_seedance_reference_video(source)
                    else:
                        info = validate_seedance_reference_video(source)
                    data.update(source_duration=info.duration, fps=info.fps)
                    if workflow == 'references':
                        mosaic = self.h.run_wardrobe_face_mosaic(job, source, score_threshold=threshold,
                            max_source_seconds=max(15, info.duration), project_label='按秒改写视频')
                        data['mosaic'] = str(mosaic)
                    job.update(status='succeeded', progress=100, stage='原片已绑定，填写改写要求后可直接生成' if raw else '打码完成，预览确认后可直接生成改写视频')
                except Exception as exc:
                    self.h.job_error(job, exc)
                self.p.save(job)
            threading.Thread(target=worker, daemon=True, name=f'rewrite-source-{job.id}').start()
            return jsonify(self.p.public(job)), 202
        except Exception as exc:
            if job:
                self.h.job_error(job, exc)
                self.p.save(job)
            return jsonify(error=str(exc)), 400


def rewrite_prompt(segment, workflow, context_start=0, extra_count=0):
    import re
    custom = re.sub(r'@\s*(视频|图片)\s*(\d+)', lambda m: f'@{m[1]}{m[2]}', segment['prompt'].strip())
    image_base = 2 if workflow == 'references' else 0
    allowed = {'@视频1'} | {f'@图片{i+1}' for i in range(image_base+extra_count)}
    if not custom or len(custom) > 1200 or '@' in re.sub(r'@(视频|图片)\d+', '', custom) or any(
            token not in allowed for token in re.findall(r'@(?:视频|图片)\d+', custom)):
        raise WorkflowError('提示词含有未绑定的素材，请使用本段实际显示的 @ 素材标签。')
    start = segment['start'] - (context_start if workflow == 'references' else 0)
    end = start + segment['duration']
    duration = segment['duration']
    text = (f"只生成原片 {segment['start']:.4f}–{segment['end']:.4f} 秒的新片段，实际替换时长 {duration:.4f} 秒。"
            f'此区间对应 @视频1 的第 {start:.4f}–{end:.4f} 秒。视频区间外的内容仅提供前后衔接上下文，不输出整部参考视频。')
    if workflow == 'library':
        text += '@视频1是角色库原片，提供人物身份、服装、场景、道具和镜头关系。保持原人物与服装，按改写要求改变本段动作或剧情。'
    else:
        text += ('@视频1是原片人脸打码后提取的参考视频，只提供场景、身体动作趋势、道具和镜头上下文。'
                 '@图片1唯一提供人物身份、五官、发型与体型；@图片2唯一提供服装。'
                 '不要继承图片的姿势、背景、排版或机位。将打码脸部还原为人物参考的清晰自然面部，不保留马赛克。')
    text += '本段开始应自然衔接参考视频对应开始时刻的人物位置、姿态、场景和光照；新动作、事件以改写要求为准，不必复刻旧动作。'
    text += (f'新动作应在输出第 {duration:.4f} 秒前完成，并平滑回到参考视频对应结束时刻的构图、站位、道具状态，以便接回原片。'
             if segment['has_after'] else '本段到达原片结尾，按新的剧情收束，无需返回旧结局。')
    if segment['generation_seconds'] > duration + 1e-6:
        text += f'只采用输出前 {duration:.4f} 秒，之后保持结束状态补足生成时长，不将关键动作放在补足部分。'
    if extra_count:
        tokens = '、'.join(f'@图片{i+1}' for i in range(image_base, image_base+extra_count))
        text += f'{tokens}为可选补充参考，仅按本段要求用于指定物品、场景或细节；未指定的元素沿用原片，不把补充图做成拼贴、分屏或额外人物。'
    return text + '保持身份和服装连续，不新增字幕、水印、分屏或参考图拼贴。\n本段改写要求：' + custom
