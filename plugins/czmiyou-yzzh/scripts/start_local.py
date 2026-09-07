"""Fixed-action desktop helper. Never interpret a supplied URL as a command."""
import os
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    runtime = root / "runtime"
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file() or not (runtime / "yzzh_local/mcp.py").is_file():
        raise SystemExit("Install this complete bundle with scripts/setup.py first.")
    # Ignore all external URL arguments. The registered scheme can only wake.
    os.chdir(runtime)
    os.execv(str(python), [str(python), "-c",
        "from yzzh_local.mcp import ensure_daemon; ensure_daemon()"])


if __name__ == "__main__":
    main()
