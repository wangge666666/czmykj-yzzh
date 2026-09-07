from __future__ import annotations

import copy
import json
import os
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from hybrid_shared import HybridError, Store, digest, file_hash, validate_manifest
from .account import AccountClient, DEFAULT_LOGIN_URL
from .settings import Settings
from .provider import ArkProvider

BUSY = {"processing", "submitting", "submit_uncertain", "provider_running", "download_pending", "cloud_running", "reconciliation_required"}


class Runtime:
    def __init__(self, root, login_url=DEFAULT_LOGIN_URL, *, development=False, account_client=None, provider_factory=ArkProvider,
                 mode="byok", platform_url=None, platform_client=None):
        self.store = Store(root)
        self.auth = account_client or AccountClient(login_url, development=development)
        self.login_url = self.auth.login_url
        self.settings = Settings(self.store.root)
        if mode not in {"byok", "platform"}:
            raise HybridError("INVALID_SERVICE_MODE")
        self.mode = mode
        self.platform = platform_client
        if mode == "platform" and self.platform is None:
            from .platform_service import PlatformService, DEFAULT_PLATFORM_URL
            self.platform = PlatformService(platform_url or DEFAULT_PLATFORM_URL, development=development)
        self.provider_factory = provider_factory
        self.token = ""
        self.account = None
        self.session_revision = uuid.uuid4().hex
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.lock = threading.RLock()
        # A prior process may have stopped mid-write: retain evidence, never invent success.
        for record in self.store.list():
            if record["state"] in {"processing", "submitting"}:
                record["state"] = "interrupted" if record["state"] == "processing" else "submit_uncertain"
                self.store.put(record, "PROCESS_RESTARTED")

    def _verify(self, require_license=True):
        if not self.token:
            raise HybridError("LOGIN_REQUIRED", 401)
        try:
            account = self.auth.verify(self.token)
        except HybridError as error:
            if self.account:
                self.account.update(licensed=False, license_status="unavailable" if error.status == 503 else "login_expired")
            raise
        if self.account and self.account["user_id"] != account["user_id"]:
            self.token, self.account = "", None
            raise HybridError("ACCOUNT_CHANGED", 401)
        self.account = account
        if require_license and not account["licensed"]:
            raise HybridError("PRODUCT_4_LICENSE_REQUIRED", 403)
        return account

    def provider_settings(self):
        if not self.account:
            return None
        if self.mode == "platform":
            result = self.platform.capabilities(self.token, self.account["user_id"], self.session_revision)
        else:
            result = self.settings.public(self.account["user_id"])
        result.update(owner_id=self.account["user_id"], session_revision=self.session_revision)
        return result

    def save_settings(self, changes, expected_owner, expected_session, clear=False):
        with self.lock:
            self._verify(require_license=False)
            if type(expected_owner) is not int or expected_owner != self.account["user_id"] or expected_session != self.session_revision:
                raise HybridError("SETTINGS_ACCOUNT_CHANGED_REFRESH", 409)
            if self.mode == "platform":
                raise HybridError("PLATFORM_SETTINGS_MANAGED_BY_ADMIN", 409)
            return self.settings.clear(self.account["user_id"]) if clear else self.settings.save(self.account["user_id"], changes)

    def login(self, username, password, role="customer"):
        with self.lock:
            return self._login(username, password, role)

    def _login(self, username, password, role="customer"):
        from .account import login_role
        role = login_role(role)
        if not isinstance(username, str) or not isinstance(password, str) or not 1 <= len(username) <= 50 or not 1 <= len(password) <= 128:
            raise HybridError("INVALID_LOGIN")
        self.token, self.account = "", None
        self.session_revision = uuid.uuid4().hex
        try:
            self.token = self.auth.login(username, password, role=role)
            return self._verify(require_license=False)
        except Exception:
            self.token, self.account = "", None
            raise

    def logout(self):
        with self.lock:
            self.token, self.account = "", None
            self.session_revision = uuid.uuid4().hex
            return {"logged_out": True}

    def login_token(self, token):
        """Accept an existing central session; never trust browser account fields."""
        if not isinstance(token, str) or not 1 <= len(token) <= 8192:
            raise HybridError("INVALID_LOGIN")
        with self.lock:
            account = self.auth.verify(token)
            self.token, self.account = token, account
            self.session_revision = uuid.uuid4().hex
            return account

    def health(self):
        import importlib.util
        return {"version": "0.5.0", "mode": self.mode, "interface": "original-workflows", "login_url": self.login_url, "logged_in": bool(self.account),
                "projects": ["wardrobe", "virtual", "real"],
                "local_dependencies": {k: bool(importlib.util.find_spec(k)) for k in ("cv2", "onnxruntime", "imageio_ffmpeg", "tos")},
                "note": ("三个项目共用米哟平台服务，管理员配置通道与计费；服务部署及真实生成须分别验收。" if self.mode == "platform"
                         else "历史本机 Key 模式：费用由用户的供应商账户承担。配置不代表真实联调通过。")}

    def _record(self, key):
        record = self.store.get(key)
        if record.get("owner", 0) and (not self.account or record["owner"] != self.account["user_id"]):
            raise HybridError("LOGIN_WITH_PROJECT_OWNER", 403)
        return record

    def public(self, record):
        result = {k: copy.deepcopy(record[k]) for k in ("id", "name", "state", "artifacts", "error") if k in record}
        if "plan" in record:
            result["plan"] = {k: copy.deepcopy(record["plan"][k]) for k in ("manifest", "cost", "hash", "references", "destination") if k in record["plan"]}
        if "provider" in record:
            result["provider"] = {k: record["provider"][k] for k in ("task_id", "status") if k in record["provider"]}
        return result

    def list_projects(self):
        owner = self.account["user_id"] if self.account else 0
        return [self.public(x) for x in self.store.list() if x.get("owner", 0) in {0, owner}]

    def status(self, project_id):
        return self.public(self._record(project_id))

    def _add_artifact(self, record, path, kind):
        path = Path(path).resolve()
        relative = str(path.relative_to(self.store.root / record["id"]))
        key = uuid.uuid4().hex
        record["artifacts"][key] = {"kind": kind, "file": relative, "sha256": file_hash(path), "size": path.stat().st_size}
        return key

    def artifact_path(self, project_id, artifact_id):
        record = self._record(project_id)
        artifact = record["artifacts"].get(artifact_id)
        if not artifact:
            raise HybridError("ARTIFACT_NOT_FOUND", 404)
        root = self.store.root / project_id
        path = (root / artifact["file"]).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise HybridError("INVALID_ARTIFACT") from None
        if not path.is_file():
            raise HybridError("ARTIFACT_MISSING", 404)
        return path

    def import_video(self, path):
        with self.lock:
            return self._import_video(path)

    def _import_video(self, path):
        self._verify()
        from workflow_core import inspect_video
        source = Path(path).expanduser().resolve()
        if not source.is_file() or source.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"}:
            raise HybridError("VIDEO_FILE_REQUIRED")
        if source.stat().st_size > 2 * 1024 ** 3:
            raise HybridError("VIDEO_TOO_LARGE")
        inspect_video(source)
        key = uuid.uuid4().hex
        root = self.store.root / key
        root.mkdir(mode=0o700)
        target = root / ("source" + source.suffix.lower())
        shutil.copyfile(source, target)
        record = {"id": key, "owner": self.account["user_id"] if self.account else 0, "name": source.name,
                  "state": "ready", "artifacts": {}, "error": ""}
        self._add_artifact(record, target, "source")
        self.store.put(record, "VIDEO_IMPORTED")
        return self.public(record)

    def process(self, project_id, artifact_id, operation):
        if operation not in {"split", "mosaic", "depth"}:
            raise HybridError("INVALID_OPERATION")
        with self.lock:
            record = self._record(project_id)
            if record["state"] in BUSY:
                raise HybridError("PROJECT_BUSY", 409)
            self._verify()
            source = self.artifact_path(project_id, artifact_id)
            # Do not silently download models while an agent processes private media.
            if operation == "mosaic":
                from face_mosaic import FACE_MODEL_PATH, _valid_face_model
                if not _valid_face_model(FACE_MODEL_PATH):
                    raise HybridError("FACE_MODEL_INSTALL_REQUIRED", 503)
            if operation == "depth":
                from workflow_core import DEPTH_MODEL_PATH, _valid_depth_model, inspect_video
                if inspect_video(source).duration > 14.5:
                    raise HybridError("SPLIT_BEFORE_DEPTH")
                if not _valid_depth_model(DEPTH_MODEL_PATH):
                    raise HybridError("DEPTH_MODEL_INSTALL_REQUIRED", 503)
            record.update(state="processing", error="")
            record.pop("plan", None)
            self.store.put(record, "LOCAL_PROCESS_STARTED")
            self.pool.submit(self._process_worker, project_id, source, operation, self.session_revision)
        return self.public(record)

    def _process_worker(self, project_id, source, operation, session_revision):
        try:
            with self.lock:
                record = self._record(project_id)
                if self.session_revision != session_revision:
                    raise HybridError("ACCOUNT_CHANGED", 409)
                self._verify()
        except HybridError:
            with self.lock:
                record = self.store.get(project_id)
                record.update(state="interrupted", error="PROCESS_AUTHORIZATION_CHANGED")
                self.store.put(record, "LOCAL_PROCESS_BLOCKED")
            return
        root = self.store.root / project_id / uuid.uuid4().hex
        root.mkdir()
        try:
            if operation == "split":
                from long_video_core import detect_video_shots, split_video_shots
                outputs = split_video_shots(source, detect_video_shots(source), root)
            elif operation == "mosaic":
                from face_mosaic import render_face_mosaic_video
                output = root / "mosaic.mp4"
                render_face_mosaic_video(source, output)
                outputs = [output]
            else:
                from workflow_core import run_depth_generation
                outputs = [run_depth_generation(source, root / "depth.mp4")]
            with self.lock:
                record = self.store.get(project_id)
                for output in outputs:
                    self._add_artifact(record, output, operation)
                record.update(state="ready", error="")
                self.store.put(record, "LOCAL_PROCESS_FINISHED")
        except Exception:
            with self.lock:
                record = self.store.get(project_id)
                record.update(state="failed", error="LOCAL_PROCESS_FAILED")
                self.store.put(record, "LOCAL_PROCESS_FAILED")

    def plan(self, project_id, artifact_id, image_paths, prompt, model, resolution="720p", ratio="adaptive", duration=5):
        if self.mode == "platform":
            raise HybridError("PLATFORM_USE_PROJECT_WORKFLOWS", 409)
        with self.lock:
            record = self._record(project_id)
            if record["state"] in BUSY:
                raise HybridError("PROJECT_BUSY", 409)
            if not self.account:
                raise HybridError("LOGIN_REQUIRED", 401)
            self._verify()
            values, config_revision = self.settings.load(self.account["user_id"])
            model = model or values["ARK_MODEL"]
            if not isinstance(image_paths, list) or not 1 <= len(image_paths) <= 3:
                raise HybridError("ONE_TO_THREE_IMAGES_REQUIRED")
            from workflow_core import inspect_video
            video = self.artifact_path(project_id, artifact_id)
            if inspect_video(video).duration > 14.5:
                raise HybridError("SPLIT_REFERENCE_FIRST")
            files = [video]
            references = [{"artifact_id": artifact_id, "name": record["artifacts"][artifact_id]["file"], "kind": "video"}]
            snapshot_dir = self.store.root / project_id / uuid.uuid4().hex
            snapshot_dir.mkdir()
            for raw in image_paths:
                path = Path(raw).expanduser().resolve()
                if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                    raise HybridError("INVALID_REFERENCE_IMAGE")
                if path.stat().st_size > 10 * 1024 * 1024:
                    raise HybridError("REFERENCE_IMAGE_TOO_LARGE")
                import cv2
                if cv2.imread(str(path)) is None:
                    raise HybridError("INVALID_REFERENCE_IMAGE")
                snapshot = snapshot_dir / (uuid.uuid4().hex + path.suffix.lower())
                shutil.copyfile(path, snapshot)
                files.append(snapshot)
                reference_id = self._add_artifact(record, snapshot, "image")
                references.append({"artifact_id": reference_id, "name": path.name, "kind": "image"})
            assets = [{"slot": f"asset_{i}", "kind": "video" if i == 0 else "image",
                       "sha256": file_hash(path), "size": path.stat().st_size} for i, path in enumerate(files)]
            manifest = validate_manifest(dict(prompt=prompt, model=model, resolution=resolution, ratio=ratio, duration=duration, assets=assets))
            self.provider_factory(values).preflight(manifest, {a["slot"]: p for a, p in zip(assets, files)})
            from yzzh_local.media import upload_destination
            cost = {"payer": "customer_provider_account", "estimate": None,
                    "notice": "供应商按实际用量收费，尚无可靠费用估算；自选存储可能另有存储/流量费用，不扣 CZMIYOU 余额。"}
            destination = {"provider": "火山方舟（北京）", **upload_destination(values)}
            if "plan" in record:
                record.setdefault("history", []).append({"plan": record["plan"], "provider": record.get("provider")})
            record.pop("cloud", None)
            record.pop("provider", None)
            record["owner"] = self.account["user_id"]
            record["plan"] = {"manifest": manifest, "files": list(map(str, files)), "cost": cost, "destination": destination,
                              "config_revision": config_revision, "session_revision": self.session_revision,
                              "hash": digest({"manifest": manifest, "cost": cost, "destination": destination,
                                              "config_revision": config_revision, "session_revision": self.session_revision}),
                              "references": references, "approved": False}
            record["error"] = ""
            record["state"] = "awaiting_approval"
            self.store.put(record, "PLAN_CREATED")
            return self.public(record)

    def approve(self, project_id, plan_hash, approved):
        with self.lock:
            record = self._record(project_id)
            if record["state"] != "awaiting_approval" or record.get("plan", {}).get("hash") != plan_hash:
                raise HybridError("STALE_APPROVAL", 409)
            if type(approved) is not bool:
                raise HybridError("BOOLEAN_APPROVAL_REQUIRED")
            if approved:
                self._check_plan(record)
            record["plan"]["approved"] = approved
            record["state"] = "approved" if approved else "rejected"
            self.store.put(record, "USER_APPROVED" if approved else "USER_REJECTED")
            return self.public(record)

    def submit(self, project_id):
        if self.mode == "platform":
            raise HybridError("PLATFORM_USE_PROJECT_WORKFLOWS", 409)
        with self.lock:
            record = self._record(project_id)
            if record["state"] != "approved" or not record.get("plan", {}).get("approved"):
                raise HybridError("HUMAN_APPROVAL_REQUIRED", 409)
            self._check_plan(record)
            record["state"] = "submitting"
            record["provider"] = {"post_started": False, "uploads": []}
            self.store.put(record, "BYOK_SUBMIT_QUEUED")
            self.pool.submit(self._submit_worker, project_id)
            return self.public(record)

    def _submit_worker(self, project_id):
        with self.lock:
            self._submit_locked(project_id)

    def _check_plan(self, record):
        if self.mode == "platform":
            raise HybridError("PLATFORM_USE_PROJECT_WORKFLOWS", 409)
        if not self.account or record["owner"] != self.account["user_id"]:
            raise HybridError("SUBMIT_ACCOUNT_CHANGED", 409)
        self._verify()
        values, revision = self.settings.load(record["owner"])
        plan = record["plan"]
        if revision != plan.get("config_revision") or self.session_revision != plan.get("session_revision"):
            raise HybridError("SETTINGS_CHANGED_REPLAN_REQUIRED", 409)
        for path, asset in zip(plan["files"], plan["manifest"]["assets"]):
            if not Path(path).is_file() or file_hash(path) != asset["sha256"]:
                raise HybridError("INPUT_CHANGED_REPLAN_REQUIRED", 409)
        return values

    def _submit_locked(self, project_id):
        record = self.store.get(project_id)
        try:
            values = self._check_plan(record)
            plan = record["plan"]
            def event(code, data):
                if code == "PROVIDER_POST_STARTED":
                    # Upload can take minutes: verify license and unchanged inputs
                    # again immediately before the paid request.
                    self._check_plan(record)
                    record["provider"]["post_started"] = True
                elif code == "UPLOAD_PLANNED":
                    record["provider"]["uploads"].append(data)
                self.store.put(record, code)
            files = {a["slot"]: p for a, p in zip(plan["manifest"]["assets"], plan["files"])}
            task_id = self.provider_factory(values).submit(plan["manifest"], files, event)
            record["provider"].update(task_id=task_id, status="queued")
            record.update(state="provider_running", error="")
        except Exception:
            uncertain = record.get("provider", {}).get("post_started", False)
            record.update(state="submit_uncertain" if uncertain else "needs_replan",
                          error="QUERY_ONLY_DO_NOT_RESUBMIT" if uncertain else "PRE_SUBMIT_FAILED_CHECK_AUTH_SETTINGS_INPUTS")
        self.store.put(record, "BYOK_SUBMIT_RETURNED")

    def poll(self, project_id):
        if self.mode == "platform":
            raise HybridError("PLATFORM_USE_PROJECT_WORKFLOWS", 409)
        with self.lock:
            record = self._record(project_id)
            if record["state"] == "succeeded":
                return self.public(record)
            if "provider" not in record:
                raise HybridError("LEGACY_TASK_REQUIRES_OLD_GATEWAY", 409)
            if record["state"] not in {"provider_running", "submit_uncertain", "download_pending"}:
                raise HybridError("NO_PROVIDER_TASK", 409)
            task_id = record["provider"].get("task_id")
            if not task_id:
                raise HybridError("SUBMIT_UNCERTAIN_CHECK_PROVIDER_CONSOLE", 409)
            # Local authenticated owner can recover accepted work after license
            # expiry/outage. Recovery never invokes a creation endpoint.
            values, _ = self.settings.load(record["owner"])
            provider = self.provider_factory(values)
            result = provider.query(task_id)
            status = result.get("status")
            if status not in {"queued", "running", "succeeded", "failed", "cancelled", "expired"}:
                raise HybridError("PROVIDER_STATUS_UNKNOWN", 503)
            record["provider"]["status"] = status
            record.update(state="download_pending" if status == "succeeded" else "failed" if status in {"failed", "cancelled", "expired"} else "provider_running", error="")
            self.store.put(record, "PROVIDER_STATUS_RECEIVED")
            if status == "succeeded":
                target = self.store.root / project_id / (uuid.uuid4().hex + ".mp4")
                try:
                    provider.download(result, target)
                    from workflow_core import inspect_video
                    inspect_video(target)
                    self._add_artifact(record, target, "output")
                    record.update(state="succeeded", error="")
                except Exception:
                    record.update(state="download_pending", error="OUTPUT_RETRIEVAL_FAILED_QUERY_ONLY")
            self.store.put(record, "BYOK_OUTPUT_CHECKED")
            return self.public(record)

    def export(self, project_id, artifact_id, destination):
        source = self.artifact_path(project_id, artifact_id)
        target = Path(destination).expanduser().resolve()
        if not target.parent.is_dir():
            raise HybridError("EXPORT_DIRECTORY_MISSING")
        with target.open("xb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
        return {"path": str(target), "sha256": file_hash(target)}
