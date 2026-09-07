"""Explicit, per-user desktop URL registration; not run by normal setup.

Run --dry-run first. Uses only OS-provided registration tools, never a network
installer. Registration affects this one protocol and never Codex/WorkBuddy MCP.
"""
import argparse
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

SCHEME = "czmiyou-yzzh"
BUNDLE_ID = "cn.czmiyou.yzzh.launcher"


def applescript(root):
    python = root / ".venv/bin/python"
    helper = root / "scripts/start_local.py"
    command = shlex.join([str(python), str(helper)]) + " >/dev/null 2>&1 &"
    quoted = command.replace("\\", "\\\\").replace('"', '\\"')
    return f'''on run
do shell script "{quoted}"
end run
on open location ignoredURL
do shell script "{quoted}"
end open location
'''


def register(root, *, dry_run=False):
    root = Path(root).resolve()
    if not dry_run:
        candidates = ([root / ".venv/Scripts/pythonw.exe", root / ".venv/Scripts/python.exe"]
                      if sys.platform == "win32" else [root / ".venv/bin/python"])
        if not any(path.is_file() for path in candidates) or not (root / "scripts/start_local.py").is_file() or not (root / "runtime/yzzh_local/mcp.py").is_file():
            raise SystemExit("Complete bundle setup first; no desktop registration was changed.")
    if sys.platform == "darwin":
        target = Path.home() / "Applications/CZMIYOU衣装智换启动器.app"
        print("Register only czmiyou-yzzh://start for this user:", target)
        if dry_run:
            return
        if target.exists():
            plist = target / "Contents/Info.plist"
            if not plist.is_file() or plistlib.loads(plist.read_bytes()).get("CFBundleIdentifier") != BUNDLE_ID:
                raise SystemExit("Existing application is not owned by this launcher; no changes made.")
            raise SystemExit("Launcher already exists. Keep the bundle at its installed path; request an explicit launcher update instead of overwriting it.")
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["/usr/bin/osacompile", "-o", str(target), "-"],
                       input=applescript(root), text=True, check=True)
        plist = target / "Contents/Info.plist"
        data = plistlib.loads(plist.read_bytes())
        data.update(CFBundleIdentifier=BUNDLE_ID, LSUIElement=True,
                    CFBundleURLTypes=[{"CFBundleURLName":BUNDLE_ID,"CFBundleURLSchemes":[SCHEME]}])
        plist.write_bytes(plistlib.dumps(data))
        subprocess.run(["/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister",
                        "-f", str(target)], check=True)
    elif sys.platform == "win32":
        print("Register only HKCU\\Software\\Classes\\czmiyou-yzzh (current user)")
        if dry_run:
            return
        import winreg
        path = "Software\\Classes\\" + SCHEME
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path):
                raise SystemExit("Protocol already registered; no existing handler was overwritten.")
        except FileNotFoundError:
            pass
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:CZMIYOU YZZH Launcher")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        python = root / ".venv/Scripts/pythonw.exe"
        if not python.is_file():
            python = root / ".venv/Scripts/python.exe"
        command = subprocess.list2cmdline([str(python), str(root / "scripts/start_local.py")])
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path + "\\shell\\open\\command") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)
    else:
        raise SystemExit("Desktop wake registration supports macOS/Windows only; start the plugin through your Agent on this OS.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    register(Path(__file__).resolve().parents[1], dry_run=args.dry_run)
