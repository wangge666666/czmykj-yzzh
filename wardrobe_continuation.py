"""Interval rewrite entry point, retaining recovery for legacy continuation jobs."""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
from pathlib import Path

from flask import jsonify, request, send_file, send_from_directory

from workflow_core import WorkflowError, build_motion_reference_payload, inspect_video, resolve_ffmpeg


RUNNING = {"queued", "running", "submitted"}


def ffmpeg(arguments: list[str]) -> None:
    result = subprocess.run([str(resolve_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y", *arguments],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise WorkflowError("视频处理失败：" + result.stderr[-700:])


def has_audio(path: Path) -> bool:
    result = subprocess.run([str(resolve_ffmpeg()), "-hide_banner", "-i", str(path)], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return bool(re.search(r"Stream #.*Audio:", result.stderr))


def prepare_cut(source: Path, run_dir: Path, seconds: float, host) -> dict:
    info = inspect_video(source)
    if not math.isfinite(seconds) or not 0.5 <= seconds < info.duration:
        raise WorkflowError("切点必须至少为 0.5 秒，且小于原片总时长。")
    # Export at source frame cadence; report the actual cut used to the user.
    fps = info.fps
    if not math.isfinite(fps) or fps <= 0:
        raise WorkflowError("无法识别原片帧率。")
    cut = round(seconds * fps) / fps
    if cut >= info.duration:
        raise WorkflowError("切点过于接近片尾，请向前调整至少一帧。")
    frame = run_dir / "cut_frame.png"
    ffmpeg(["-i", str(source), "-ss", f"{max(0,cut-1/fps):.8f}", "-frames:v", "1", str(frame)])
    start = max(0, cut - 8)
    context = run_dir / "context.mp4"
    ffmpeg(["-i", str(source), "-ss", f"{start:.8f}", "-t", f"{cut-start:.8f}",
            "-vf", "fps=30,scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1", "-an", "-c:v", "libx264",
            "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(context)])
    if cut-start < 2:
        context = host.extend_video_with_trailing_hold(context, run_dir/"context_padded.mp4", target_duration=2, with_audio=False)
    return {"keep_seconds": cut, "source_duration": info.duration, "source": str(source),
            "cut_frame": str(frame), "context": str(context), "fps": fps}


def assemble_continuation(source: Path, suffix: Path, output: Path, cut: float) -> Path:
    """Decode the untouched original prefix, then append the generated suffix.

    Only encoding changes the prefix; it is never passed through an AI redraw.
    Audio before the cut is from the original, after it from the new suffix.
    """
    info = inspect_video(source)
    tail = inspect_video(suffix)
    width, height = info.width + info.width % 2, info.height + info.height % 2
    fps = info.fps
    prefix_filter = f"trim=duration={cut:.8f},setpts=PTS-STARTPTS,fps={fps:.8f},pad={width}:{height},setsar=1,format=yuv420p"
    suffix_filter = f"setpts=PTS-STARTPTS,fps={fps:.8f},scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
    filters = [f"[0:v]{prefix_filter}[v0]", f"[1:v]{suffix_filter}[v1]"]
    for index, (path, duration) in enumerate(((source,cut),(suffix,tail.duration))):
        if has_audio(path):
            filters.append(f"[{index}:a]atrim=duration={duration:.8f},asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=duration={duration:.8f}[a{index}]")
        else:
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.8f}[a{index}]")
    filters.append("[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")
    ffmpeg(["-i", str(source), "-i", str(suffix), "-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-crf", "16", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(output)])
    result = inspect_video(output)
    if abs(result.duration - cut - tail.duration) > max(.12,2/fps):
        raise WorkflowError("拼接时长校验未通过，已保留原片与新后半段供恢复。")
    return output


def continuation_prompt(custom: str) -> str:
    custom = custom.strip()
    if not custom or len(custom) > 1200:
        raise WorkflowError("请用 1–1200 字描述后半段的新走向。")
    normalized = re.sub(r"@\s*(视频|图片)\s*(\d+)", lambda match: f"@{match[1]}{match[2]}", custom)
    if "@" in re.sub(r"@(视频|图片)\d+", "", normalized) or any(token not in {"@视频1","@图片1"} for token in re.findall(r"@(?:视频|图片)\d+", normalized)):
        raise WorkflowError("这里只能引用 @视频1（切点前的片段）和 @图片1（切点画面）。")
    return ("只生成从切点开始的新后续片段，不要重播、概括或改写已经发生的前半段。"
            "@视频1只提供切点之前的角色、场景、动作趋势和镜头上下文，不是新后续的动作模板。"
            "@图片1为原片切点前最后画面，新片段开头应自然衔接该画面的角色、姿态、道具、场景与光照，"
            "不要把参考画面作为静态拼贴或额外片头。保持已有角色身份和服装一致；后续动作与事件按以下新走向展开。"
            "仅在新走向要求时改变场景或机位，转折过程应连贯，不突然变脸、跳位或增减肢体。"
            "不新增字幕、标题、水印、分屏或素材边框。\n后半段新走向：" + normalized)


class WardrobeContinuation:
    def __init__(self, app, host):
        self.h = host
        self.lock = threading.RLock()
        from wardrobe_intervals import WardrobeIntervals
        self.intervals = WardrobeIntervals(self)
        from wardrobe_full_rewrite import FullVideoRewrite
        self.full = FullVideoRewrite(self)
        from wardrobe_rewrite_sources import RewriteSources
        self.sources = RewriteSources(self, app)
        app.add_url_rule("/projects/wardrobe-continuation", "wardrobe_continuation_page", lambda: send_from_directory(host.PROJECT_DIR/"web", "wardrobe_continuation.html"))
        for suffix, method, handler in (("latest","GET",self.latest),("prepare","POST",self.prepare),
                                         ("generate","POST",self.generate),("recover","POST",self.recover)):
            app.add_url_rule(f"/api/wardrobe-continuation/{suffix}", f"continuation_{suffix}", handler, methods=[method])
        app.add_url_rule("/api/wardrobe-continuation/jobs/<job_id>", "continuation_job", self.get)
        app.add_url_rule("/api/wardrobe-continuation/jobs/<job_id>/file/<kind>", "continuation_file", self.file)
        app.add_url_rule("/api/wardrobe-continuation/jobs/<job_id>/segments/<int:index>/file/<kind>", "continuation_segment_file", self.intervals.file)
        app.add_url_rule("/api/wardrobe-continuation/jobs/<job_id>/extra-references/<int:index>", "continuation_extra_reference", self.intervals.extra_file)

    def data(self, job):
        return job.cast_continuity["continuation"]

    def save(self, job):
        with self.lock:
            self.h.save_shot_manifest(job.run_dir/"continuation.json", {
                "id":job.id,"status":job.status,"stage":job.stage,"progress":job.progress,
                "error":job.error,"created_at":job.created_at,"logs":job.logs,
                "data":self.data(job),"output":str(job.output_path or ""),
                "inline_cast":job.cast_continuity.get("inline_cast", []),
            })

    def load(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{12}", str(job_id)):
            raise WorkflowError("后半段改写任务 ID 无效。")
        with self.lock:
            existing = self.h.JOBS.get(job_id)
            if existing and existing.kind == "wardrobe_continuation_prepare":
                return existing
            files = list((self.h.PROJECT_DIR/"runs").glob(f"20*_web_wardrobe_continuation_prepare_{job_id}/continuation.json"))
            if not files:
                raise WorkflowError("找不到后半段改写任务。")
            path = files[0]
            record = json.loads(path.read_text(encoding="utf-8"))
            job = self.h.WebJob(id=job_id,kind="wardrobe_continuation_prepare",project=self.h.WARDROBE_SWAP_PROJECT,run_dir=path.parent,
                               status=record["status"],stage=record["stage"],progress=record["progress"],error=record.get("error", ""),
                               logs=record.get("logs",[]),created_at=record["created_at"],cast_continuity={"continuation":record["data"],"inline_cast":record.get("inline_cast",[])})
            if record.get("output"):
                job.output_path = self.safe_path(job,record["output"])
            if job.status in RUNNING:
                job.update(status="failed",stage="任务中断，可恢复查询或本地拼接",error="服务已重启，点击恢复任务继续；不会重新提交付费生成。")
            self.h.JOBS[job_id] = job
            return job

    def safe_path(self, job, value):
        path = Path(value).resolve()
        if not path.is_relative_to(job.run_dir.resolve()) or not path.is_file():
            raise WorkflowError("任务文件不存在或不属于当前任务。")
        return path

    def public(self, job):
        if self.data(job).get("workflow_version") == 2:
            return self.intervals.public(job)
        result = job.public()
        data = self.data(job)
        base = f"/api/wardrobe-continuation/jobs/{job.id}/file/"
        result.update({"source_url":base+"source", "cut_frame_url":base+"cut_frame" if data.get("cut_frame") else "",
                       "context_url":base+"context" if data.get("context") else "", "output_url":base+"output" if job.output_path else "",
                       "suffix_url":base+"suffix" if data.get("suffix") else "", "settings":{key:data.get(key) for key in ("keep_seconds","source_duration","suffix_seconds","prompt","resolution","generate_audio")},
                       "revision":data.get("active_attempt", "prepared"),
                       "can_recover":bool(data.get("active_attempt")) and job.status=="failed"})
        active = data.get("active_attempt")
        if active and job.status in RUNNING and job.progress < 90:
            child = self.h.JOBS.get(data["attempts"][active]["child_id"])
            if child:
                result.update(progress=child.progress,stage=child.stage,logs=job.logs+child.logs)
        return result

    def get(self, job_id):
        try: return jsonify(self.public(self.load(job_id)))
        except Exception as exc: return jsonify(error=str(exc)),404

    def latest(self):
        files = sorted((self.h.PROJECT_DIR/"runs").glob("20*_web_wardrobe_continuation_prepare_*/continuation.json"),reverse=True)
        return self.get(files[0].parent.name.rsplit("_",1)[-1]) if files else jsonify(id="")

    def file(self,job_id,kind):
        try:
            job = self.load(job_id)
            if kind not in {"source","mosaic","clothing","cut_frame","context","suffix","output"}:
                raise WorkflowError("未知文件类型。")
            value = str(job.output_path or "") if kind=="output" else self.data(job).get(kind,"")
            return send_file(self.safe_path(job,value),as_attachment=request.args.get("download")=="1",conditional=True)
        except Exception as exc: return jsonify(error=str(exc)),404

    def prepare(self):
        if "segments" in request.form:
            return self.intervals.prepare()
        try:
            seconds = float(request.form.get("keep_seconds",""))
            if not math.isfinite(seconds): raise WorkflowError("切点必须是有效秒数。")
            upload = request.files.get("reference_video")
            old = self.load(request.form.get("source_job_id","")) if not upload else None
            if old and old.status in RUNNING: raise WorkflowError("当前任务正在运行，请等待完成。")
            job = self.h.new_job("wardrobe_continuation_prepare")
            if old:
                original = self.safe_path(old,self.data(old)["source"])
                source = job.run_dir/f"reference{original.suffix}"
                self.h.shutil.copyfile(original,source)
            else:
                source = self.h.save_upload(upload,job.run_dir,"reference")
            info = inspect_video(source)
            if not .5 <= seconds < info.duration: raise WorkflowError("切点必须至少为 0.5 秒，且小于原片总时长。")
            job.cast_continuity["continuation"] = {"source":str(source),"keep_seconds":seconds,"source_duration":info.duration,"attempts":{}}
            job.update(status="running",stage="正在准备切点画面和前文片段",progress=5)
            self.save(job)
            def worker():
                try:
                    self.data(job).update(prepare_cut(source,job.run_dir,seconds,self.h))
                    job.update(status="succeeded",stage="切点已准备，请预览并填写后半段走向",progress=100)
                except Exception as exc: self.h.job_error(job,exc)
                self.save(job)
            threading.Thread(target=worker,daemon=True,name=f"continuation-prepare-{job.id}").start()
            return jsonify(self.public(job)),202
        except Exception as exc: return jsonify(error=str(exc)),400

    def generate(self):
        try:
            with self.lock:
                job = self.load(request.form.get("source_job_id",""))
                data = self.data(job)
                if request.form.get('generation_mode') == 'full_video':
                    return self.full.generate(job)
                if data.get("workflow_version") == 2:
                    return self.intervals.generate(job)
                key = request.form.get("request_id","")
                if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}",key): raise WorkflowError("缺少有效的提交标识。")
                if request.form.get("paid_confirmed")!="true": raise WorkflowError("请确认本次付费生成。")
                if key in data["attempts"]: return jsonify(self.public(job)),202
                if job.status in RUNNING: raise WorkflowError("任务正在运行，请勿重复提交。")
                if request.form.get("cut_reviewed")!="true": raise WorkflowError("请预览切点画面并勾选确认。")
                for attempt in data["attempts"].values():
                    cloud_file = job.run_dir/"attempts"/attempt["key"]/"job.json"
                    if cloud_file.is_file() and json.loads(cloud_file.read_text(encoding="utf-8")).get("status") in {"submitting","ambiguous"}:
                        raise WorkflowError("上次提交状态尚不明确，请先恢复查询，不能重复付费提交。")
                prompt = continuation_prompt(request.form.get("prompt",""))
                duration = int(request.form.get("suffix_seconds","8"))
                if not 4<=duration<=15: raise WorkflowError("新后半段时长支持 4–15 秒。")
                resolution = request.form.get("resolution","720p")
                if resolution not in {"480p","720p"}: raise WorkflowError("请选择 480p 或 720p。")
                frame = self.safe_path(job,data.get("cut_frame",""))
                self.safe_path(job,data.get("context",""))
                build_motion_reference_payload(prompt=prompt,image_sources=[str(frame)],video_reference="https://validation.invalid/context.mp4",resolution=resolution,duration=duration)
                data.update(prompt=request.form["prompt"],suffix_seconds=duration,resolution=resolution,generate_audio=request.form.get("generate_audio")=="true")
                child_dir = job.run_dir/"attempts"/key
                child_dir.mkdir(parents=True,exist_ok=True)
                child = self.h.WebJob(id=f"{job.id}-{len(data['attempts'])+1}",kind="wardrobe_continuation_suffix",project=self.h.WARDROBE_SWAP_PROJECT,run_dir=child_dir)
                self.h.JOBS[child.id] = child
                data["attempts"][key] = {"key":key,"child_id":child.id,"duration":duration}
                data["active_attempt"] = key
                data.pop("suffix", None)
                job.update(output_path=None,status="running",stage="只生成后半段，前半段保留原片",progress=1,error="")
                self.save(job)
                options = {"prompt":prompt,"model":self.h.DEFAULT_SEEDANCE_25_MODEL,"resolution":resolution,"ratio":"adaptive","duration":duration,
                           "generate_audio":data["generate_audio"],"watermark":False,"delete_tos_after":True,"reference_upload_strategy":"stable"}
                def worker():
                    try:
                        self.h.run_generation(child,depth_path=Path(data["context"]),depth_reference="",person_source="",clothing_source="",scene_source="",reference_images=[str(frame)],options=options)
                        self.finish(job,child,key)
                    except Exception as exc:
                        self.h.job_error(job,exc);self.save(job)
                threading.Thread(target=worker,daemon=True,name=f"continuation-generate-{job.id}").start()
                return jsonify(self.public(job)),202
        except Exception as exc: return jsonify(error=str(exc)),400

    def finish(self,job,child,key):
        try:
            if child.status!="succeeded" or not child.output_path:
                raise WorkflowError(child.error or "新后半段尚未生成成功。")
            job.update(stage="正在拼接保留的原片与新后半段",progress=92)
            data = self.data(job)
            data["suffix"] = str(child.output_path)
            self.save(job)
            tail = self.h.conform_video_duration(child.output_path,child.run_dir/"suffix_timed.mp4",data["attempts"][key]["duration"],with_audio=has_audio(child.output_path))
            data["suffix"] = str(tail)
            output = assemble_continuation(self.safe_path(job,data["source"]),tail,child.run_dir/"完整成片.mp4",data["keep_seconds"])
            data["attempts"][key]["output"] = str(output)
            job.update(output_path=output,status="succeeded",stage="前半段原片与新后半段已拼接完成",progress=100,error="")
        except Exception as exc: self.h.job_error(job,exc)
        self.save(job)

    def recover(self):
        try:
            with self.lock:
                job = self.load(request.form.get("source_job_id",""))
                if self.data(job).get("workflow_version") == 2:
                    return self.intervals.recover(job)
                if job.status in RUNNING: raise WorkflowError("任务正在运行，请等待。")
                data = self.data(job); key = data.get("active_attempt")
                if not key: raise WorkflowError("没有可恢复的生成任务，请重新准备切点。")
                run_dir = job.run_dir/"attempts"/key
                cloud = json.loads((run_dir/"job.json").read_text(encoding="utf-8"))
                child = self.h.WebJob(id=data["attempts"][key]["child_id"],kind="wardrobe_continuation_suffix",project=self.h.WARDROBE_SWAP_PROJECT,run_dir=run_dir,
                                     task_id=str(cloud.get("task_id") or ""),depth_path=Path(data["context"]))
                self.h.JOBS[child.id]=child
                job.update(status="running",stage="正在恢复查询或拼接，不重新生成",progress=60,error="")
                self.save(job)
                def worker():
                    try:
                        saved = cloud.get("output")
                        if saved and Path(saved).is_file():
                            child.output_path=self.safe_path(job,saved);child.status="succeeded"
                        else:
                            if not child.task_id:
                                child.task_id=self.h.api_client().recover_created_task(set(cloud.get("known_task_ids") or []),model=cloud.get("model",self.h.DEFAULT_SEEDANCE_25_MODEL),created_after=float(cloud.get("submitted_at") or 0),created_before=float(cloud.get("created_before") or cloud.get("submitted_at") or 0)+120,resolution=cloud.get("resolution","720p"),ratio=cloud.get("expected_ratio",""),duration=int(cloud.get("duration") or 8),generate_audio=bool(cloud.get("generate_audio")),attempts=3,poll_interval=3)
                                if not child.task_id: raise WorkflowError("暂时无法唯一确认上次任务；没有重新提交，请稍后恢复查询。")
                            self.h.resume_cloud_job(child)
                        self.finish(job,child,key)
                    except Exception as exc:
                        self.h.job_error(job,exc);self.save(job)
                threading.Thread(target=worker,daemon=True,name=f"continuation-recover-{job.id}").start()
                return jsonify(self.public(job)),202
        except Exception as exc: return jsonify(error=str(exc)),400
