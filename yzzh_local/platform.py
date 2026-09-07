"""Credential-free adapters for the original workflows' platform channel.

The parent process owns HTTPS, account authentication, request IDs, project/stage
binding, and approval. It supplies ``PlatformTransport(rpc, upload)`` here:

* ``rpc(operation, payload)`` returns the unwrapped native Ark JSON result.
* ``upload(Path)`` returns the unwrapped ``{object_key, signed_url}`` result.

Neither callback is retried by this module. Provider and ambiguous-connection
errors should retain their WorkflowError/ArkAPIError/ArkConnectionError types.
Workers receive no platform token, provider key, HTTP session, or HTTP endpoint.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from performance_analysis import ArkPerformanceAnalyzer, DEFAULT_PERFORMANCE_MODEL
from workflow_core import ArkAssetsClient, ArkVideoClient, UploadedObject, WorkflowError


ASSET_ACTIONS = frozenset({
    "ListAssetGroups", "GetAssetGroup", "CreateAssetGroup", "UpdateAssetGroup", "DeleteAssetGroup",
    "ListAssets", "GetAsset", "CreateAsset", "DeleteAsset",
    "CreateVisualValidateSession", "GetVisualValidateResult",
})
OPERATIONS = frozenset({"video.create", "video.get", "video.list", "image.generate", "analysis.create"}) | {
    "assets." + action for action in ASSET_ACTIONS
}


class PlatformTransport:
    def __init__(self, rpc: Callable[[str, dict[str, Any]], dict[str, Any]],
                 upload: Callable[[Path], dict[str, Any]]) -> None:
        self._rpc = rpc
        self._upload = upload

    def rpc(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        if operation not in OPERATIONS or not isinstance(payload, dict):
            raise WorkflowError("此平台操作尚未支持，未发送请求。")
        result = self._rpc(operation, payload)
        if not isinstance(result, dict):
            raise WorkflowError("平台返回的操作结果格式异常。")
        return result

    def upload(self, path: str | Path) -> UploadedObject:
        source = Path(path).expanduser().resolve()
        if not source.is_file() or source.stat().st_size <= 0:
            raise WorkflowError("找不到待上传素材，或素材文件为空。")
        result = self._upload(source)
        if not isinstance(result, dict):
            raise WorkflowError("平台未返回有效素材地址，本步已停止。")
        object_key = result.get("object_key")
        signed_url = result.get("signed_url")
        try:
            parsed = urlsplit(signed_url) if isinstance(signed_url, str) else None
            valid_url = parsed and parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
        except ValueError:
            valid_url = False
        if not isinstance(object_key, str) or not object_key.strip() or not valid_url:
            raise WorkflowError("平台未返回有效素材地址，本步已停止。")
        return UploadedObject(object_key=object_key, signed_url=signed_url)


class PlatformVideoClient(ArkVideoClient):
    def __init__(self, transport: PlatformTransport) -> None:
        # Do not invoke the BYOK constructor or create a requests.Session.
        self.transport = transport

    def _request(self, method: str, path: str, *, json_body=None, params=None, timeout=None):
        method = method.upper()
        route = "/" + path.lstrip("/")
        if route == "/contents/generations/tasks":
            if method == "POST":
                return self.transport.rpc("video.create", json_body or {})
            if method == "GET":
                return self.transport.rpc("video.list", params or {})
        prefix = "/contents/generations/tasks/"
        if method == "GET" and route.startswith(prefix):
            task_id = route[len(prefix):]
            if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
                return self.transport.rpc("video.get", {"task_id": task_id})
        if method == "POST" and route == "/images/generations":
            return self.transport.rpc("image.generate", json_body or {})
        raise WorkflowError("此平台模型操作尚未支持，未发送请求。")

    def check_credentials(self) -> str:
        self.list_tasks(page_size=1)
        return "平台连接正常，任务查询权限可用。"


class PlatformAssetsClient(ArkAssetsClient):
    def __init__(self, transport: PlatformTransport, *, project_name: str = "default") -> None:
        self.transport = transport
        self.project_name = str(project_name or "default").strip() or "default"

    def call(self, action: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if action not in ASSET_ACTIONS:
            raise WorkflowError("此平台素材库操作尚未支持，未发送请求。")
        return self.transport.rpc("assets." + action, body or {})


class _AnalysisResponse:
    status_code = 200

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result

    def json(self) -> dict[str, Any]:
        return self.result


class _AnalysisSession:
    """Method compatibility only: there is no network session behind this object."""
    def __init__(self, transport: PlatformTransport) -> None:
        self.transport = transport

    def post(self, url, *, headers, json, timeout):
        if url != "platform:analysis/responses":
            raise WorkflowError("此平台分析操作尚未支持，未发送请求。")
        # Original analyzer builds its complete prompts and normalization. Its
        # empty authorization header stays local and is never passed to RPC.
        return _AnalysisResponse(self.transport.rpc("analysis.create", json))


class PlatformPerformanceAnalyzer(ArkPerformanceAnalyzer):
    def __init__(self, transport: PlatformTransport, *, model: str = DEFAULT_PERFORMANCE_MODEL) -> None:
        self.api_key = ""
        self.base_url = "platform:analysis"
        self.model = str(model or DEFAULT_PERFORMANCE_MODEL).strip() or DEFAULT_PERFORMANCE_MODEL
        self.session = _AnalysisSession(transport)


def install_platform(web, core, transport: PlatformTransport, publiccapabilities: dict[str, Any]) -> str:
    """Bind all three workflows to the same platform factories, without BYOK fallback.

    Capabilities must contain ``ready: true`` and strictly boolean feature flags
    under ``capabilities`` (video/image/analysis/assets/media). ``models.analysis``
    selects the analysis model. The server owns the asset project/account scope.
    The transport, rather than callers of these factories, binds each request to
    the current wardrobe/virtual/real operation and its stage.
    """
    capabilities = dict(publiccapabilities)
    models = capabilities.get("models") if isinstance(capabilities.get("models"), dict) else {}
    features = capabilities.get("capabilities") if isinstance(capabilities.get("capabilities"), dict) else {}
    project_name = "default"
    analysis_model = str(models.get("analysis") or DEFAULT_PERFORMANCE_MODEL)
    platform_ready = capabilities.get("ready") is True
    assets_ready = platform_ready and features.get("assets") is True

    def require_ready(service=None):
        if not platform_ready or (service and features.get(service) is not True):
            raise WorkflowError("平台服务尚未配置就绪，请联系管理员；无需填写个人 API Key。")

    class ReadyTransport:
        def rpc(self, operation, payload):
            require_ready(operation.partition(".")[0])
            return transport.rpc(operation, payload)

        def upload(self, path):
            require_ready("media")
            return transport.upload(path)

    selected_transport = ReadyTransport()

    def video_client():
        require_ready()
        return PlatformVideoClient(selected_transport)

    def assets_client():
        require_ready("assets")
        return PlatformAssetsClient(selected_transport, project_name=project_name)

    def analyzer():
        require_ready("analysis")
        return PlatformPerformanceAnalyzer(selected_transport, model=analysis_model)

    class PlatformMediaStore:
        def __init__(self, **_options):
            require_ready("media")

        @staticmethod
        def available():
            return platform_ready and features.get("media") is True

        def upload_file(self, path, **_options):
            return selected_transport.upload(path)

        def upload_video(self, path, **_options):
            return selected_transport.upload(path)

        def delete(self, _object_key):
            # The platform owns retention. Workflow cleanup must never become
            # an implicit cloud deletion or a second supplier request.
            return None

    class NoByokStorage:
        @staticmethod
        def configured():
            return False

        def __init__(self, **_options):
            raise WorkflowError("当前使用平台素材服务，不会切换到个人存储或临时公网通道。")

    def prepare(source, *, video=False, on_log=None):
        store = PlatformMediaStore()
        if on_log:
            on_log("正在通过平台准备本次素材。")
        uploaded = store.upload_video(source) if video else store.upload_file(source)
        if on_log:
            on_log("平台已返回素材读取地址。素材保存期限由平台管理。")
        cls = web.SeedanceVideoReferenceSource if video else web.ArkCharacterUploadSource
        return cls(url=uploaded.signed_url, channel="platform", object_key=uploaded.object_key)

    web.api_client = video_client
    web.ark_assets_credentials = lambda: ("", "")
    web.ark_assets_configured = lambda: assets_ready
    web.ark_assets_project_name = lambda: project_name
    web.ark_assets_client = assets_client
    web.performance_analyzer = analyzer
    web.TempFileMediaStore = core.TempFileMediaStore = PlatformMediaStore
    web.TosMediaStore = core.TosMediaStore = NoByokStorage
    web.prepare_seedance_stable_video_reference = lambda source, **kw: prepare(source, video=True, **kw)
    web.prepare_ark_character_upload_source = lambda source, **kw: prepare(source, **kw)
    return "platform"
