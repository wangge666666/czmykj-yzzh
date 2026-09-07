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

from hybrid_shared import HybridError, Store, digest, file_hash, trusted_url, validate_manifest

BUSY = {"processing", "submitting", "submit_uncertain", "cloud_running", "reconciliation_required"}


class Runtime:
    def __init__(self, root, cloud_url="", login_url="", *, development=False):
        self.store = Store(root)
        self.cloud_url = trusted_url(cloud_url, local=development) if cloud_url else ""
        self.login_url = trusted_url(login_url) if login_url else ""
        self.token = ""
        self.account = None
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.lock = threading.RLock()
        # A prior process may have stopped mid-write: retain evidence, never invent success.
        for record in self.store.list():
            if record["state"] in {"processing", "submitting"}:
                record["state"] = "interrupted" if record["state"] == "processing" else "submit_uncertain"
                self.store.put(record, "PROCESS_RESTARTED")

    def cloud(self, method, path, **kwargs):
        if not self.cloud_url:
            raise HybridError("CLOUD_NOT_CONFIGURED", 503)
        if not self.token:
            raise HybridError("LOGIN_REQUIRED", 401)
        try:
            response = requests.request(method, self.cloud_url + path, headers={"Authorization": "Bearer " + self.token},
                                        timeout=(10, 240), allow_redirects=False, **kwargs)
            if response.status_code != 200:
                if response.status_code in {400, 401, 402, 403, 404, 409, 413, 415}:
                    raise HybridError("CLOUD_SUBMIT_REJECTED", response.status_code)
                raise HybridError("CLOUD_REQUEST_REJECTED", response.status_code if response.status_code < 500 else 503)
            return response
        except requests.RequestException:
            raise HybridError("CLOUD_CONNECTION_UNCERTAIN", 503) from None

    def login(self, username, password):
        with self.lock:
            return self._login(username, password)

    def _login(self, username, password):
        if not self.login_url or not self.cloud_url:
            raise HybridError("LOGIN_ENDPOINT_NOT_CONFIGURED", 503)
        if not isinstance(username, str) or not isinstance(password, str) or not 1 <= len(username) <= 50 or not 1 <= len(password) <= 128:
            raise HybridError("INVALID_LOGIN")
        # The gateway validates the signed token independently after login-center authentication.
        try:
            response = requests.post(self.login_url, json={"username": username, "password": password, "role": "customer"},
                                     timeout=(10, 30), allow_redirects=False)
            data = response.json() if response.status_code == 200 else {}
            token = data.get("token")
            if data.get("success") is not True or not isinstance(token, str) or not 1 <= len(token) <= 4096:
                raise HybridError("LOGIN_REJECTED", 401)
            self.token = token
            self.account = self.cloud("GET", "/v1/account").json()
            return self.account
        except (requests.RequestException, ValueError, HybridError):
            self.token, self.account = "", None
            raise HybridError("LOGIN_REJECTED", 401) from None

    def logout(self):
        with self.lock:
            self.token, self.account = "", None
            return {"logged_out": True}

    def health(self):
        import importlib.util
        return {"version": "0.1.0", "cloud_configured": bool(self.cloud_url), "logged_in": bool(self.account),
                "local_dependencies": {k: bool(importlib.util.find_spec(k)) for k in ("cv2", "onnxruntime", "imageio_ffmpeg")},
                "note": "配置或依赖就绪不代表真实生成与计费已验收"}

    def _record(self, key):
        record = self.store.get(key)
        if record.get("owner", 0) and (not self.account or record["owner"] != self.account["user_id"]):
            raise HybridError("LOGIN_WITH_PROJECT_OWNER", 403)
        return record

    def public(self, record):
        result = {k: copy.deepcopy(record[k]) for k in ("id", "name", "state", "artifacts", "error") if k in record}
        if "plan" in record:
            result["plan"] = {k: copy.deepcopy(record["plan"][k]) for k in ("manifest", "quote", "hash", "references") if k in record["plan"]}
        if "cloud" in record:
            result["cloud"] = record["cloud"]
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
            self.pool.submit(self._process_worker, project_id, source, operation)
        return self.public(record)

    def _process_worker(self, project_id, source, operation):
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
        with self.lock:
            record = self._record(project_id)
            if record["state"] in BUSY:
                raise HybridError("PROJECT_BUSY", 409)
            if not self.account:
                raise HybridError("LOGIN_REQUIRED", 401)
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
            quote = self.cloud("POST", "/v1/quotes", json=manifest).json()
            if "plan" in record:
                record.setdefault("history", []).append({"plan": record["plan"], "cloud": record.get("cloud")})
            record.pop("cloud", None)
            record["owner"] = self.account["user_id"]
            record["plan"] = {"manifest": manifest, "files": list(map(str, files)), "quote": quote,
                              "hash": digest({"manifest": manifest, "quote": quote}), "references": references, "approved": False}
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
            record["plan"]["approved"] = approved
            record["state"] = "approved" if approved else "rejected"
            self.store.put(record, "USER_APPROVED" if approved else "USER_REJECTED")
            return self.public(record)

    def submit(self, project_id):
        with self.lock:
            record = self._record(project_id)
            if record["state"] != "approved" or not record.get("plan", {}).get("approved"):
                raise HybridError("HUMAN_APPROVAL_REQUIRED", 409)
            plan = record["plan"]
            for path, asset in zip(plan["files"], plan["manifest"]["assets"]):
                if file_hash(path) != asset["sha256"]:
                    raise HybridError("INPUT_CHANGED_REPLAN_REQUIRED", 409)
            record["state"] = "submitting"
            self.store.put(record, "CLOUD_SUBMIT_STARTED")
            self.pool.submit(self._submit_worker, project_id)
            return self.public(record)

    def _submit_worker(self, project_id):
        with self.lock:
            self._submit_locked(project_id)

    def _submit_locked(self, project_id):
        from contextlib import ExitStack
        record = self.store.get(project_id)
        try:
            # A login can win the lock after submit queues this worker. Never send
            # another owner's multipart media with the newly selected account.
            if not self.account or record.get("owner") != self.account["user_id"]:
                raise HybridError("SUBMIT_ACCOUNT_CHANGED", 409)
            plan = record["plan"]
            with ExitStack() as stack:
                files = {asset["slot"]: ("input.mp4" if asset["kind"] == "video" else "input.img",
                         stack.enter_context(Path(path).open("rb"))) for path, asset in zip(plan["files"], plan["manifest"]["assets"])}
                result = self.cloud("POST", f"/v1/tasks/{plan['quote']['id']}/submit", files=files).json()
            record["cloud"] = result
            record["state"] = "failed" if result["state"] == "failed" else "cloud_running"
        except HybridError as error:
            rejected = error.code in {"CLOUD_SUBMIT_REJECTED", "SUBMIT_ACCOUNT_CHANGED"}
            record.update(state="needs_replan" if rejected else "submit_uncertain",
                          error="CLOUD_REJECTED_RELOGIN_OR_REPLAN" if rejected else "QUERY_ONLY_DO_NOT_RESUBMIT")
        except Exception:
            record.update(state="submit_uncertain", error="QUERY_ONLY_DO_NOT_RESUBMIT")
        self.store.put(record, "CLOUD_SUBMIT_RETURNED")

    def poll(self, project_id):
        with self.lock:
            record = self._record(project_id)
            if record["state"] not in {"cloud_running", "submit_uncertain", "reconciliation_required"}:
                raise HybridError("NO_CLOUD_TASK", 409)
            result = self.cloud("GET", f"/v1/tasks/{record['plan']['quote']['id']}").json()
            record["cloud"] = result
            record["state"] = result["state"] if result["state"] in {"succeeded", "failed", "reconciliation_required", "submit_uncertain"} else "cloud_running"
            if result["state"] == "quoted":
                record.update(state="submit_uncertain", error="SUBMISSION_NOT_OBSERVED_CONTACT_SUPPORT")
            if result["state"] == "succeeded":
                target = self.store.root / project_id / (uuid.uuid4().hex + ".mp4")
                response = self.cloud("GET", f"/v1/tasks/{record['plan']['quote']['id']}/output", stream=True)
                with response, target.open("xb") as output:
                    count = 0
                    for chunk in response.iter_content(1024 * 1024):
                        count += len(chunk)
                        if count > 1024 ** 3:
                            raise HybridError("OUTPUT_TOO_LARGE")
                        output.write(chunk)
                from workflow_core import inspect_video
                inspect_video(target)
                self._add_artifact(record, target, "output")
            self.store.put(record, "CLOUD_STATUS_RECEIVED")
            return self.public(record)

    def export(self, project_id, artifact_id, destination):
        source = self.artifact_path(project_id, artifact_id)
        target = Path(destination).expanduser().resolve()
        if not target.parent.is_dir():
            raise HybridError("EXPORT_DIRECTORY_MISSING")
        with target.open("xb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
        return {"path": str(target), "sha256": file_hash(target)}
