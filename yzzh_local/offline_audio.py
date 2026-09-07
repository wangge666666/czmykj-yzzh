"""Run the optional original audio extension with Python networking disabled."""
import runpy
import socket
from pathlib import Path


def blocked(*args, **kwargs):
    raise RuntimeError("音频扩展缺少已下载权重；请单独安装，插件不会自动联网下载。")


if __name__ == "__main__":
    socket.create_connection = blocked
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    try:
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "demucs_wav_separate.py"), run_name="__main__")
    except Exception:
        raise SystemExit("人声分离需要另行安装 Demucs 音频扩展及完整权重；本次没有下载模型。") from None
