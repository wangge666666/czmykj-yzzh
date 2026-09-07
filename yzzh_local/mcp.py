"""Minimal stdio MCP 2025-06-18 adapter. Long operations run in the local daemon."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import requests

from hybrid_shared import HybridError
from .app import default_root


def schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


STRING = {"type": "string"}
TOOLS = [
    ("health", "检查衣装智换插件环境；不代表付费链路已验收。", schema()),
    ("open_workbench", "默认打开原项目页面；单段辅助工具的计划预览/批准用 view=agent。不要自动批准。", schema({"view":{"type":"string","enum":["original","agent"]}})),
    ("original_analyze", "在原版长视频页面创建本地拆镜任务，不上传；之后用 original_status 查询，原页面可继续操作。", schema(
        {"path":STRING, "project":{"type":"string","enum":["virtual","real"]}, "manual_cuts":STRING}, ["path"])),
    ("original_status", "查询原版工作流任务；与单段辅助任务的 status 不同，不重复执行。", schema({"job_id":STRING}, ["job_id"])),
    ("list_projects", "列出本地衣装智换项目和处理状态。", schema()),
    ("import_video", "仅导入用户明确选择的本地视频；不上传。", schema({"path": STRING}, ["path"])),
    ("status", "查询持久化本地任务状态，不重复执行。", schema({"project_id": STRING}, ["project_id"])),
    ("process", "本地分镜、打码或深度处理；返回后查询状态。缺模型时先请求安装许可。", schema(
        {"project_id": STRING, "artifact_id": STRING, "operation": {"type": "string", "enum": ["split", "mosaic", "depth"]}},
        ["project_id", "artifact_id", "operation"])),
    ("plan", "创建用户自带 Key 的单段参考重绘方案；本地准备，不上传媒体、不查询中央价格。模型可传空字符串使用本地设置。", schema(
        {"project_id": STRING, "artifact_id": STRING, "image_paths": {"type": "array", "items": STRING, "minItems": 1, "maxItems": 3},
         "prompt": STRING, "model": STRING, "resolution": STRING, "ratio": STRING, "duration": {"type": "integer", "minimum": 4, "maximum": 15}},
        ["project_id", "artifact_id", "image_paths", "prompt", "model"])),
    ("submit", "提交已在工作台人工批准的方案，会上传素材并产生费用；未批准必须拒绝。", schema({"project_id": STRING}, ["project_id"])),
    ("poll", "查询既有供应商任务，成功后取回输出；时间卡到期仍可取回，不会创建新生成。", schema({"project_id": STRING}, ["project_id"])),
    ("export", "把已登记产物复制到用户指定的新文件；不会覆盖已有文件。", schema(
        {"project_id": STRING, "artifact_id": STRING, "destination": STRING}, ["project_id", "artifact_id", "destination"])),
]


def connection():
    info = json.loads((default_root() / "connection.json").read_text())
    if type(info.get("port")) is not int or not 1024 <= info["port"] <= 65535 or not isinstance(info.get("session"), str):
        raise HybridError("INVALID_LOCAL_CONNECTION")
    return info


def invoke(name, arguments):
    info = connection()
    base = f"http://127.0.0.1:{info['port']}"
    if name == "open_workbench":
        health = invoke("health", {})
        if not health.get("logged_in"):
            from urllib.parse import urlsplit
            login = urlsplit(health["login_url"])
            webbrowser.open(f"{login.scheme}://{login.netloc}")
            return {"message": "已打开 CZMIYOU 统一登录；登录后在我的应用点击衣装智换。首次需按安装说明注册本机启动器。"}
        path = "/agent-workbench" if arguments.get("view") == "agent" else "/"
        webbrowser.open(base + path + "#session=" + info["session"])
        return {"url": base, "message": "请在浏览器中登录、检查和批准；不要将密码交给对话模型。"}
    response = requests.post(base + "/api/call", json={"name": name, "arguments": arguments},
        headers={"X-Yzzh-Session": info["session"], "X-Yzzh-Request": "1"}, timeout=(3, 250), allow_redirects=False)
    if response.status_code != 200:
        raise HybridError(response.json().get("error", "LOCAL_REQUEST_REJECTED"))
    return response.json()


def ensure_daemon():
    try:
        health = invoke("health", {})
    except Exception:
        health = None
    if health is not None:
        if health.get("version") != "0.5.0" or health.get("mode") not in {"byok", "platform"}:
            raise HybridError("LOCAL_DAEMON_VERSION_MISMATCH_RESTART", 409)
        return
    root = default_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Detached daemon survives MCP disconnects and conversation changes.
    log = root / "daemon.log"
    fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as output:
        subprocess.Popen([sys.executable, "-m", "yzzh_local.app", "--data-dir", str(root)],
            stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True,
            cwd=str(Path(__file__).resolve().parent.parent))
    for _ in range(50):
        time.sleep(0.1)
        try:
            invoke("health", {})
            return
        except Exception:
            pass
    raise HybridError("LOCAL_START_FAILED_CHECK_PRIVATE_LOG", 503)


class Protocol:
    def __init__(self, call=invoke):
        self.call = call
        self.initialized = False

    def handle(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        key, method = message.get("id"), message["method"]
        if "id" not in message:
            return None
        reply = {"jsonrpc": "2.0", "id": key}
        if method == "initialize":
            self.initialized = True
            version = (message.get("params") or {}).get("protocolVersion")
            reply["result"] = {"protocolVersion": version if version in {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"} else "2025-06-18",
                "serverInfo": {"name": "czmiyou-yzzh", "version": "0.5.0"}, "capabilities": {"tools": {}},
                "instructions": "先查询项目状态。付费方案只能由用户在工作台批准；不要代填密码，不要绕过批准，不要重发结果不明的任务。"}
        elif method == "ping":
            reply["result"] = {}
        elif not self.initialized:
            reply["error"] = {"code": -32000, "message": "Initialize first"}
        elif method == "tools/list":
            reply["result"] = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS]}
        elif method == "tools/call":
            params = message.get("params") or {}
            name, args = params.get("name"), params.get("arguments", {})
            spec = next((s for n, _, s in TOOLS if n == name), None)
            if spec is None or not isinstance(args, dict) or set(args) - set(spec["properties"]) or set(spec["required"]) - set(args):
                reply["error"] = {"code": -32602, "message": "Invalid tool arguments"}
            else:
                try:
                    result = self.call(name, args)
                    reply["result"] = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}
                except Exception as error:
                    code = error.code if isinstance(error, HybridError) else "LOCAL_TOOL_FAILED"
                    reply["result"] = {"content": [{"type": "text", "text": code}], "isError": True}
        else:
            reply["error"] = {"code": -32601, "message": "Method not found"}
        return reply


def main():
    ensure_daemon()
    protocol = Protocol()
    while True:
        line = sys.stdin.buffer.readline(1024 * 1024 + 1)
        if not line:
            break
        if len(line) > 1024 * 1024:
            break
        try:
            reply = protocol.handle(json.loads(line))
        except (ValueError, TypeError, AttributeError):
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if reply is not None:
            print(json.dumps(reply, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
