"""Object replacement: restore actors from references, or use a video asset directly."""
from __future__ import annotations
import inline_cast

import json
import hashlib
import re
import threading
from pathlib import Path
import requests
from flask import jsonify, request

from wardrobe_dynamic import object_final_prompt, scene_final_prompt
from workflow_core import ArkAPIError, WorkflowError, build_motion_reference_payload, validate_seedance_reference_video


MODE = "dynamic_object"
RUNNING = {"queued", "running", "submitted"}


def upload_failure(exc, *, submitting=False):
    """A provider rejection is definitive; transport loss / 5xx is ambiguous."""
    raw = str(exc)
    code = str(getattr(exc, "code", ""))
    http = getattr(exc, "status_code", None)
    quota = code == "QuotaSharedPoolExceeded" or "QuotaSharedPoolExceeded" in raw
    if quota:
        return {"status": "quota_full", "error_code": "QuotaSharedPoolExceeded", "http_status": http or 429,
                "error": "角色库共享素材额度已满，本次新增入库被拒绝。可选择已入库的原片继续制作；上传新视频需先释放不再需要的素材额度，或由账号管理员调整项目素材配额。反复重试或新建素材组无法增加额度。",
                "error_detail": raw}
    rejected = isinstance(exc, ArkAPIError) and 400 <= exc.status_code < 500 and exc.status_code != 408
    return {"status": "failed" if rejected or not submitting else "uncertain", "error": raw,
            "error_code": code, "http_status": http}


def video_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class WardrobeObject:
    """Shared original-library / character-proxy flows for object and scene edits."""
    upload_lock = threading.RLock()

    def __init__(self, app, host, mode=MODE):
        self.h = host
        if mode not in {"dynamic_object", "dynamic_scene"}:
            raise ValueError("Unsupported replacement mode")
        self.mode = mode
        self.scene = mode == "dynamic_scene"
        self.namespace = "scene" if self.scene else "object"
        self.workflow_key = f"{self.namespace}_workflow"
        self.label = "背景" if self.scene else "物品"
        self.image_label = "场景参考图" if self.scene else "物品参考图"
        self.white_label = "绿底白模" if self.scene else "主角白模"
        self.lock = self.upload_lock
        for path, method, handler in (
            ("latest", "GET", self.latest), ("import-white", "POST", self.import_white),
            ("select-video", "POST", self.select_video), ("video-library", "GET", self.video_library),
            ("upload-video", "POST", self.upload_video), ("upload-status/<key>", "GET", self.upload_status),
            ("latest-upload", "GET", self.latest_upload),
        ):
            app.add_url_rule(f"/api/wardrobe-{self.namespace}/{path}", f"wardrobe_{self.namespace}_" + path, handler, methods=[method])

    @staticmethod
    def data(job):
        return job.cast_continuity.get("wardrobe_dynamic", {})

    def save(self, job):
        self.h.persist_wardrobe_swap_job(job, self.mode)

    def new_source(self, workflow):
        job = self.h.new_job(f"wardrobe_prepare_{self.mode}")
        job.cast_continuity["wardrobe_dynamic"] = {
            "workflow_version": 2, self.workflow_key: workflow, "requests": {}, "description": "",
            "mosaic_scope": "all_faces" if workflow == "references" else "original_asset",
        }
        return job

    def asset(self, uri, kind):
        if not re.fullmatch(r"asset://asset-[A-Za-z0-9_-]{6,120}", str(uri)):
            raise WorkflowError("请选择有效的角色库素材。")
        raw = self.h.ark_assets_client().get_asset(uri[8:])
        value = self.h._ark_asset_value
        if value(raw, "Status", "status") != "Active" or value(raw, "AssetType", "asset_type") != kind:
            raise WorkflowError(f"所选素材必须是审核通过（Active）的{ '视频' if kind == 'Video' else '人物图片'}。请刷新角色库查看状态。")
        return raw

    def latest(self):
        workflow = request.args.get("workflow", "references")
        if workflow not in {"references", "library"}:
            return jsonify(error=f"未知{self.label}替换流程。"), 400
        # Read each branch independently; legacy object-white-model jobs never
        # become character-white-model jobs merely because the UI was upgraded.
        candidates = []
        for path in (self.h.PROJECT_DIR / "runs").glob(f"20*_web_wardrobe_prepare_{self.mode}_*/wardrobe_manifest.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                data = record.get("wardrobe_dynamic", {})
                if data.get("workflow_version") == 2 and data.get(self.workflow_key) == workflow:
                    candidates.append((record.get("created_at", ""), path.stat().st_mtime, record["local_job_id"]))
            except (OSError, ValueError, KeyError):
                continue
        if not candidates:
            return jsonify(id="")
        job = self.h.restore_wardrobe_swap_job(max(candidates)[2])
        return jsonify(job.public() if job else {"id": ""})

    def import_white(self):
        job = None
        try:
            job = self.new_source("references")
            path = self.h.save_upload(request.files.get("white_video"), job.run_dir, "white_uploaded")
            info = validate_seedance_reference_video(path)
            job.white_model_path = path
            job.source_duration = info.duration
            self.data(job)["white_uploaded"] = True
            job.update(status="succeeded", progress=100, stage=f"{self.white_label}已上传，请预览后添加人物、服装" + ("及场景参考" if self.scene else "参考"))
            self.save(job)
            return jsonify(job.public()), 201
        except Exception as exc:
            if job:
                self.h.job_error(job, exc)
            return jsonify(error=str(exc)), 400

    def select_video(self):
        try:
            uri = request.form.get("video_asset", "").strip()
            raw = self.asset(uri, "Video")
            job = self.new_source("library")
            value = self.h._ark_asset_value
            self.data(job).update(video_asset=uri, video_name=str(value(raw, "Name", "name") or uri),
                                  video_url=str(value(raw, "URL", "Url", "url") or ""),
                                  video_group_id=str(value(raw, "GroupId", "group_id") or ""))
            job.update(status="succeeded", progress=100, stage=f"已选择原片视频素材，可直接设置{self.label}替换")
            self.save(job)
            return jsonify(job.public()), 201
        except Exception as exc:
            return jsonify(error=str(exc)), 400

    def library_records(self):
        if not self.h.ark_assets_configured():
            raise WorkflowError("尚未配置火山素材库 AK/SK，无法读取或上传视频。")
        client = self.h.ark_assets_client()
        groups, assets, errors = [], [], []
        value = self.h._ark_asset_value
        # Preserve the official group type. Never relabel a real-person asset
        # to evade that group's authorization or consistency checks.
        for group_type in ("AIGC", "LivenessFace"):
            try:
                raw_groups = client.list_asset_groups(group_type=group_type)
                ids = []
                for raw in raw_groups:
                    gid = str(value(raw, "Id", "id") or "")
                    if not gid:
                        continue
                    ids.append(gid)
                    groups.append({"id": gid, "name": str(value(raw, "Name", "name") or gid), "group_type": group_type})
                if not ids:
                    continue
                for raw in client.list_assets(group_type=group_type, group_ids=ids, statuses=["Active", "Processing", "Failed"]):
                    if value(raw, "AssetType", "asset_type") != "Video":
                        continue
                    aid = str(value(raw, "Id", "id") or "")
                    assets.append({"id": aid, "uri": "asset://" + aid, "group_id": value(raw, "GroupId", "group_id"),
                                   "name": str(value(raw, "Name", "name") or aid), "status": value(raw, "Status", "status"),
                                   "url": str(value(raw, "URL", "Url", "url") or "")})
            except Exception as exc:
                errors.append(f"{group_type}: {exc}")
        return {"groups": groups, "assets": assets, "message": "；".join(errors)}

    def video_library(self):
        try:
            return jsonify(self.library_records())
        except Exception as exc:
            return jsonify(error=str(exc)), 400

    def receipt_path(self, key):
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", key):
            raise WorkflowError("缺少有效的入库提交标识。")
        return self.h.PROJECT_DIR / "runs" / "wardrobe_video_uploads" / f"{key}.json"

    def upload_status(self, key):
        try:
            path = self.receipt_path(key)
            if not path.is_file():
                return jsonify(error="尚未收到这次入库提交。"), 404
            record = json.loads(path.read_text(encoding="utf-8"))
            # Also repair receipts written by the previous release, which
            # incorrectly labelled a quota rejection as an unknown outcome.
            if record.get("status") == "uncertain" and "QuotaSharedPoolExceeded" in record.get("error", ""):
                record.update(upload_failure(WorkflowError(record["error"]), submitting=True))
                self.h.save_shot_manifest(path, record)
            if record.get("status") in {"uploading", "submitting"} and not any(
                thread.is_alive() and thread.name == f"wardrobe-video-library-{key}" for thread in threading.enumerate()
            ):
                record.update(status="uncertain", error="服务在视频入库期间中断，请先刷新角色库核对原片是否已入库；系统不会自动重复提交。")
            if record.get("asset_id"):
                raw = self.h.ark_assets_client().get_asset(record["asset_id"])
                record["asset_status"] = self.h._ark_asset_value(raw, "Status", "status")
            return jsonify(record)
        except Exception as exc:
            return jsonify(error=str(exc)), 400

    def latest_upload(self):
        directory = self.h.PROJECT_DIR / "runs" / "wardrobe_video_uploads"
        candidates = list(directory.glob("*.json")) if directory.is_dir() else []
        return self.upload_status(max(candidates, key=lambda path: path.stat().st_mtime).stem) if candidates else jsonify(request_id="")

    def existing_video(self, path, digest, assets):
        """Reuse only byte-identical Active videos, never just matching names.

        Receipts cover our uploads without reading media again. Older assets
        are checked through their fresh Ark URL, bounded to five candidates.
        No files or assets are removed and no generation is submitted here.
        """
        directory = self.h.PROJECT_DIR / "runs" / "wardrobe_video_uploads"
        for receipt in directory.glob("*.json"):
            try:
                record = json.loads(receipt.read_text(encoding="utf-8"))
                if record.get("sha256") == digest and record.get("asset_id"):
                    raw = self.asset("asset://" + record["asset_id"], "Video")
                    return {"id": record["asset_id"], "name": str(self.h._ark_asset_value(raw, "Name", "name") or record.get("name", ""))}
            except (OSError, ValueError, WorkflowError):
                continue
        size = path.stat().st_size
        for item in [asset for asset in assets if asset.get("status") == "Active"][:5]:
            try:
                raw = self.asset(item["uri"], "Video")
                url = str(self.h._ark_asset_value(raw, "URL", "Url", "url") or "")
                if not re.match(r"https?://", url):
                    continue
                with requests.get(url, stream=True, timeout=(10, 30)) as response:
                    if response.status_code != 200:
                        continue
                    length = response.headers.get("Content-Length")
                    if length and int(length) != size:
                        continue
                    actual, count = hashlib.sha256(), 0
                    for chunk in response.iter_content(1024 * 1024):
                        count += len(chunk)
                        if count > size:
                            break
                        actual.update(chunk)
                    if count == size and actual.hexdigest() == digest:
                        return {"id": item["id"], "name": item["name"]}
            except (requests.RequestException, ValueError, WorkflowError):
                continue
        return None

    def upload_video(self):
        # A durable receipt is written before CreateAsset. An ambiguous response
        # is not automatically retried, so a reconnect cannot create duplicates.
        try:
            key = request.form.get("request_id", "")
            with self.lock:
                receipt = self.receipt_path(key)
                if receipt.is_file():
                    return jsonify(json.loads(receipt.read_text(encoding="utf-8"))), 202
                gid = request.form.get("group_id", "").strip()
                name = " ".join(request.form.get("name", "").split())
                if not re.fullmatch(r"group-[A-Za-z0-9_-]{6,120}", gid) or not name or len(name) > 64:
                    raise WorkflowError("请选择素材组并填写 1–64 字的视频名称。")
                library = self.library_records()
                groups = library["groups"]
                if gid not in {item["id"] for item in groups}:
                    raise WorkflowError("找不到该素材组，请刷新后重新选择。")
                run_dir = self.h.timestamped_run_dir("wardrobe_video_asset_upload")
                path = self.h.save_upload(request.files.get("reference_video"), run_dir, "original")
                # Asset URI generation cannot locally crop a library video. Keep
                # uploads within the model's duration envelope before ingesting.
                validate_seedance_reference_video(path)
                digest = video_digest(path)
                record = {"request_id": key, "status": "uploading", "name": name, "sha256": digest}
                self.h.save_shot_manifest(receipt, record)

            def worker():
                upload_source = None
                try:
                    duplicate = self.existing_video(path, digest, library.get("assets", []))
                    if duplicate:
                        self.h.save_shot_manifest(receipt, {**record, "status": "reused", "asset_id": duplicate["id"],
                            "uri": "asset://" + duplicate["id"], "existing_name": duplicate["name"], "deduplicated": True})
                        return
                    upload_source = self.h.prepare_seedance_stable_video_reference(path, context_label="原片视频入库")
                    self.h.save_shot_manifest(receipt, {**record, "status": "submitting"})
                    aid = self.h.ark_assets_client().create_asset(group_id=gid, url=upload_source.url, name=name, asset_type="Video")
                    self.h.save_shot_manifest(receipt, {**record, "status": "submitted", "asset_id": aid,
                                                        "uri": "asset://" + aid, "name": name})
                    # Only this dedicated upload copy is removed after ingestion.
                    self.h._cleanup_ark_asset_upload(aid, upload_source, path)
                    upload_source = None
                except Exception as exc:
                    old = json.loads(receipt.read_text(encoding="utf-8"))
                    old.update(upload_failure(exc, submitting=old["status"] == "submitting"))
                    self.h.save_shot_manifest(receipt, old)
                finally:
                    if upload_source:
                        upload_source.close()
            threading.Thread(target=worker, daemon=True, name=f"wardrobe-video-library-{key}").start()
            return jsonify(request_id=key, status="uploading"), 202
        except Exception as exc:
            return jsonify(error=str(exc)), 400

    def generate(self, source_job):
        h = self.h
        data = self.data(source_job)
        workflow = data.get(self.workflow_key)
        if source_job.kind != f"wardrobe_prepare_{self.mode}" or data.get("workflow_version") != 2 or workflow not in {"references", "library"}:
            raise WorkflowError(f"这是旧版或其他流程的白模任务，请重新打码、上传{self.white_label}，或选择角色库原片视频。")
        key, receipt = h.wardrobe_dynamic_request(source_job, "generate")
        if receipt is not None:
            return jsonify(receipt.public()), 202
        if source_job.status in RUNNING:
            raise WorkflowError("当前素材准备尚未完成。")
        image_upload = request.files.get("replacement_image")
        has_object = bool(image_upload and image_upload.filename)
        if not self.scene and h.form_bool("special_object") and not has_object:
            raise WorkflowError("特殊物品需要上传物品参考图。")
        people_requested = inline_cast.submitted()
        if people_requested and workflow != 'references':
            raise WorkflowError('新增人物请切换到有人物、服装参考流程。')
        if people_requested and (not request.form.get('prompt', '').strip() or self.scene and not has_object):
            raise WorkflowError('请填写替换要求；背景替换还需上传场景参考图。')
        first_clothing = bool(request.files.get('clothing_image') and request.files['clothing_image'].filename)
        prompt = '' if people_requested or workflow == 'references' and not first_clothing else (scene_final_prompt if self.scene else object_final_prompt)(workflow, request.form.get("prompt", ""), has_object)
        resolution = request.form.get("resolution", "720p")
        if resolution not in {"480p", "720p"}:
            raise WorkflowError("成片分辨率只支持 480p 或 720p。")
        character = ""
        video_asset = ""
        if workflow == "references":
            if not source_job.white_model_path or not source_job.white_model_path.is_file():
                raise WorkflowError(f"请先生成或上传{self.white_label}视频。")
            if not h.form_bool("white_reviewed"):
                raise WorkflowError(f"请先预览{self.white_label}并确认人物、遮挡与运镜正确。")
            character = h.requested_character_asset()
            self.asset(character, "Image")
            clothing = request.files.get("clothing_image")
        else:
            video_asset = data.get("video_asset", "")
            self.asset(video_asset, "Video")
            if request.form.get("person_asset") or request.files.get("clothing_image"):
                raise WorkflowError("原片视频入库流程不接收人物或服装参考，请切换到有人物服装参考流程。")
        job = h.new_job(f"wardrobe_generate_{self.mode}")
        try:
            references = []
            job.cast_continuity["wardrobe_dynamic"] = json.loads(json.dumps(data))
            self.data(job).update(final_prompt=prompt, custom_prompt=request.form.get("prompt", ""), special_object=h.form_bool("special_object"))
            if workflow == "references":
                job.white_model_path = source_job.white_model_path
                job.mosaic_path = source_job.mosaic_path
                job.actors = [{"id": 1, "role": "原片人物", "trusted_asset_uri": character}]
                clothing = h.save_optional_reference_image(job, "clothing_image", "clothing")
                job.clothing_path = h.prepare_wardrobe_seedance_reference(job, clothing, role="clothing", label="人物1服装") if clothing else None
                references = [character] + ([str(job.clothing_path)] if job.clothing_path else [])
            if has_object:
                image = h.save_optional_reference_image(job, "replacement_image", "replacement")
                job.replacement_path = h.prepare_wardrobe_seedance_reference(job, image, role="replacement", label=self.image_label)
                references.append(str(job.replacement_path))
            people = inline_cast.collect(h, source_job, job, references, workflow == 'references')
            if people or workflow == 'references' and not first_clothing:
                if not request.form.get('prompt','').strip() or self.scene and not has_object:
                    raise WorkflowError('请填写替换要求；背景替换还需上传场景参考图。')
                prompt = inline_cast.final_prompt(self.mode, people, len(references), request.form.get('prompt', ''),
                                                  has_replacement=has_object, color_plan=source_job.cast_continuity.get('inline_color_plan'), first_clothing=first_clothing)
            self.data(job)['final_prompt'] = prompt
            if references:
                build_motion_reference_payload(prompt=prompt, image_sources=references,
                                               video_reference=video_asset or "https://validation.invalid/white.mp4", resolution=resolution)
            options = {"generation_channel": "api", "prompt": prompt, "model": h.DEFAULT_SEEDANCE_25_MODEL,
                       "resolution": resolution, "ratio": "adaptive", "duration": -1,
                       "generate_audio": h.form_bool("generate_audio", True), "watermark": False,
                       "delete_tos_after": True, "reference_upload_strategy": "stable"}
            data["requests"][key] = {"action": "generate", "job_id": job.id}
            data.update(custom_prompt=request.form.get("prompt", ""), special_object=h.form_bool("special_object"))
            h.persist_wardrobe_swap_job(job, self.mode, source_job_id=source_job.id)
            inline_cast.remember(source_job, job)
            self.save(source_job)
            job.log("@视频1 已绑定" + (f"{self.white_label}；@图片1 为原片人物。" + ("@图片2 为人物1服装。" if first_clothing else "人物1服装沿用@图片1。") if workflow == "references" else "角色库原片 Video Asset，直接引用原视频。"))
            if has_object:
                job.log(f"@图片{len(references)} 为{self.image_label}。")
            threading.Thread(target=h.run_generation, kwargs={
                "job": job, "depth_path": job.white_model_path, "depth_reference": video_asset,
                "person_source": "", "clothing_source": "", "scene_source": "", "options": options,
                "reference_images": references if references else None, "video_only": not references,
                "on_finished": lambda completed: h.persist_wardrobe_generation_result(completed, source_job, self.mode),
            }, daemon=True, name=f"wardrobe-generate-{self.namespace}-{job.id}").start()
            return jsonify(job.public()), 202
        except Exception as exc:
            h.job_error(job, exc)
            h.persist_wardrobe_swap_job(job, self.mode, source_job_id=source_job.id)
            raise
