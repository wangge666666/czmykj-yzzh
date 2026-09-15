"""Replace disjoint intervals on the original timeline; never retime the rest."""
from __future__ import annotations

import json
import hashlib
import math
import re
import threading
from pathlib import Path

from flask import jsonify, request, send_file

from wardrobe_continuation import ffmpeg, has_audio, RUNNING
from workflow_core import WorkflowError, inspect_video, build_motion_reference_payload
from workflow_core import build_video_reference_seedance_payload
from wardrobe_rewrite_sources import rewrite_prompt


def normalize_segments(raw, duration, fps, *, require_prompts=False):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 12:
        raise WorkflowError("请添加 1–12 个改写区间。")
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(fps) or fps <= 0:
        raise WorkflowError("无法识别原片时长或帧率。")
    total_frames = round(duration * fps)
    result = []
    for item in raw:
        if not isinstance(item, dict):
            raise WorkflowError("改写区间格式无效。")
        try:
            start, end = float(item["start"]), float(item["end"])
        except (ValueError, TypeError, KeyError):
            raise WorkflowError("每段都需要填写有效的开始秒数和结束秒数。")
        if not all(math.isfinite(x) for x in (start, end)) or not 0 <= start < end <= duration + 1e-6:
            raise WorkflowError("区间需满足：0 ≤ 开始秒数 < 结束秒数 ≤ 原片时长。")
        first, last = round(start * fps), min(total_frames, round(end * fps))
        if last <= first or (last - first) / fps > 15 + 1e-6:
            raise WorkflowError("每段至少一帧、最长 15 秒；较长范围请拆成多个区间。")
        prompt = str(item.get("prompt", "")).strip()
        if len(prompt) > 1200 or (require_prompts and not prompt):
            raise WorkflowError("请为每段填写 1–1200 字的改写要求。")
        result.append({"start": first / fps, "end": last / fps, "start_frame": first,
                       "end_frame": last, "duration": (last-first)/fps, "prompt": prompt,
                       "generation_seconds": max(4, math.ceil((last-first)/fps - 1e-6)),
                       "has_after": last < total_frames})
    result.sort(key=lambda item: item["start_frame"])
    if any(right["start_frame"] < left["end_frame"] for left, right in zip(result, result[1:])):
        raise WorkflowError("改写区间不能重叠；例如 2–4 秒和 3–5 秒需要合并或调整。")
    return result


def interval_prompt(segment, extra_count=0):
    custom = re.sub(r"@\s*(视频|图片)\s*(\d+)", lambda m: f"@{m[1]}{m[2]}", segment["prompt"].strip())
    allowed = {"@视频1", "@图片1"} | ({"@图片2"} if segment["has_after"] else set())
    image_base = 1 + int(segment['has_after'])
    allowed.update(f'@图片{i+1}' for i in range(image_base, image_base+extra_count))
    if not custom or len(custom) > 1200:
        raise WorkflowError("请为每段填写 1–1200 字的改写要求。")
    if "@" in re.sub(r"@(视频|图片)\d+", "", custom) or any(x not in allowed for x in re.findall(r"@(?:视频|图片)\d+", custom)):
        raise WorkflowError("提示词引用了本段没有的素材，请使用本段的 @ 素材标签。")
    duration = segment["duration"]
    prompt = (f"仅生成用于替换原片 {segment['start']:.4f}–{segment['end']:.4f} 秒的片段，实际替换时长 {duration:.4f} 秒。"
              "@视频1提供本段及附近原片的角色身份、服装、场景、镜头上下文；需要改写的动作与事件按下方要求重新生成，"
              "不要重播整部原片，也不要把原动作当成必须复刻的模板。"
              "@图片1为本段开始衔接画面，保持人物身份、服装、道具和空间关系，平滑进入新动作，不添加片头。")
    if segment["has_after"]:
        prompt += (f"@图片2为本段结束后紧接的原片画面，新动作需在本段第 {duration:.4f} 秒前完成并自然过渡至该画面的姿态、"
                   "站位、道具、构图和光照，以便接回后面的原片。参考图仅为衔接依据，不做拼贴或静态分屏。")
    else:
        prompt += "本段到达原片末尾，结尾按改写要求收束，无需返回旧结局。"
    if segment["generation_seconds"] > duration + 1e-6:
        prompt += (f"输出前 {duration:.4f} 秒必须完整完成上述改写和衔接，之后保持结束状态以补足生成时长；"
                   f"拼接只采用输出前 {duration:.4f} 秒，不要把关键动作放在补足部分，也不要加速或延长改写区间。")
    if extra_count:
        tokens = '、'.join(f'@图片{i+1}' for i in range(image_base, image_base+extra_count))
        prompt += f'{tokens}为可选补充参考，仅按本段要求用于指定物品、场景或细节，未指定的元素沿用原片。'
    return prompt + "保持角色和服装一致，不突然变脸、跳位、增减肢体，不新增字幕、标题、水印或素材边框。\n本段改写要求：" + custom


def prepare_intervals(source, run_dir, segments, host, *, on_segment=None):
    info = inspect_video(source)
    prepared = []
    for index, segment in enumerate(segments):
        folder = run_dir / "segments" / str(index); folder.mkdir(parents=True, exist_ok=True)
        item = dict(segment)
        # Keep the target interval plus nearby context, bounded to one reference video.
        context_start = max(0, item["start"] - min(2, (15-item["duration"])/2))
        context_end = min(info.duration, context_start + 15, item["end"]+2)
        context = folder / "context.mp4"
        ffmpeg(["-i", str(source), "-ss", f"{context_start:.8f}", "-t", f"{context_end-context_start:.8f}",
                "-vf", "fps=30,scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1", "-an", "-c:v", "libx264",
                "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(context)])
        if inspect_video(context).duration < 2:
            context = host.extend_video_with_trailing_hold(context, folder/"context_padded.mp4", target_duration=2, with_audio=False)
        item["context"] = str(context)
        item.update(context_start=context_start, context_end=context_end)
        for kind, seconds in (("start_frame_image", max(0, item["start"]-1/info.fps)),
                              ("end_frame_image", item["end"])):
            if kind == "end_frame_image" and not item["has_after"]:
                continue
            image = folder / f"{kind}.png"
            ffmpeg(["-i", str(source), "-ss", f"{seconds:.8f}", "-frames:v", "1", str(image)])
            if not image.is_file():
                raise WorkflowError("边界画面提取失败，请将区间边界向内调整一帧。")
            item[kind] = str(image)
        prepared.append(item)
        if on_segment:
            on_segment(index+1)
    return prepared


def assemble_intervals(source, replacements, output):
    """Keep original frame/audio slices between equal-length replacements."""
    info = inspect_video(source)
    fps, total = info.fps, round(info.duration * info.fps)
    width, height = info.width + info.width % 2, info.height + info.height % 2
    pieces, cursor = [], 0
    for index, item in enumerate(replacements):
        start, end = item["start_frame"], item["end_frame"]
        if start < cursor or end <= start or end > total:
            raise WorkflowError("拼接区间无效，不能覆盖或移动其他原片时间。")
        if start > cursor:
            pieces.append((0, cursor/fps, (start-cursor)/fps))
        pieces.append((index+1, 0, (end-start)/fps)); cursor = end
    if cursor < total:
        pieces.append((0, cursor/fps, (total-cursor)/fps))
    inputs = [source] + [Path(item["output"]) for item in replacements]
    audio_present = [has_audio(path) for path in inputs]
    filters = []
    for index, (stream, start, duration) in enumerate(pieces):
        filters.append(f"[{stream}:v]trim=start={start:.8f}:duration={duration:.8f},setpts=PTS-STARTPTS,"
                       f"fps={fps:.8f},scale={width}:{height}:force_original_aspect_ratio=decrease,"
                       f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
                       f"tpad=stop_mode=clone:stop_duration={duration:.8f},trim=end_frame={round(duration*fps)},setpts=N/({fps:.8f}*TB)[v{index}]")
        if audio_present[stream]:
            filters.append(f"[{stream}:a]atrim=start={start:.8f}:duration={duration:.8f},asetpts=PTS-STARTPTS,"
                           f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=end_sample={round(duration*48000)},asetpts=N/SR/TB[a{index}]")
        else:
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.8f}[a{index}]")
    filters.append("".join(f"[v{i}][a{i}]" for i in range(len(pieces))) + f"concat=n={len(pieces)}:v=1:a=1[v][a]")
    ffmpeg([part for path in inputs for part in ("-i", str(path))] + ["-filter_complex", ";".join(filters),
            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(output)])
    result = inspect_video(output)
    if result.frame_count != total or abs(result.duration - info.duration) > max(.12, 2/fps):
        raise WorkflowError("区间拼接时长校验未通过，原片和已生成片段均已保留。")
    return output


class WardrobeIntervals:
    def __init__(self, parent):
        self.p, self.h = parent, parent.h

    def cloud_record(self, job, entry):
        path = job.run_dir / "attempts" / entry["key"] / str(entry["index"]) / "job.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def needs_recovery(self, job, entry):
        if entry.get("output") or entry["status"] == "pending":
            return False
        cloud = self.cloud_record(job, entry)
        if not cloud:  # run_generation persists the submission receipt before CreateTask.
            return False
        if cloud.get("cloud_status") in {"failed", "expired", "cancelled", "canceled"}:
            return False
        return bool(cloud.get("task_id") or cloud.get("output") or cloud.get("status") in {"submitting", "ambiguous"})

    def public(self, job):
        data = self.p.data(job); result = job.public()
        active = data.get("attempts", {}).get(data.get("active_attempt"), {})
        entries = active.get("segments", [])
        base = f"/api/wardrobe-continuation/jobs/{job.id}"
        public_segments = []
        for index, segment in enumerate(data["segments"]):
            item = {key: segment[key] for key in ("start", "end", "duration", "prompt", "generation_seconds", "has_after")}
            for key in ("context", "start_frame_image", "end_frame_image"):
                item[key+"_url"] = f"{base}/segments/{index}/file/{key}" if segment.get(key) else ""
            entry = entries[index] if index < len(entries) else {}
            item.update(status=entry.get("status", "prepared" if segment.get("context") else "preparing"),
                        output_url=f"{base}/segments/{index}/file/output" if entry.get("output") else "",
                        reusable=bool(entry.get("output")), submitted_prompt=entry.get("prompt", ""))
            public_segments.append(item)
        unresolved = any(self.needs_recovery(job, entry) for entry in entries)
        result.update(workflow_version=2, source_url=base+"/file/source", output_url=base+"/file/output" if job.output_path else "",
                      segments=public_segments, settings={key:data.get(key) for key in ("source_duration", "fps", "resolution", "generate_audio")},
                      last_generation={key:active.get(key) for key in ("resolution", "generate_audio")},
                      revision=data.get("active_attempt", "prepared"), needs_recovery=unresolved,
                      can_recover=job.status == "failed" and bool(entries) and (unresolved or all(e.get("output") for e in entries)))
        result.update(rewrite_workflow=data.get('rewrite_workflow', 'legacy'),
                      video_asset=data.get('video_asset', ''), video_name=data.get('video_name', ''),
                      video_group_id=data.get('video_group_id', ''),
                      mosaic_url=base+'/file/mosaic' if data.get('mosaic') else '',
                      clothing_url=base+'/file/clothing' if data.get('clothing') else '',
                      person_asset=data.get('person_asset', ''),
                      face_score_threshold=data.get('face_score_threshold', .55))
        result['extra_references'] = [dict(name=item['name'], sha256=item['sha256'], index=index,
            source_job_id=job.id, url=f"{base}/extra-references/{index}?v={item['sha256']}")
            for index, item in enumerate(data.get('extra_references', []))]
        if active.get('mode') == 'full_video':
            result.update(generation_mode='full_video', segments=[{key:s[key] for key in ('start','end','duration','prompt','generation_seconds','has_after')} for s in data['segments']])
            if job.status in RUNNING and entries:
                child=self.h.JOBS.get(entries[0]['child_id'])
                if child:result.update(progress=child.progress,stage=child.stage,logs=job.logs+child.logs)
            return result
        if job.status in RUNNING and entries:
            completed = sum(bool(entry.get("output")) for entry in entries)
            child = next((self.h.JOBS.get(entry["child_id"]) for entry in entries if entry["status"] == "running"), None)
            if child:
                result.update(progress=min(90, (completed + child.progress/100)/len(entries)*90),
                              stage=f"第 {completed+1}/{len(entries)} 段：{child.stage}", logs=job.logs+child.logs)
        return result

    def file(self, job_id, index, kind):
        try:
            job = self.p.load(job_id); data = self.p.data(job)
            if data.get("workflow_version") != 2 or kind not in {"context", "start_frame_image", "end_frame_image", "output"}:
                raise WorkflowError("未知区间文件。")
            if not 0 <= index < len(data["segments"]):
                raise WorkflowError("区间不存在。")
            if kind == "output":
                value = data["attempts"][data["active_attempt"]]["segments"][index].get("output", "")
            else:
                value = data["segments"][index].get(kind, "")
            return send_file(self.p.safe_path(job, value), as_attachment=request.args.get("download") == "1", conditional=True)
        except Exception as exc:
            return jsonify(error=str(exc)), 404

    def extra_file(self, job_id, index):
        try:
            job = self.p.load(job_id)
            references = self.p.data(job).get('extra_references', [])
            if not 0 <= index < len(references):
                raise WorkflowError('找不到该补充参考图。')
            return send_file(self.p.safe_path(job, references[index]['path']), conditional=True)
        except Exception as exc:
            return jsonify(error=str(exc)), 404

    def collect_extras(self, job, key):
        raw = request.form.get('extra_references')
        if raw is None:
            return self.p.data(job).get('extra_references', [])
        choices = json.loads(raw)
        if not isinstance(choices, list) or len(choices) > 7:
            raise WorkflowError('其他参考最多上传 7 张图片。')
        uploads = request.files.getlist('extra_images')
        if len(uploads) != sum(isinstance(item, dict) and 'upload' in item for item in choices):
            raise WorkflowError('补充参考图上传不完整，请重新选择。')
        result, seen_uploads = [], set()
        folder = job.run_dir/'materials'/key/'extras'
        for index, item in enumerate(choices):
            if not isinstance(item, dict):
                raise WorkflowError('补充参考图格式无效。')
            if 'upload' in item:
                slot = item['upload']
                if type(slot) is not int or not 0 <= slot < len(uploads) or slot in seen_uploads:
                    raise WorkflowError('补充参考图上传编号无效。')
                seen_uploads.add(slot); folder.mkdir(parents=True, exist_ok=True)
                path = self.h.save_upload(uploads[slot], folder, f'extra_{index}')
                if path.suffix.lower() not in {'.jpg', '.jpeg', '.png', '.webp'}:
                    raise WorkflowError('其他参考支持 JPG、PNG 或 WEBP 图片。')
                name = Path(uploads[slot].filename.replace('\\', '/')).name[:120]
            elif 'keep' in item:
                origin = self.p.load(item.get('source_job_id', job.id))
                saved = self.p.data(origin).get('extra_references', [])
                slot = item['keep']
                if type(slot) is not int or not 0 <= slot < len(saved) or saved[slot]['sha256'] != item.get('sha256'):
                    raise WorkflowError('补充参考图已变化，请刷新后重新选择。')
                original = self.p.safe_path(origin, saved[slot]['path'])
                folder.mkdir(parents=True, exist_ok=True)
                path = folder/f'extra_{index}{original.suffix}'
                self.h.shutil.copyfile(original, path)
                name = saved[slot]['name']
            else:
                raise WorkflowError('补充参考图来源无效。')
            result.append(dict(name=name, path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        return result

    def prepare(self):
        job = None
        try:
            raw = json.loads(request.form.get("segments", "null"))
            upload = request.files.get("reference_video")
            old = self.p.load(request.form.get("source_job_id", "")) if not upload else None
            if old and old.status in RUNNING:
                raise WorkflowError("当前任务正在运行，请等待完成。")
            material = self.p.data(old) if old else {}
            workflow = material.get('rewrite_workflow', 'legacy')
            if workflow == 'references' and request.form.get('mosaic_reviewed') != 'true':
                raise WorkflowError('请先预览打码视频并确认没有明显漏码。')
            job = self.h.new_job("wardrobe_continuation_prepare")
            if old:
                original = self.p.safe_path(old, self.p.data(old)["source"])
                source = job.run_dir / f"reference{original.suffix}"; self.h.shutil.copyfile(original, source)
            else:
                source = self.h.save_upload(upload, job.run_dir, "reference")
            info = inspect_video(source)
            segments = normalize_segments(raw, info.duration, info.fps)
            job.cast_continuity["continuation"] = {"workflow_version": 2, "source": str(source), "source_duration": info.duration,
                "fps": info.fps, "segments": segments, "attempts": {}}
            data = self.p.data(job)
            if material.get('extra_references'):
                data['extra_references'] = []
                for index, item in enumerate(material['extra_references']):
                    original = self.p.safe_path(old, item['path'])
                    destination = job.run_dir/f'extra_{index}{original.suffix}'
                    self.h.shutil.copyfile(original, destination)
                    data['extra_references'].append({**item, 'path': str(destination)})
            reference_source = source
            if workflow in {'library', 'references'}:
                data['rewrite_workflow'] = workflow
                for key in ('video_asset', 'video_name', 'video_group_id', 'face_score_threshold', 'person_asset'):
                    if key in material:
                        data[key] = material[key]
                for key in ('mosaic', 'clothing'):
                    if material.get(key):
                        original = self.p.safe_path(old, material[key])
                        destination = job.run_dir/(key+original.suffix)
                        self.h.shutil.copyfile(original, destination)
                        data[key] = str(destination)
                if workflow == 'references':
                    reference_source = self.p.safe_path(job, data.get('mosaic', ''))
            job.update(status="running", stage="正在提取各区间的参考视频和衔接画面", progress=2)
            self.p.save(job)
            def worker():
                try:
                    prepared = prepare_intervals(reference_source, job.run_dir, segments, self.h,
                        on_segment=lambda n: job.update(progress=5+90*n/len(segments), stage=f"已准备 {n}/{len(segments)} 个区间"))
                    self.p.data(job)["segments"] = prepared
                    job.update(status="succeeded", progress=100, stage="区间已准备，请预览边界并填写每段改写要求")
                except Exception as exc:
                    self.h.job_error(job, exc)
                self.p.save(job)
            threading.Thread(target=worker, daemon=True, name=f"interval-prepare-{job.id}").start()
            return jsonify(self.public(job)), 202
        except Exception as exc:
            if job:
                self.h.job_error(job, exc)
            return jsonify(error=str(exc)), 400

    def generate(self, job):
        data = self.p.data(job)
        key = request.form.get("request_id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", key):
            raise WorkflowError("缺少有效的提交标识。")
        if request.form.get("paid_confirmed") != "true":
            raise WorkflowError("请确认本次各区间的付费生成。")
        if key in data["attempts"]:
            return jsonify(self.public(job)), 202
        if job.status in RUNNING:
            raise WorkflowError("任务正在运行，请勿重复提交。")
        if request.form.get("ranges_reviewed") != "true":
            raise WorkflowError("请预览各区间的衔接画面并勾选确认。")
        workflow = data.get('rewrite_workflow', 'legacy')
        if workflow == 'references' and request.form.get('mosaic_reviewed') != 'true':
            raise WorkflowError('请先确认人脸打码视频。')
        raw = json.loads(request.form.get("segments", "null"))
        segments = normalize_segments(raw, data["source_duration"], data["fps"], require_prompts=True)
        if [(s["start_frame"],s["end_frame"]) for s in segments] != [(s["start_frame"],s["end_frame"]) for s in data["segments"]]:
            raise WorkflowError("区间已修改，请重新免费准备各段参考后再生成。")
        resolution, audio = request.form.get("resolution", "720p"), request.form.get("generate_audio") == "true"
        if resolution not in {"480p", "720p"}:
            raise WorkflowError("请选择 480p 或 720p。")
        previous = data["attempts"].get(data.get("active_attempt"), {})
        old_entries = previous.get("segments", [])
        if any(self.needs_recovery(job, e) for attempt in data["attempts"].values() for e in attempt.get("segments", [])):
            raise WorkflowError("已有片段的提交或下载尚未确认，请先恢复查询；不会重复提交付费生成。")
        partial = bool(old_entries) and not all(e.get("output") for e in old_entries)
        extras = self.collect_extras(job, key)
        images, material_key = [], ''
        if workflow == 'library':
            self.h.wardrobe_object.asset(data.get('video_asset', ''), 'Video')
            if request.form.get('person_asset') or request.files.get('clothing_image'):
                raise WorkflowError('无人物服装参考流程只接收原片和改写提示词。')
        elif workflow == 'references':
            person = request.form.get('person_asset', '').strip()
            self.h.wardrobe_object.asset(person, 'Image')
            clothing_upload = request.files.get('clothing_image')
            if clothing_upload and clothing_upload.filename:
                folder = job.run_dir/'materials'/key
                folder.mkdir(parents=True, exist_ok=True)
                clothing = self.h.save_upload(clothing_upload, folder, 'clothing')
            elif data.get('clothing'):
                clothing = self.p.safe_path(job, data['clothing'])
            else:
                raise WorkflowError('请上传服装参考图，对应 @图片2。')
            images = [person, str(clothing)]
            material_key = person + ':' + hashlib.sha256(clothing.read_bytes()).hexdigest()
        images += [str(self.p.safe_path(job, item['path'])) for item in extras]
        if extras:
            material_key += ':extras:' + ':'.join(item['sha256'] for item in extras)
        entries = []
        for index, (segment, prepared) in enumerate(zip(segments, data["segments"])):
            prompt = (rewrite_prompt(segment, workflow, prepared.get('context_start', 0), len(extras))
                      if workflow != 'legacy' else interval_prompt(segment, len(extras)))
            frames = [str(self.p.safe_path(job, prepared[k])) for k in ("start_frame_image", "end_frame_image") if prepared.get(k)]
            self.p.safe_path(job, prepared.get("context", ""))
            if len(frames) != 1 + int(segment["has_after"]):
                raise WorkflowError("衔接画面尚未准备完整，请重新准备区间。")
            if workflow == 'library' and not images:
                build_video_reference_seedance_payload(prompt=prompt, video_reference=data['video_asset'],
                    model=self.h.DEFAULT_SEEDANCE_25_MODEL, resolution=resolution, duration=segment['generation_seconds'])
            else:
                build_motion_reference_payload(prompt=prompt, image_sources=frames+images if workflow == 'legacy' else images,
                    video_reference=data['video_asset'] if workflow == 'library' else "https://validation.invalid/context.mp4", resolution=resolution, duration=segment["generation_seconds"])
            old = old_entries[index] if index < len(old_entries) else {}
            reuse = partial and old.get("output") and old.get("prompt") == segment["prompt"] and previous.get("resolution") == resolution and previous.get("generate_audio") == audio and previous.get('material_key', '') == material_key
            entry = {"key":key, "index":index, "child_id":f"{job.id}-{len(data['attempts'])+1}-{index}", "prompt":segment["prompt"],
                     "final_prompt":prompt, "duration":segment["generation_seconds"], "status":"succeeded" if reuse else "pending"}
            if reuse:
                entry["output"] = str(self.p.safe_path(job, old["output"]))
            entries.append(entry)
        for segment, prepared in zip(segments, data["segments"]):
            prepared["prompt"] = segment["prompt"]
        data.update(active_attempt=key, resolution=resolution, generate_audio=audio)
        data['extra_references'] = extras
        if workflow == 'references':
            data.update(person_asset=person, clothing=str(clothing))
        data["attempts"][key] = {"key":key, "resolution":resolution, "generate_audio":audio, "segments":entries,
                                  'material_key': material_key, 'images': images}
        job.update(status="running", stage="按原片时间顺序生成指定区间", progress=1, output_path=None, error="")
        self.p.save(job)
        count = sum(not e.get("output") for e in entries)
        job.log(f"本次生成 {count} 个区间，其余原片画面和原声按原时间保留。")
        threading.Thread(target=self.run, args=(job, key), daemon=True, name=f"interval-generate-{job.id}").start()
        return jsonify(self.public(job)), 202

    def make_child(self, job, entry):
        folder = job.run_dir / "attempts" / entry["key"] / str(entry["index"])
        folder.mkdir(parents=True, exist_ok=True)
        child = self.h.WebJob(id=entry["child_id"], kind="wardrobe_interval_segment", project=self.h.WARDROBE_SWAP_PROJECT, run_dir=folder)
        self.h.JOBS[child.id] = child
        return child

    def finish_segment(self, job, child, entry):
        if self.p.data(job)['attempts'][entry['key']].get('mode') == 'full_video':
            return self.p.full.finish(job,child,entry)
        if child.status != "succeeded" or not child.output_path:
            raise WorkflowError(child.error or "该区间尚未生成成功。")
        segment = self.p.data(job)["segments"][entry["index"]]
        clip = self.h.conform_video_duration(self.p.safe_path(job, str(child.output_path)), child.run_dir/"interval_timed.mp4",
                                             segment["duration"], with_audio=has_audio(child.output_path))
        entry.update(output=str(clip), status="succeeded")
        self.p.save(job)

    def assemble(self, job, key):
        data = self.p.data(job); entries = data["attempts"][key]["segments"]
        if data['attempts'][key].get('mode') == 'full_video':
            if not entries[0].get('output'):
                raise WorkflowError('完整视频尚未生成完成。')
            job.update(status='succeeded',progress=100,stage='完整改写视频已恢复',output_path=self.p.safe_path(job,entries[0]['output']),error='')
            self.p.save(job);return
        if not all(e.get("output") for e in entries):
            raise WorkflowError("已保存完成的片段；其余区间尚未完成，可继续生成剩余片段。")
        job.update(stage="正在按原时间拼接改写区间与保留片段", progress=92)
        replacements = [{**s, "output":str(self.p.safe_path(job, e["output"]))} for s,e in zip(data["segments"],entries)]
        output = job.run_dir / "attempts" / key / "完整成片.mp4"
        assemble_intervals(self.p.safe_path(job, data["source"]), replacements, output)
        data["attempts"][key]["output"] = str(output)
        job.update(status="succeeded", stage="指定区间已改写并拼接，总时长保持原片", progress=100, output_path=output, error="")
        self.p.save(job)

    def run(self, job, key):
        entry = None
        try:
            data = self.p.data(job); attempt = data["attempts"][key]
            for entry in attempt["segments"]:
                if entry.get("output"):
                    continue
                segment = data["segments"][entry["index"]]
                child = self.make_child(job, entry)
                entry["status"] = "running"; self.p.save(job)
                job.log(f"正在生成 {segment['start']:.3f}–{segment['end']:.3f} 秒区间。")
                options = {"prompt":entry["final_prompt"], "model":self.h.DEFAULT_SEEDANCE_25_MODEL, "resolution":attempt["resolution"],
                    "ratio":"adaptive", "duration":entry["duration"], "generate_audio":attempt["generate_audio"],
                    "watermark":False, "delete_tos_after":True, "reference_upload_strategy":"stable"}
                frames = [segment[k] for k in ("start_frame_image", "end_frame_image") if segment.get(k)]
                workflow = data.get('rewrite_workflow', 'legacy')
                kwargs = {'depth_path': Path(segment['context']), 'depth_reference': '', 'reference_images': frames}
                if workflow == 'library':
                    kwargs.update(depth_path=None, depth_reference=data['video_asset'], reference_images=attempt.get('images') or None, video_only=not attempt.get('images'))
                elif workflow == 'references':
                    kwargs['reference_images'] = attempt['images']
                else:
                    kwargs['reference_images'] += attempt.get('images', [])
                self.h.run_generation(child, person_source="", clothing_source="", scene_source="", options=options, **kwargs)
                self.finish_segment(job, child, entry)
            self.assemble(job, key)
        except Exception as exc:
            if entry and not entry.get("output"):
                entry["status"] = "failed"
            self.h.job_error(job, exc); self.p.save(job)

    def recover(self, job):
        if job.status in RUNNING:
            raise WorkflowError("任务正在运行，请等待。")
        data = self.p.data(job); key = data.get("active_attempt")
        if not key:
            raise WorkflowError("没有可恢复的区间生成任务。")
        entries = data["attempts"][key]["segments"]
        if not any(self.needs_recovery(job,e) for e in entries) and not all(e.get("output") for e in entries):
            raise WorkflowError("已完成的片段保留，可通过生成按钮继续剩余区间。")
        full = data['attempts'][key].get('mode') == 'full_video'
        job.update(status="running", stage="恢复完整视频生成结果，不重新生成" if full else "恢复已提交片段及本地拼接，不重新生成", progress=60, error="")
        self.p.save(job)
        def worker():
            try:
                for entry in entries:
                    if not self.needs_recovery(job, entry):
                        continue
                    cloud = self.cloud_record(job, entry); child = self.make_child(job, entry)
                    path=data['attempts'][key].get('reference_path') if full else data['segments'][entry['index']]['context']
                    child.depth_path = Path(path) if path else None
                    if cloud.get("output") and Path(cloud["output"]).is_file():
                        child.output_path = self.p.safe_path(job, cloud["output"]); child.status = "succeeded"
                    else:
                        child.task_id = str(cloud.get("task_id") or "")
                        if not child.task_id:
                            submitted = float(cloud.get("submitted_at") or 0)
                            if submitted <= 0:
                                raise WorkflowError("缺少提交时间，无法唯一确认任务；没有重新提交。")
                            child.task_id = self.h.api_client().recover_created_task(set(cloud.get("known_task_ids") or []),
                                model=cloud.get("model",self.h.DEFAULT_SEEDANCE_25_MODEL), created_after=submitted,
                                created_before=float(cloud.get("created_before") or submitted)+120, resolution=cloud.get("resolution","720p"),
                                ratio=cloud.get("expected_ratio",""), duration=int(cloud.get("duration") or entry["duration"]),
                                generate_audio=bool(cloud.get("generate_audio")), attempts=3, poll_interval=3)
                            if not child.task_id:
                                raise WorkflowError("暂时无法唯一确认已提交任务，请稍后恢复查询；没有重新付费。")
                            cloud["task_id"] = child.task_id
                            self.h.save_shot_manifest(child.run_dir/"job.json", cloud)
                        self.h.resume_cloud_job(child)
                    self.finish_segment(job, child, entry)
                self.assemble(job, key)
            except Exception as exc:
                self.h.job_error(job, exc); self.p.save(job)
        threading.Thread(target=worker, daemon=True, name=f"interval-recover-{job.id}").start()
        return jsonify(self.public(job)), 202
