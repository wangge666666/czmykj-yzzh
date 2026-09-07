"""Explicit, bounded media transport; no provider keys go to temporary hosting.

Protocol: https://litterbox.catbox.moe/tools.php (fileToUpload, time=72h).
The original worker approves the frozen upload before sending. The Agent path
approves the same destination as part of its immutable generation plan.
"""
import mimetypes
import re
import uuid
from http.cookiejar import DefaultCookiePolicy
from pathlib import Path
from urllib.parse import urlsplit

import requests

from hybrid_shared import HybridError

TEMP_UPLOAD_URL = "https://litterbox.catbox.moe/resources/internals/api.php"
TEMP_DESTINATION = "Litterbox 临时素材托管"
TEMP_NOTICE = "所选素材将上传至第三方 Litterbox，并产生持链接可访问的地址；请求保存 72 小时，不支持本插件提前删除。请勿上传保密或未获授权的素材。"
TOS_FIELDS = ("TOS_ACCESS_KEY", "TOS_SECRET_KEY", "TOS_BUCKET")


def upload_mode(values):
    # Keep existing customers' explicit storage credentials on their old path.
    mode = values.get("MEDIA_UPLOAD_MODE") or ("tos" if all(values.get(k) for k in TOS_FIELDS) else "temporary")
    if mode not in {"temporary", "tos"}:
        raise HybridError("INVALID_UPLOAD_MODE")
    return mode


def upload_destination(values):
    if upload_mode(values) == "tos":
        return {"storage": "自己的北京 TOS：" + values.get("TOS_BUCKET", ""),
                "retention": "读取链接 24 小时；辅助任务对象需自行设置生命周期。"}
    return {"storage": TEMP_DESTINATION, "retention": TEMP_NOTICE}


def validate_temporary_url(url):
    if not isinstance(url, str) or len(url) > 512:
        raise HybridError("TEMP_UPLOAD_INVALID_RESPONSE", 503)
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != "litter.catbox.moe" or parts.query or parts.fragment
            or not re.fullmatch(r"/[A-Za-z0-9_-]{1,100}\.(?:png|jpg|jpeg|webp|mp4|mov)", parts.path)):
        raise HybridError("TEMP_UPLOAD_INVALID_RESPONSE", 503)
    return url


def upload_response_url(response):
    if response.status_code != 200:
        raise HybridError("TEMP_UPLOAD_UNAVAILABLE", 503)
    body = bytearray()
    for chunk in response.iter_content(1024):
        body.extend(chunk)
        if len(body) > 4096:
            raise HybridError("TEMP_UPLOAD_INVALID_RESPONSE", 503)
    response._content = bytes(body)  # Permit the guarded caller to parse it too.
    try:
        return validate_temporary_url(body.decode("utf-8").strip())
    except (UnicodeError, ValueError):
        raise HybridError("TEMP_UPLOAD_INVALID_RESPONSE", 503) from None


class _NoHostingCookies(DefaultCookiePolicy):
    """Temporary file upload and verification never require a cookie session."""
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


class TemporaryMediaStore:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.trust_env = False
        # The upload host can set a parent-domain cookie that requests would
        # otherwise replay to the file host during verify(). Keep this transport
        # anonymous; explicit credential headers still fail the network guard.
        self.session.cookies.clear()
        self.session.cookies.set_policy(_NoHostingCookies())

    @staticmethod
    def available():
        return True  # Adapter availability, not a claim about service uptime.

    def upload_file(self, path, *, maximum_bytes=30 * 1024 * 1024, **_options):
        source = Path(path)
        if source.is_symlink() or not source.is_file() or source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov"}:
            raise HybridError("UPLOAD_FILE_UNSUPPORTED")
        size = source.stat().st_size
        if not 0 < size <= maximum_bytes:
            raise HybridError("UPLOAD_FILE_TOO_LARGE")
        # Do not expose client filenames, usernames or local paths to the host.
        name = "material-" + uuid.uuid4().hex + source.suffix.lower()
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        try:
            with source.open("rb") as stream:
                expected = stream.read(64)
                stream.seek(0)
                with self.session.post(TEMP_UPLOAD_URL, files={"fileToUpload": (name, stream, mime)},
                                       data={"reqtype": "fileupload", "time": "72h"},
                                       timeout=(15, 180), stream=True, allow_redirects=False) as response:
                    url = upload_response_url(response)
            self.verify(url, size, expected)
        except requests.RequestException:
            raise HybridError("TEMP_UPLOAD_UNAVAILABLE", 503) from None
        from workflow_core import UploadedObject
        return UploadedObject(object_key=urlsplit(url).path.lstrip("/"), signed_url=url)

    def upload_video(self, path, **options):
        if Path(path).suffix.lower() not in {".mp4", ".mov"}:
            raise HybridError("UPLOAD_FILE_UNSUPPORTED")
        options.pop("maximum_bytes", None)
        return self.upload_file(path, maximum_bytes=200 * 1024 * 1024, **options)

    def verify(self, url, size, expected):
        validate_temporary_url(url)
        with self.session.get(url, headers={"Range": "bytes=0-63"}, timeout=(10, 30),
                              stream=True, allow_redirects=False) as response:
            if response.status_code not in {200, 206}:
                raise HybridError("TEMP_LINK_NOT_READABLE", 503)
            if response.status_code == 206:
                match = re.fullmatch(r"bytes 0-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if not match or int(match[2]) != size or int(match[1]) != len(expected) - 1:
                    raise HybridError("TEMP_LINK_NOT_READABLE", 503)
            elif response.headers.get("Content-Length") != str(size):
                raise HybridError("TEMP_LINK_NOT_READABLE", 503)
            prefix = bytearray()
            for chunk in response.iter_content(64):
                prefix.extend(chunk[:64 - len(prefix)])
                if len(prefix) >= len(expected):
                    break
            if bytes(prefix) != expected:
                raise HybridError("TEMP_LINK_NOT_READABLE", 503)

    def delete(self, _key):
        # The host has no documented client deletion API. Never claim deletion.
        return False
