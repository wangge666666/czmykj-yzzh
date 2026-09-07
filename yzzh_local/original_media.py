"""Bind the original image/avatar and video flows to one selected upload mode."""
from yzzh_local.media import TemporaryMediaStore, upload_mode
from hybrid_shared import HybridError


UPLOAD_ERRORS = {
    "TEMP_UPLOAD_UNAVAILABLE": "素材上传服务暂时不可用，本步尚未提交模型生成。请稍后再试。",
    "TEMP_UPLOAD_INVALID_RESPONSE": "上传服务没有返回有效素材地址，本步已停止，不会继续提交模型。",
    "TEMP_LINK_NOT_READABLE": "素材地址读取检查未通过，本步已停止，不会继续提交模型。",
    "UPLOAD_FILE_UNSUPPORTED": "请选择 JPG、PNG、WEBP 图片或 MP4、MOV 视频文件。",
    "UPLOAD_FILE_TOO_LARGE": "素材为空或超过上传限制：图片 30 MB，参考视频 200 MB。",
}


def install_media(web, core, values):
    mode = upload_mode(values)
    original_tos = core.TosMediaStore

    class SelectedStore:
        def __init__(self, **_options):
            self.store = TemporaryMediaStore() if mode == "temporary" else original_tos()

        @staticmethod
        def available():
            return mode == "temporary" or original_tos.configured()

        def upload_file(self, path, **_options):
            return self._upload(path, video=False)

        def upload_video(self, path, **_options):
            return self._upload(path, video=True)

        def _upload(self, path, *, video):
            try:
                return self.store.upload_video(path) if video else self.store.upload_file(path)
            except HybridError as error:
                raise core.WorkflowError(UPLOAD_ERRORS.get(error.code, "素材上传未完成，请检查文件后重试。")) from None

        def delete(self, key):
            return self.store.delete(key)

    class NoTosFallback:
        @staticmethod
        def configured():
            return False

        def __init__(self):
            raise core.WorkflowError("当前使用自动临时上传；不会擅自切换到其他存储。")

    if mode == "temporary":
        web.TosMediaStore = core.TosMediaStore = NoTosFallback
    web.TempFileMediaStore = core.TempFileMediaStore = SelectedStore

    def prepare(source, *, video=False, on_log=None):
        store = SelectedStore()
        if on_log:
            on_log("正在准备素材上传，请在右下角确认此次上传。")
        result = store.upload_video(source) if video else store.upload_file(source)
        if on_log:
            on_log("素材上传完成并已检查读取地址。" + ("临时文件由 Litterbox 托管，服务声明约 3 天过期。" if mode == "temporary" else "使用自己的北京 TOS。"))
        cls = web.SeedanceVideoReferenceSource if video else web.ArkCharacterUploadSource
        return cls(url=result.signed_url, channel=mode, temporary_store=store, temporary_file_id=result.object_key)

    web.prepare_seedance_stable_video_reference = lambda source, **kw: prepare(source, video=True, **kw)
    web.prepare_ark_character_upload_source = lambda source, **kw: prepare(source, **kw)
    return mode


def library_status(data, mode):
    """Keep library state truthful without replacing the original page assets."""
    if not data.get("configured"):
        data["message"] = "新增或同步火山人物库，请在右下角「账号与创作设置 → 火山素材库连接」填写有相应权限的 AK/SK；无需配置存储桶。"
        return data
    data["storage_mode"] = mode
    parts = [part for part in str(data.get("message", "")).split("；") if "TOS 尚未开通" not in part]
    parts.append("人物图片由插件自动上传到 Litterbox，持链接可访问，申请保存 72 小时，不能提前删除；上传前需确认。" if mode == "temporary" else "人物图片使用自己的北京 TOS；云端清理仍需逐次确认，拒绝后对象保留。")
    data["message"] = "；".join(filter(None, parts))
    return data


def upload_log(message, mode):
    """Correct legacy retention/deletion language without changing workflow UI."""
    text = str(message)
    if mode == "temporary":
        if text in {"免费临时深度视频已立即删除。", "免费临时深度视频已删除。"}:
            return "临时素材使用已结束；Litterbox 源文件由服务按期限清理，插件未执行提前删除。"
        text = text.replace("1 小时自动过期", "约 3 天过期（服务声明）")
        text = text.replace("1 小时后自动过期", "约 3 天后过期（服务声明）")
        text = text.replace("将在任务结束后删除", "由 Litterbox 按期限清理，插件不能提前删除")
        text = text.replace("审核结束后自动删除", "由 Litterbox 按期限清理，插件不能提前删除")
    elif "临时" in text and ("上传" in text or "地址" in text):
        text = "素材传输使用自己的北京 TOS，正在处理当前步骤。"
    return text
