"""Launch only a complete customer bundle; never fall back to server source."""
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
runtime = root / "runtime"
python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
if not (runtime / "yzzh_local" / "mcp.py").is_file():
    raise SystemExit("Incomplete source scaffold: build the customer ZIP first; see README.")
if not python.is_file():
    raise SystemExit("Run this bundle's scripts/setup.py first. No dependencies installed automatically.")
os.chdir(runtime)
os.execv(str(python), [str(python), "-m", "yzzh_local.mcp"])
