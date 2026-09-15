"""Isolated character motion transfer workspace and durable generation records."""
from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path

import cv2
import numpy as np
from flask import jsonify, request, send_file, send_from_directory

from workflow_core import WorkflowError, build_motion_reference_payload
from motion_video import prepare_motion_video_reference


PROJECT = "character_motion_transfer"
RUNNING = {"queued", "running", "submitted"}
CLOUD_FAILED = {"failed", "cancelled", "canceled", "expired"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


class MotionTransfer:
    PROJECT = PROJECT
    KIND = "motion_prepare"
    API = "/api/motion-transfer"
    PAGE = "motion-transfer"
    TEMPLATE = "motion_transfer.html"
    MANIFEST = "motion_transfer.json"

    def __init__(self, app, host):
        self.h = host
        self.lock = threading.RLock()
        app.add_url_rule(f"/projects/{self.PAGE}", f"{self.PAGE}_page", self.page)
        for path, method, handler in (
            ("latest", "GET", self.latest), ("projects", "GET", self.history),
            ("prepare", "POST", self.prepare), ("import-white", "POST", self.import_white),
            ("white-model", "POST", self.white_model), ("draft", "POST", self.draft),
            ("generate", "POST", self.generate), ("recover", "POST", self.recover),
            ("projects/<job_id>", "GET", self.status),
            ("projects/<job_id>/files/<kind>", "GET", self.file),
        ):
            app.add_url_rule(f"{self.API}/{path}", f"{self.PAGE}_{handler.__name__}", self.endpoint(handler), methods=[method])

    def page(self):
        return send_from_directory(self.h.WEB_DIR, self.TEMPLATE)

    def endpoint(self, handler):
        def wrapped(**kwargs):
            try:
                with self.lock:
                    return handler(**kwargs)
            except (WorkflowError, ValueError, TypeError, KeyError) as exc:
                return jsonify({"error": str(exc)}), 400
            except Exception as exc:
                return jsonify({"error": f"人物动作迁移操作失败：{exc}"}), 500
        return wrapped

    def meta(self, job):
        return job.cast_continuity

    def save(self, job):
        with self.lock:
            record = {key: getattr(job, key) for key in ("id", "kind", "project", "status", "stage", "progress", "error", "created_at", "source_duration", "logs", "cast_continuity")}
            for key in ("depth_path", "mosaic_path", "white_model_path"):
                record[key] = str(getattr(job, key) or "")
            self.h.save_shot_manifest(job.run_dir / self.MANIFEST, record)

    def manifests(self):
        return sorted((self.h.PROJECT_DIR / "runs").glob(f"20*_web_{self.KIND}_*/{self.MANIFEST}"), key=lambda p: p.stat().st_mtime, reverse=True)

    def load(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{12}", str(job_id)):
            raise WorkflowError("人物动作迁移项目 ID 无效。")
        existing = self.h.JOBS.get(job_id)
        if existing and existing.project == self.PROJECT and existing.kind == self.KIND:
            return existing
        for path in self.manifests():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("id") != job_id or data.get("project") != self.PROJECT:
                continue
            job = self.h.WebJob(id=job_id, kind=self.KIND, project=self.PROJECT, run_dir=path.parent)
            for key in ("status", "stage", "progress", "error", "created_at", "source_duration", "logs", "cast_continuity"):
                if key in data:
                    setattr(job, key, data[key])
            for key in ("depth_path", "mosaic_path", "white_model_path"):
                setattr(job, key, self.h._runs_record_file(data.get(key)))
            if job.status in RUNNING:
                job.update(status="failed", stage="上次处理已中断，可查询已提交任务", error="服务曾重启，请先查询已提交任务；未提交的本地处理可重新执行。")
            self.h.JOBS[job_id] = job
            return job
        raise WorkflowError("找不到人物动作迁移项目。")

    def current(self):
        return self.load(request.form.get("project_id", ""))

    def new(self):
        job = self.h.new_job(self.KIND)
        job.project = self.PROJECT
        job.cast_continuity = {"draft": {"mode": "cast", "roles": [], "prompt": "", "resolution": "720p", "generate_audio": False}, "outputs": [], "requests": {}}
        return job

    def child(self, job, output_id="white"):
        child_id = f"{job.id}-white" if output_id == "white" else output_id
        existing = self.h.JOBS.get(child_id)
        if existing and existing.project == self.PROJECT:
            return existing
        directory = job.run_dir / "white_model_task" if output_id == "white" else job.run_dir / "outputs" / output_id
        try:
            data = json.loads((directory / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if data.get("project") != self.PROJECT:
            return None
        child = self.h.WebJob(id=child_id, kind="motion_white" if output_id == "white" else "motion_output", project=self.PROJECT, run_dir=directory)
        child.output_path = self.h._runs_record_file(data.get("output"))
        child.depth_path = self.h._runs_record_file(data.get("depth"))
        child.task_id = str(data.get("task_id") or "")
        child.cloud_status = str(data.get("cloud_status") or "")
        child.recovery_action = str(data.get("recovery_action") or "")
        child.status = "succeeded" if child.output_path else "failed"
        child.stage = "已恢复任务记录"
        child.error = str(data.get("error") or "")
        if data.get("status") in {"submitting", "ambiguous"} and not child.task_id:
            child.recovery_action = "recover_seedance_submission"
        self.h.JOBS[child_id] = child
        return child

    def needs_query(self, child):
        return bool(child and not child.output_path and child.cloud_status not in CLOUD_FAILED and (child.task_id or child.recovery_action))

    def pending(self, job):
        children = [("white", self.child(job))] if not job.white_model_path else []
        children += [(item["id"], self.child(job, item["id"])) for item in self.meta(job)["outputs"]]
        return [(key, child) for key, child in children if self.needs_query(child) or (key == "white" and child and child.output_path)]

    def idle(self, job):
        if job.status in RUNNING:
            raise WorkflowError("当前项目正在处理，请等待完成。")

    def request_key(self):
        key = request.form.get("request_id", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{12,80}", key):
            raise WorkflowError("缺少有效的请求标识，请刷新页面后重试。")
        return key

    def launch(self, job, stage, worker):
        job.update(status="running", stage=stage, error="", progress=0)
        self.save(job)
        def execute():
            try:
                worker()
                job.update(status="succeeded", stage="处理完成", progress=100, error="")
            except Exception as exc:
                self.h.job_error(job, exc)
            finally:
                self.save(job)
        threading.Thread(target=execute, daemon=True, name=f"motion-{job.id}").start()

    def url(self, job, kind, path):
        return f"{self.API}/projects/{job.id}/files/{kind}" if self.h._runs_record_file(path) else ""

    def public(self, job):
        data = self.meta(job)
        draft = dict(data["draft"])
        for key in ("scene", "first", "last"):
            draft[f"{key}_url"] = self.url(job, key, draft.pop(f"{key}_path", ""))
        active = self.child(job, data.get("active_child", "white"))
        use_child = job.status in RUNNING and active and active.status in RUNNING
        outputs = []
        for item in data["outputs"]:
            child = self.child(job, item["id"])
            outputs.append({**item, "status": child.status if child else "failed", "stage": child.stage if child else "未提交或记录缺失", "error": child.error if child else "", "task_id": child.task_id if child else "", "url": self.url(job, f"output-{item['id']}", child.output_path if child else None)})
        return {"id": job.id, "status": job.status, "stage": active.stage if use_child else job.stage,
                "progress": active.progress if use_child else job.progress, "error": job.error,
                "logs": (job.logs + (active.logs if active else []))[-100:], "created_at": job.created_at,
                "source_duration": job.source_duration, "source_origin": data.get("origin", "source"),
                "source_url": self.url(job, "source", job.depth_path), "mosaic_url": self.url(job, "mosaic", job.mosaic_path),
                "white_url": self.url(job, "white", job.white_model_path), "draft": draft, "outputs": outputs,
                "needs_query": bool(self.pending(job)), "request_ids": list(data["requests"]),
                "white_task_id": self.child(job).task_id if self.child(job) else ""}

    def latest(self):
        items = self.manifests()
        if not items:
            return jsonify({"id": ""})
        data = json.loads(items[0].read_text(encoding="utf-8"))
        return jsonify(self.public(self.load(data["id"])))

    def history(self):
        items = []
        for path in self.manifests():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("project") == self.PROJECT:
                    items.append({"id": data["id"], "created_at": data["created_at"], "name": data.get("cast_continuity", {}).get("source_name", "动作项目")})
            except (OSError, ValueError, KeyError):
                continue
        return jsonify({"projects": items})

    def status(self, job_id):
        return jsonify(self.public(self.load(job_id)))

    def video_upload(self, job, field):
        upload = request.files.get(field)
        if not upload or Path(upload.filename or "").suffix.lower() not in {".mp4", ".mov"}:
            raise WorkflowError("请选择 MP4 或 MOV 视频。")
        return self.h.save_upload(upload, job.run_dir, field)

    def prepare(self):
        job = self.new()
        try:
            source = self.video_upload(job, "source")
            info = self.h.inspect_video(source)
            if info.duration < 2:
                raise WorkflowError("原片至少需要 2 秒。")
            job.depth_path = source
            self.meta(job).update(origin="source", source_name=request.files["source"].filename)
            def worker():
                source = self.h.normalize_wardrobe_source_duration(job, job.depth_path, max_seconds=15, project_label="人物动作迁移")
                job.depth_path = source
                self.h.run_wardrobe_face_mosaic(job, source, project_label="人物动作迁移")
            self.launch(job, "正在准备原片并生成人脸打码", worker)
            return jsonify(self.public(job)), 202
        except Exception as exc:
            self.h.job_error(job, exc)
            raise

    def import_white(self):
        job = self.new()
        try:
            white = self.video_upload(job, "white_video")
            info = self.h.validate_seedance_reference_video(white)
            job.white_model_path = white
            job.source_duration = info.duration
            self.meta(job).update(origin="white", source_name=request.files["white_video"].filename)
            job.update(status="succeeded", stage="已导入白膜，可进入视频制作", progress=100)
            self.save(job)
            return jsonify(self.public(job))
        except Exception as exc:
            self.h.job_error(job, exc)
            raise

    def white_model(self):
        job = self.current()
        key = self.request_key()
        if key in self.meta(job)["requests"] or job.white_model_path:
            return jsonify(self.public(job))
        self.idle(job)
        if not self.h.form_bool("paid_confirmed") or not self.h.form_bool("mosaic_reviewed"):
            raise WorkflowError("请预览确认打码视频，并确认本次白膜付费调用。")
        if not self.h._runs_record_file(job.mosaic_path) or not self.h._runs_record_file(job.depth_path):
            raise WorkflowError("请先完成原片打码。")
        if self.pending(job):
            raise WorkflowError("请先查询上次已提交任务，避免重复计费。")
        self.meta(job)["requests"][key] = "white"
        self.meta(job)["active_child"] = "white"
        def worker():
            self.h.run_wardrobe_white_model(job, job.depth_path, span=100,
                prompt_override=self.h.REAL_PERSON_SAFE_WHITE_MODEL_PROMPT,
                child_project=self.PROJECT, child_kind="motion_white", project_label="人物动作迁移",
                reference_source_factory=self.video_reference)
        self.launch(job, "正在生成动作白膜", worker)
        return jsonify(self.public(job)), 202

    def image_upload(self, job, field):
        return self.save_image_upload(job, request.files.get(field), field)

    def save_image_upload(self, job, upload, field):
        if not upload or not upload.filename:
            return ""
        if Path(upload.filename).suffix.lower() not in IMAGE_EXTENSIONS:
            raise WorkflowError("图片支持 JPG、PNG、WEBP、BMP。")
        path = self.h.save_upload(upload, job.run_dir, f"{field}_{uuid.uuid4().hex[:8]}")
        if path.stat().st_size >= 30 * 1024 * 1024:
            raise WorkflowError("单张图片必须小于 30 MB。")
        if cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR) is None:
            raise WorkflowError("上传的图片无法读取，请换一张有效图片。")
        return str(path)

    def update_draft(self, job):
        draft = dict(self.meta(job)["draft"])
        mode = request.form.get("mode", draft["mode"])
        if mode not in {"cast", "frames"}:
            raise WorkflowError("制作模式无效。")
        roles = json.loads(request.form.get("roles", json.dumps(draft["roles"])))
        if not isinstance(roles, list) or len(roles) > 8:
            raise WorkflowError("最多选择 8 位角色库人物。")
        cleaned = []
        for role in roles:
            if not isinstance(role, dict) or not re.fullmatch(r"asset://[A-Za-z0-9_-]{6,160}", str(role.get("uri", ""))):
                raise WorkflowError("请选择有效的角色库人物。")
            cleaned.append({"uri": role["uri"], "name": str(role.get("name", "角色"))[:100], "role": str(role.get("role", ""))[:100]})
        if len({role["uri"] for role in cleaned}) != len(cleaned):
            raise WorkflowError("同一角色不要重复添加，请为每位人物选择独立素材。")
        prompt = request.form.get("prompt", draft["prompt"]).strip()
        if len(prompt) > 1600:
            raise WorkflowError("可编辑提示词最多 1600 字。")
        resolution = request.form.get("resolution", draft["resolution"])
        if resolution not in {"480p", "720p"}:
            raise WorkflowError("请选择 480p 或 720p。")
        draft.update(mode=mode, roles=cleaned, prompt=prompt, resolution=resolution,
                     generate_audio=self.h.form_bool("generate_audio", draft.get("generate_audio", False)))
        for field in ("scene", "first", "last"):
            uploaded = self.image_upload(job, field)
            if uploaded:
                draft[f"{field}_path"] = uploaded
            elif self.h.form_bool(f"clear_{field}"):
                draft.pop(f"{field}_path", None)
        self.meta(job)["draft"] = draft
        return draft

    def draft(self):
        job = self.current()
        self.idle(job)
        self.update_draft(job)
        self.save(job)
        return jsonify(self.public(job))

    def references(self, draft, *, validate_roles=True):
        def image(field):
            path = self.h._runs_record_file(draft.get(f"{field}_path"))
            if not path:
                raise WorkflowError(f"请上传{'场景图' if field == 'scene' else '首帧图'}。")
            return str(path)
        if draft["mode"] == "cast":
            if not 1 <= len(draft["roles"]) <= 8:
                raise WorkflowError("请从角色库选择 1–8 位人物。")
            images = [image("scene")]
            for role in draft["roles"]:
                if validate_roles:
                    raw = self.h.ark_assets_client().get_asset(role["uri"][8:])
                    if self.h._ark_asset_value(raw, "Status", "status") != "Active" or self.h._ark_asset_value(raw, "AssetType", "asset_type") not in {"Image", ""}:
                        raise WorkflowError(f"{role['name']}尚未审核通过或不是图片素材，请刷新角色库。")
                images.append(role["uri"])
            mapping = "@图片1仅提供目标场景。" + "".join(f"@图片{i + 2}对应{r['role'] or f'原片人物{i + 1}'}，仅提供目标身份与外观，不改变动作。" for i, r in enumerate(draft["roles"]))
        else:
            images = [image("first")]
            mapping = "@图片1作为目标首帧画面参考，保持其人物身份、场景与构图。"
            if draft.get("last_path"):
                last = self.h._runs_record_file(draft["last_path"])
                if not last:
                    raise WorkflowError("尾帧图片已丢失，请重新上传或移除。")
                images.append(str(last))
                mapping += "@图片2作为目标尾帧画面参考，动作自然过渡到该画面。"
        prompt = draft["prompt"]
        if not prompt or "【已移除" in prompt:
            raise WorkflowError("请填写提示词，并修正已移除的图片引用。")
        for index in re.findall(r"@图片\s*(\d+)", prompt):
            if not 1 <= int(index) <= len(images):
                raise WorkflowError(f"@图片{index}没有对应素材，请重新选择引用。")
        if any(int(index) != 1 for index in re.findall(r"@视频\s*(\d+)", prompt)):
            raise WorkflowError("当前动作视频仅有 @视频1。")
        contract = "@视频1是白膜动作参考，只提取姿态、动作顺序、节奏、移动与互动，不继承白膜外观或绿幕。不要增加或删减动作，人物身份全程稳定，输出正常彩色视频，无字幕、水印。"
        return images, f"{contract}\n{mapping}\n用户要求：{prompt}"

    def generate(self):
        job = self.current()
        key = self.request_key()
        if key in self.meta(job)["requests"]:
            return jsonify(self.public(job)), 202
        self.idle(job)
        if not self.h.form_bool("paid_confirmed") or not self.h.form_bool("white_reviewed"):
            raise WorkflowError("请预览确认白膜，并确认本次成片付费调用。")
        if not self.h._runs_record_file(job.white_model_path):
            raise WorkflowError("请先生成或上传白膜视频。")
        if self.pending(job):
            raise WorkflowError("请先查询已提交的任务，确认结果后再生成新片。")
        draft = self.update_draft(job)
        self.save(job)
        images, prompt = self.references(draft)
        # Validate prompt and image payload before recording any paid attempt.
        build_motion_reference_payload(prompt=prompt, image_sources=images, video_reference="https://validation.invalid/white.mp4", resolution=draft["resolution"])
        self.h.validate_seedance_reference_video(job.white_model_path)
        output_id = uuid.uuid4().hex[:12]
        directory = job.run_dir / "outputs" / output_id
        directory.mkdir(parents=True, exist_ok=True)
        self.h.save_shot_manifest(directory / "motion_inputs.json", {"mode": draft["mode"], "prompt": prompt, "image_sources": images,
            "white_video": str(job.white_model_path), "roles": draft["roles"] if draft["mode"] == "cast" else [],
            "resolution": draft["resolution"], "generate_audio": draft["generate_audio"]})
        child = self.h.WebJob(id=output_id, kind="motion_output", project=self.PROJECT, run_dir=directory, depth_path=job.white_model_path)
        self.h.JOBS[output_id] = child
        self.meta(job)["requests"][key] = output_id
        self.meta(job)["outputs"].append({"id": output_id, "mode": draft["mode"], "prompt": draft["prompt"], "created_at": child.created_at})
        self.meta(job)["active_child"] = output_id
        options = {"model": self.h.DEFAULT_SEEDANCE_25_MODEL, "resolution": draft["resolution"], "ratio": "adaptive", "duration": -1,
                   "generate_audio": draft["generate_audio"], "watermark": False, "prompt": prompt, "reference_upload_strategy": "stable",
                   "reference_upload_context": "人物动作迁移", "verify_tos_public": True, "delete_tos_after": True}
        def worker():
            child.update(status="running")
            self.h.run_generation(child, depth_path=job.white_model_path, depth_reference="", person_source="", clothing_source="", scene_source="", options=options, reference_images=images, reference_source_factory=self.video_reference)
            if child.status != "succeeded":
                raise WorkflowError(child.error or "成片尚未完成，请查询任务状态。")
        self.launch(job, "正在生成人物动作迁移成片", worker)
        return jsonify(self.public(job)), 202

    def video_reference(self, source, *, on_log=None):
        return prepare_motion_video_reference(source, self.h, on_log=on_log)

    def recover(self):
        job = self.current()
        self.idle(job)
        pending = self.pending(job)
        if not pending:
            raise WorkflowError("没有需要查询的已提交任务。")
        def worker():
            for output_id, child in pending:
                self.meta(job)["active_child"] = output_id
                if not child.task_id:
                    record = json.loads((child.run_dir / "job.json").read_text(encoding="utf-8"))
                    task_id = self.h.api_client().recover_created_task(
                        set(record.get("known_task_ids") or []), model=record.get("model", ""),
                        created_after=float(record.get("submitted_at") or 0),
                        created_before=float(record.get("created_before") or record.get("submitted_at") or 0) + 120,
                        resolution=record.get("resolution", ""), ratio=record.get("expected_ratio", ""),
                        duration=max(0, int(record.get("duration") or 0)), generate_audio=bool(record.get("generate_audio")),
                        attempts=1,
                    )
                    if not task_id:
                        raise WorkflowError("尚未找到唯一匹配的已提交任务，未重新提交付费请求，请稍后再查询。")
                    child.task_id = task_id
                if not child.output_path:
                    self.h.resume_cloud_job(child)
                if child.status != "succeeded" or not child.output_path:
                    raise WorkflowError(child.error or "查询尚未完成。")
                if output_id == "white":
                    job.white_model_path = self.h.conform_video_duration(child.output_path, job.run_dir / "white_model.mp4", job.source_duration, with_audio=False)
        self.launch(job, "正在查询已有任务，不会新建付费任务", worker)
        return jsonify(self.public(job)), 202

    def file(self, job_id, kind):
        job = self.load(job_id)
        paths = {"source": job.depth_path, "mosaic": job.mosaic_path, "white": job.white_model_path}
        paths.update({key: self.meta(job)["draft"].get(f"{key}_path") for key in ("scene", "first", "last")})
        if kind.startswith("output-"):
            output_id = kind[7:]
            if any(item["id"] == output_id for item in self.meta(job)["outputs"]):
                child = self.child(job, output_id)
                paths[kind] = child.output_path if child else None
        path = self.h._runs_record_file(paths.get(kind))
        if not path:
            return jsonify({"error": "文件尚未生成或不存在。"}), 404
        return send_file(path, conditional=True, as_attachment=request.args.get("download") == "1")
